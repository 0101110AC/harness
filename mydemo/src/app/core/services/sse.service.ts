import { HttpClient } from '@angular/common/http';
import { Injectable, inject, signal } from '@angular/core';
import { Observable, Subject } from 'rxjs';
import { API_BASE_URL } from '../api';
import { AgentEventType, ReviewStreamEvent, StreamTokenOut } from '../models/review.model';

/** prd-06 §9.5：退避 1s/2s/4s/8s，最多 5 次；>45s 无任何事件判链路假死。 */
const BACKOFF_MS = [1_000, 2_000, 4_000, 8_000] as const;
const MAX_RETRIES = 5;
const HEARTBEAT_TIMEOUT_MS = 45_000;
const WATCHDOG_INTERVAL_MS = 5_000;
/** 连接建立后极短时间内被关闭、且没收到任何业务事件 → 按 token 失效（401）处理。 */
const AUTH_FAILURE_WINDOW_MS = 2_000;

export type StreamState =
  | 'idle'
  | 'connecting'
  | 'open'
  | 'reconnecting'
  | 'disconnected'
  | 'closed';

export interface SseOptions {
  readonly drawingId: string;
  readonly threadId: string;
  /** 首屏快照的 `last_seq`；重连时作为 `from_seq` 续传 */
  readonly fromSeq?: number;
}

/** 前端实际注册监听的事件（prd-02 §5.3.2 的 V1.0 口径 + hitl_resolved/终态/重置/心跳）。 */
const CONSUMED_EVENTS: readonly AgentEventType[] = [
  'run_started',
  'plan_created',
  'progress',
  'agent_thought',
  'issue_found',
  'risk_interrupt',
  'hitl_resolved',
  'report_ready',
  'report_interrupt',
  'run_completed',
  'run_failed',
  'run_returned',
  'stream_reset',
  'heartbeat',
];

/**
 * SSE 连接的封装。刻意做成"每次订阅一个实例"：
 *
 * - `EventSource` 会自动重连，但它复用同一个 URL——而这个 URL 里的一次性
 *   `stream_token` 已经消费/过期，自动重连必然 401，所以 error 时要主动
 *   `close()` 掉浏览器自带的重连，改由本类换 token 后重连；
 * - 续传靠 `from_seq` 查询参数（prd-07 §5.5，等价 `Last-Event-ID`），
 *   因为新建的 EventSource 无法手动设置 Last-Event-ID 头；
 * - 所有定时器与连接都收敛在 `close()`，页面在 `DestroyRef` 里调用一次即可。
 */
export class SseConnection {
  private readonly eventsSubject = new Subject<ReviewStreamEvent>();
  readonly events: Observable<ReviewStreamEvent> = this.eventsSubject.asObservable();

  readonly state = signal<StreamState>('idle');
  readonly attempt = signal(0);
  readonly lastEventId = signal(0);
  readonly errorMessage = signal<string | null>(null);

  private source: EventSource | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private watchdog: ReturnType<typeof setInterval> | null = null;
  private lastActivityAt = 0;
  private openedAt = 0;
  private receivedBusinessEvent = false;
  private authRetried = false;
  private disposed = false;

  constructor(
    private readonly http: HttpClient,
    private readonly options: SseOptions,
  ) {
    this.lastEventId.set(options.fromSeq ?? 0);
  }

  /** 建立连接：先换一次性 token，再用返回的 `stream_url` 建流。 */
  start(): void {
    if (this.disposed) {
      return;
    }
    this.state.set(this.state() === 'idle' ? 'connecting' : 'reconnecting');
    this.errorMessage.set(null);
    this.lastActivityAt = Date.now();
    this.startWatchdog();

    this.http
      .post<StreamTokenOut>(
        `${API_BASE_URL}/drawings/${this.options.drawingId}/stream-token`,
        { thread_id: this.options.threadId },
      )
      .subscribe({
        next: (token) => this.openSource(token.stream_url),
        error: () => this.scheduleReconnect('实时通道鉴权失败，请刷新页面重试'),
      });
  }

  /** 手动重连（"重新连接"按钮）：重置连续失败计数，立刻重新取 token。 */
  reconnectNow(): void {
    if (this.disposed) {
      return;
    }
    this.attempt.set(0);
    this.authRetried = false;
    this.clearReconnectTimer();
    this.start();
  }

  /** 收到终态事件后由页面调用，之后不再重连。 */
  close(): void {
    if (this.disposed) {
      return;
    }
    this.disposed = true;
    this.clearReconnectTimer();
    this.stopWatchdog();
    this.closeSource();
    this.state.set('closed');
    this.eventsSubject.complete();
  }

  private openSource(streamUrl: string): void {
    if (this.disposed) {
      return;
    }
    this.closeSource();

    const source = new EventSource(this.withFromSeq(streamUrl));
    this.source = source;
    this.openedAt = Date.now();
    this.receivedBusinessEvent = false;

    source.onopen = () => {
      if (this.disposed) {
        return;
      }
      this.state.set('open');
      this.errorMessage.set(null);
      this.lastActivityAt = Date.now();
    };

    for (const type of CONSUMED_EVENTS) {
      source.addEventListener(type, (event: Event) => {
        this.onMessage(type, event as MessageEvent<string>);
      });
    }

    source.onerror = () => {
      if (this.disposed) {
        return;
      }
      // 不 close 的话浏览器会用同一个已失效的 token 无限重连，表现为 401 风暴
      this.closeSource();
      this.scheduleReconnect(null);
    };
  }

  private onMessage(type: AgentEventType, event: MessageEvent<string>): void {
    this.lastActivityAt = Date.now();

    if (type === 'heartbeat') {
      // 心跳只更新"最后心跳时间"，不进入 UI 事件流
      return;
    }

    let payload: ReviewStreamEvent;
    try {
      payload = JSON.parse(event.data) as ReviewStreamEvent;
    } catch {
      return;
    }

    const seq = Number.isFinite(payload.seq) && payload.seq > 0
      ? payload.seq
      : Number(event.lastEventId);
    if (seq > 0) {
      this.lastEventId.set(seq);
    }

    this.receivedBusinessEvent = true;
    this.authRetried = false;
    this.attempt.set(0);
    this.state.set('open');
    this.eventsSubject.next(payload);
  }

  private scheduleReconnect(errorMessage: string | null): void {
    if (this.disposed) {
      return;
    }
    this.errorMessage.set(errorMessage);
    const nextAttempt = this.attempt() + 1;

    // 连上就断、且没收到业务事件：优先判为 401（token 已消费/过期），
    // 立刻换新 token 重连一次；这也是"401 自动重取 token 并重连一次"的落点。
    const looksLikeAuthFailure =
      !this.receivedBusinessEvent && this.openedAt > 0 && Date.now() - this.openedAt < AUTH_FAILURE_WINDOW_MS;
    if (looksLikeAuthFailure && !this.authRetried) {
      this.authRetried = true;
      this.attempt.set(nextAttempt);
      this.state.set('reconnecting');
      this.start();
      return;
    }

    if (nextAttempt > MAX_RETRIES) {
      this.attempt.set(nextAttempt);
      this.state.set('disconnected');
      this.errorMessage.set(errorMessage ?? '实时连接已断开，请手动重新连接');
      return;
    }

    this.attempt.set(nextAttempt);
    this.state.set('reconnecting');
    this.clearReconnectTimer();
    const delay = BACKOFF_MS[Math.min(nextAttempt - 1, BACKOFF_MS.length - 1)];
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.start();
    }, delay);
  }

  private startWatchdog(): void {
    if (this.watchdog !== null) {
      return;
    }
    this.watchdog = setInterval(() => {
      if (this.disposed || this.source === null) {
        return;
      }
      if (Date.now() - this.lastActivityAt <= HEARTBEAT_TIMEOUT_MS) {
        return;
      }
      // 服务端 15s 一次心跳；45s 无任何事件说明链路假死（代理缓冲、半开连接）
      this.closeSource();
      this.scheduleReconnect(null);
    }, WATCHDOG_INTERVAL_MS);
  }

  private stopWatchdog(): void {
    if (this.watchdog !== null) {
      clearInterval(this.watchdog);
      this.watchdog = null;
    }
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private closeSource(): void {
    if (this.source === null) {
      return;
    }
    this.source.onopen = null;
    this.source.onerror = null;
    this.source.close();
    this.source = null;
  }

  private withFromSeq(url: string): string {
    const seq = this.lastEventId();
    if (seq <= 0) {
      return url;
    }
    const separator = url.includes('?') ? '&' : '?';
    return `${url}${separator}from_seq=${seq}`;
  }
}

/**
 * 全局注入的工厂：连接状态归 `SseConnection` 实例所有，不放 service 单例，
 * 避免一个页面关流时把另一个页面的连接状态一起改掉。
 */
@Injectable({ providedIn: 'root' })
export class SseService {
  private readonly http = inject(HttpClient);

  open(options: SseOptions): SseConnection {
    // 不在工厂里 start：调用方需要先订阅事件流，再建连（重连回放可能是同步的）
    return new SseConnection(this.http, options);
  }
}
