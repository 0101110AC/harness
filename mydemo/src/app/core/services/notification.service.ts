import { HttpClient, HttpParams } from '@angular/common/http';
import { computed, inject, Injectable, signal } from '@angular/core';
import { map, Observable } from 'rxjs';
import { API_BASE_URL } from '../api';
import {
  NotificationOut,
  NotificationQuery,
  PreferencesOut,
  ReadAllResponse,
  UnreadCountResponse,
} from '../models/notification.model';
import { Page } from '../models/project.model';

/** prd-04 §5：30s 轮询，不用 SSE（V1.5 才换）。 */
const POLL_INTERVAL_MS = 30_000;
/** 连续失败 3 次进入降级：停止轮询，等页面切回前台或手动刷新 */
const MAX_FAILURES = 3;

/**
 * 未读角标 + 消息列表的数据入口。
 *
 * 未读数是**全局单例状态**：顶栏铃铛、消息中心页、批注页都要读同一个数字，
 * 所以轮询的所有权放在这里，而不是各个组件各自 setInterval。
 * 失败策略按 prd-04 §5 落地：静默重试、连续 3 次转降级、切回前台/手动刷新恢复。
 */
@Injectable({ providedIn: 'root' })
export class NotificationService {
  private readonly http = inject(HttpClient);

  /** `null` = 还没拿到过数字：不显示角标，也不显示 0（避免误导） */
  private readonly countState = signal<number | null>(null);
  private readonly degradedState = signal(false);

  readonly unreadCount = this.countState.asReadonly();
  readonly degraded = this.degradedState.asReadonly();

  /** 角标文案：>99 显示 `99+`，0 或未知返回 null（模板据此不渲染角标） */
  readonly badgeText = computed(() => {
    const count = this.countState();
    if (count === null || count <= 0) {
      return null;
    }
    return count > 99 ? '99+' : String(count);
  });

  private timer: ReturnType<typeof setInterval> | null = null;
  private failureStreak = 0;
  private active = false;
  /** 引用计数：停掉一个消费者不能把别人的轮询一起停掉（顶栏重建 / 多入口都能起） */
  private consumers = 0;

  private readonly onVisibilityChange = (): void => {
    // 从后台切回前台立即拉一次：定时器在后台会被浏览器节流，不能等它
    if (typeof document !== 'undefined' && document.visibilityState === 'visible') {
      this.retryNow();
    }
  };

  /* ---------- 轮询 ---------- */

  start(): void {
    this.consumers += 1;
    if (this.active) {
      return;
    }
    this.active = true;
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', this.onVisibilityChange);
    }
    this.refresh();
    this.schedule();
  }

  stop(): void {
    this.consumers = Math.max(0, this.consumers - 1);
    if (this.consumers > 0) {
      return;
    }
    this.active = false;
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', this.onVisibilityChange);
    }
    this.clearTimer();
  }

  /** 定时器一轮：后台标签页不发请求（回来由 visibilitychange 补） */
  private tick(): void {
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
      return;
    }
    this.refresh();
  }

  /**
   * 静默拉取未读数：失败不弹错、不打扰，只累加失败计数。
   * 连续失败到阈值后停止轮询，避免对一个坏掉的后端持续打点。
   */
  refresh(): void {
    this.http.get<UnreadCountResponse>(`${API_BASE_URL}/notifications/unread-count`).subscribe({
      next: (response) => {
        const count = Number(response?.count);
        this.countState.set(Number.isFinite(count) ? Math.max(0, count) : 0);
        this.failureStreak = 0;
        if (this.degradedState()) {
          this.degradedState.set(false);
          if (this.active) {
            this.schedule();
          }
        }
      },
      error: () => {
        this.failureStreak += 1;
        if (this.failureStreak >= MAX_FAILURES) {
          this.degradedState.set(true);
          this.clearTimer();
        }
      },
    });
  }

  /**
   * 手动刷新 / 切回前台：重新计一轮失败次数，但**不立刻清掉降级标记**。
   * 标记只在一次成功请求之后清除（`refresh()` 的 next 里），否则用户永远看不到
   * "为什么角标不动"，而且一次注定失败的请求还会把轮询重新点着、继续打后端。
   */
  retryNow(): void {
    this.failureStreak = 0;
    this.refresh();
  }

  /* ---------- 本地乐观更新 ---------- */

  /** 单条已读：角标 −1（失败由下一轮轮询校正，不阻塞跳转） */
  applyReadLocally(): void {
    this.countState.update((count) => (count === null ? count : Math.max(0, count - 1)));
  }

  applyReadAllLocally(): void {
    this.countState.set(0);
  }

  /* ---------- 列表 ---------- */

  list(query: NotificationQuery = {}): Observable<Page<NotificationOut>> {
    let params = new HttpParams()
      .set('tab', query.tab ?? 'all')
      .set('page', String(query.page ?? 1))
      .set('page_size', String(query.pageSize ?? 20));
    if (query.type) {
      params = params.set('type', query.type);
    }
    return this.http.get<Page<NotificationOut>>(`${API_BASE_URL}/notifications`, { params });
  }

  /** 铃铛浮层：最近 N 条（契约允许数组 / 分页信封两种形态） */
  recent(limit = 5): Observable<readonly NotificationOut[]> {
    return this.http
      .get<unknown>(`${API_BASE_URL}/notifications/recent`, {
        params: new HttpParams().set('limit', String(limit)),
      })
      .pipe(map((body) => toList<NotificationOut>(body)));
  }

  /** 单条已读：契约保证幂等，重复点击不会报错 */
  markRead(id: string): Observable<void> {
    return this.http
      .post<unknown>(`${API_BASE_URL}/notifications/${encodeURIComponent(id)}/read`, {})
      .pipe(map(() => undefined));
  }

  markAllRead(): Observable<ReadAllResponse> {
    return this.http.post<ReadAllResponse>(`${API_BASE_URL}/notifications/read-all`, {});
  }

  preferences(): Observable<PreferencesOut> {
    return this.http.get<PreferencesOut>(`${API_BASE_URL}/notifications/preferences`);
  }

  /* ---------- 定时器 ---------- */

  private schedule(): void {
    this.clearTimer();
    this.timer = setInterval(() => this.tick(), POLL_INTERVAL_MS);
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}

/** 单对象 / 数组 / 分页信封 → 数组，避免为契约歧义写多份分支。 */
function toList<T>(body: unknown): readonly T[] {
  if (Array.isArray(body)) {
    return body as T[];
  }
  if (typeof body === 'object' && body !== null) {
    const items = (body as { items?: unknown }).items;
    if (Array.isArray(items)) {
      return items as T[];
    }
    if ('id' in body) {
      return [body as T];
    }
  }
  return [];
}
