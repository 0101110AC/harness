import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  OnDestroy,
  computed,
  effect,
  inject,
  input,
  signal,
  untracked,
  viewChild,
} from '@angular/core';
import { toSignal, takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import type { PageViewport } from 'pdfjs-dist';
import { forkJoin, of, Subject } from 'rxjs';
import { catchError, finalize } from 'rxjs/operators';
import { readErrorMessage } from '../../core/api-error';
import {
  DISCIPLINE_LABEL,
  DISCIPLINES,
  DISCIPLINE_OPTIONS,
  ISSUE_STATUS_META,
  ISSUE_STATUS_OPTIONS,
  ISSUE_STATUSES,
  SEVERITY_META,
  SEVERITY_OPTIONS,
  SEVERITY_ORDER,
  STAGE_FALLBACK_TEXT,
} from '../../core/dictionaries';
import { DrawingOut, SeverityKey } from '../../core/models/drawing.model';
import {
  Discipline,
  HitlResumeRequest,
  InterruptTaskOut,
  IssueOut,
  IssueRow,
  IssueStatus,
  ReviewStage,
  ReviewStreamEvent,
  ReviewTaskSnapshot,
  RunStatus,
  Severity,
} from '../../core/models/review.model';
import { DrawingService } from '../../core/services/drawing.service';
import { ProjectService } from '../../core/services/project.service';
import { ReviewService } from '../../core/services/review.service';
import { SseConnection, SseService, StreamState } from '../../core/services/sse.service';
import { ToastService } from '../../core/services/toast.service';
import { toScreen } from '../../core/pdf/coordinates';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { IconComponent } from '../../shared/icon/icon';
import { MultiSelectComponent } from '../../shared/multi-select';
import { PdfCanvasComponent, type PdfStatusEvent } from '../../shared/pdf/pdf-canvas';
import { RiskConfirmModalComponent } from './risk-confirm-modal';
import { ThoughtLine, ThoughtPanelComponent, ThoughtStatus } from './thought-panel';

export type RunUiState =
  | 'idle'
  | 'running'
  | 'waiting-risk'
  | 'waiting-report'
  | 'done'
  | 'failed'
  | 'returned';

interface RunStateMeta {
  readonly label: string;
  readonly chip: string;
  readonly bar: string;
}

const RUN_STATE_META: Record<RunUiState, RunStateMeta> = {
  idle: { label: '排队中', chip: 'bg-slate-100 text-slate-600', bar: 'bg-slate-300' },
  running: { label: '运行中', chip: 'bg-primary-soft text-primary', bar: 'bg-primary' },
  'waiting-risk': { label: '已暂停', chip: 'bg-warning-soft text-warning', bar: 'bg-warning' },
  'waiting-report': { label: '待确认报告', chip: 'bg-warning-soft text-warning', bar: 'bg-warning' },
  done: { label: '已完成', chip: 'bg-success-soft text-success', bar: 'bg-success' },
  failed: { label: '失败', chip: 'bg-danger-soft text-danger', bar: 'bg-danger' },
  returned: { label: '已退回', chip: 'bg-warning-soft text-warning', bar: 'bg-warning' },
};

const TERMINAL_STATES: ReadonlySet<RunUiState> = new Set<RunUiState>(['done', 'failed', 'returned']);
const ROW_HEIGHT = 56;
const OVERSCAN = 6;

/** 画布高亮框：颜色与列表严重度同源，避免"画布红、列表橙"的两套语义。 */
const SEVERITY_BOX: Record<Severity, string> = {
  BLOCKER: 'border-severity-blocker bg-severity-blocker/10',
  MAJOR: 'border-severity-major bg-severity-major/10',
  MINOR: 'border-severity-minor bg-severity-minor/10',
  INFO: 'border-severity-info bg-severity-info/10',
};

function clampPercent(value: number): number {
  return Math.min(100, Math.max(0, Math.round(value)));
}

function clockOf(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return '--:--:--';
  }
  return [date.getHours(), date.getMinutes(), date.getSeconds()]
    .map((part) => String(part).padStart(2, '0'))
    .join(':');
}

function toRow(issue: IssueOut): IssueRow {
  return {
    id: issue.id,
    severity: issue.severity,
    discipline: issue.discipline,
    title: issue.title,
    description: issue.description,
    ruleCode: issue.rule_code,
    confidence: issue.confidence,
    status: issue.status,
    location: issue.location,
    locationValid: issue.location_valid,
    annotationId: issue.annotation_id,
    live: false,
  };
}

/** 默认排序：severity DESC, confidence DESC（prd-02 §6.1）。 */
function sortRows(rows: readonly IssueRow[]): IssueRow[] {
  return [...rows].sort((left, right) => {
    const severityDiff = SEVERITY_ORDER.indexOf(left.severity) - SEVERITY_ORDER.indexOf(right.severity);
    return severityDiff !== 0 ? severityDiff : right.confidence - left.confidence;
  });
}

/**
 * 预审中心主界面（prd-02 §3.1）：左清单 + 右画布 + 底部思考链路。
 *
 * 状态所有权：
 * - 运行态（进度 / 阶段 / 状态机）只由 SSE 事件推进；
 * - 清单是"服务端快照 + SSE 增量占位行"的合并结果，终态后整体重拉；
 * - 所有订阅、连接、定时器统一在 `DestroyRef` 与 `closeStream()` 收尾。
 */
@Component({
  selector: 'app-review-center',
  imports: [
    RouterLink,
    IconComponent,
    MultiSelectComponent,
    PdfCanvasComponent,
    ThoughtPanelComponent,
    RiskConfirmModalComponent,
    ConfirmDialogComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './review-center.html',
})
export class ReviewCenterComponent implements OnDestroy {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly reviewService = inject(ReviewService);
  private readonly drawingService = inject(DrawingService);
  private readonly projectService = inject(ProjectService);
  private readonly sse = inject(SseService);
  private readonly toast = inject(ToastService);
  private readonly destroyRef = inject(DestroyRef);

  /** 由 `withComponentInputBinding()` 从 `/projects/:pid/drawings/:did/review` 注入 */
  readonly pid = input.required<string>();
  readonly did = input.required<string>();

  /* ---------- 上下文 ---------- */
  protected readonly projectName = signal('');
  protected readonly drawing = signal<DrawingOut | null>(null);
  protected readonly task = signal<ReviewTaskSnapshot | null>(null);
  protected readonly loading = signal(true);
  protected readonly contextError = signal(false);
  protected readonly taskMissing = signal(false);

  /* ---------- 运行态 ---------- */
  protected readonly runState = signal<RunUiState>('idle');
  protected readonly percent = signal(0);
  protected readonly stageText = signal('');
  protected readonly thoughts = signal<readonly ThoughtLine[]>([]);
  protected readonly thoughtsCollapsed = signal(false);
  protected readonly reportReady = signal(false);

  /* ---------- 连接态 ---------- */
  protected readonly streamState = signal<StreamState>('idle');
  protected readonly streamAttempt = signal(0);
  protected readonly streamMessage = signal<string | null>(null);

  /* ---------- 清单 ---------- */
  protected readonly issues = signal<readonly IssueRow[]>([]);
  protected readonly issuesLoading = signal(true);
  protected readonly issuesError = signal(false);
  protected readonly scrollTop = signal(0);
  protected readonly viewportHeight = signal(480);
  protected readonly selectedIds = signal<ReadonlySet<string>>(new Set<string>());
  protected readonly focusedIssueId = signal<string | null>(null);
  protected readonly adoptBusyId = signal<string | null>(null);
  protected readonly batchConfirm = signal(false);
  protected readonly batchBusy = signal(false);

  protected readonly severityFilter = signal<readonly Severity[]>([...SEVERITY_ORDER]);
  protected readonly disciplineFilter = signal<readonly Discipline[]>([...DISCIPLINES]);
  protected readonly statusFilter = signal<readonly IssueStatus[]>(['PENDING']);

  protected readonly severityOptions = SEVERITY_OPTIONS;
  protected readonly disciplineOptions = DISCIPLINE_OPTIONS;
  protected readonly statusOptions = ISSUE_STATUS_OPTIONS;

  /* ---------- 画布（真实 PDF.js 渲染） ---------- */
  protected readonly pageNo = signal(1);
  protected readonly zoom = signal(100);
  /** 短时签名 URL；PDF 之外（DXF）保持 null，页面显示"暂不支持在线渲染" */
  protected readonly pdfUrl = signal<string | null>(null);
  /** 换值即重新拉文档（签名 URL 过期 / 用户重试） */
  protected readonly pdfAttempt = signal(0);
  protected readonly pdfViewport = signal<PageViewport | null>(null);
  protected readonly pdfStatus = signal<'idle' | 'loading' | 'ready' | 'error'>('idle');
  protected readonly pdfError = signal<string | null>(null);
  private readonly canvasViewportRef = viewChild<ElementRef<HTMLElement>>('canvasViewport');

  /* ---------- HITL ---------- */
  protected readonly riskInterrupt = signal<InterruptTaskOut | null>(null);
  protected readonly deferredRisk = signal<InterruptTaskOut | null>(null);
  protected readonly riskBusy = signal(false);
  protected readonly riskError = signal<string | null>(null);
  protected readonly reportInterruptId = signal<string | null>(null);
  protected readonly pendingReReview = signal(false);
  protected readonly reReviewBusy = signal(false);

  /* ---------- 分栏 ---------- */
  protected readonly leftWidth = signal(this.readStoredWidth());

  /* ---------- 派生 ---------- */
  protected readonly runMeta = computed(() => RUN_STATE_META[this.runState()]);
  protected readonly isTerminal = computed(() => TERMINAL_STATES.has(this.runState()));
  protected readonly isInterrupted = computed(
    () => this.runState() === 'waiting-risk' || this.runState() === 'waiting-report',
  );
  protected readonly thoughtStatus = computed<ThoughtStatus>(() => {
    switch (this.runState()) {
      case 'running':
        return 'running';
      case 'idle':
        return 'idle';
      case 'waiting-risk':
      case 'waiting-report':
      case 'returned':
        return 'waiting';
      case 'done':
        return 'done';
      case 'failed':
        return 'failed';
    }
  });
  protected readonly stageLabel = computed(() => {
    if (this.runState() === 'waiting-risk' || this.runState() === 'waiting-report') {
      return `已暂停 · ${this.stageText()}`;
    }
    return this.stageText();
  });
  protected readonly thoughtSummary = computed(() => {
    if (this.isTerminal()) {
      const seconds = ((this.task()?.cost_ms ?? 0) / 1000).toFixed(1);
      return `耗时 ${seconds}s · ${this.task()?.issue_total ?? this.issues().length} 个问题`;
    }
    return this.stageText() ? `${this.stageLabel()} · ${this.percent()}%` : '等待任务启动…';
  });

  protected readonly filteredIssues = computed(() => {
    const severities = new Set(this.severityFilter());
    const disciplines = new Set(this.disciplineFilter());
    const statuses = new Set(this.statusFilter());
    return sortRows(
      this.issues().filter(
        (issue) =>
          severities.has(issue.severity) &&
          disciplines.has(issue.discipline) &&
          statuses.has(issue.status),
      ),
    );
  });

  protected readonly visibleWindow = computed(() => {
    const total = this.filteredIssues().length;
    const start = Math.max(0, Math.floor(this.scrollTop() / ROW_HEIGHT) - OVERSCAN);
    const visibleCount = Math.ceil(this.viewportHeight() / ROW_HEIGHT) + OVERSCAN * 2;
    const end = Math.min(total, start + visibleCount);
    return {
      start,
      end,
      rows: this.filteredIssues().slice(start, end),
      totalHeight: total * ROW_HEIGHT,
      offset: start * ROW_HEIGHT,
    };
  });

  protected readonly severityCounts = computed(() => {
    const counts: Record<SeverityKey, number> = { BLOCKER: 0, MAJOR: 0, MINOR: 0, INFO: 0 };
    for (const issue of this.issues()) {
      counts[issue.severity] += 1;
    }
    return counts;
  });

  protected readonly selectedCount = computed(() => this.selectedIds().size);
  protected readonly allVisibleSelected = computed(() => {
    const rows = this.filteredIssues();
    return rows.length > 0 && rows.every((row) => this.selectedIds().has(row.id));
  });
  protected readonly focusedIssue = computed(
    () => this.issues().find((issue) => issue.id === this.focusedIssueId()) ?? null,
  );

  protected readonly pageCount = computed(() => Math.max(1, this.drawing()?.page_count ?? 1));

  protected readonly canvasRects = computed(() => {
    const pageNo = this.pageNo();
    const focusedId = this.focusedIssueId();
    const viewport = this.pdfViewport();
    // 没有真实 viewport（还没渲染完 / 非 PDF）时不画框：宁可空着，也不按估算比例乱标
    if (!viewport) {
      return [];
    }
    return this.issues().flatMap((issue) => {
      const location = issue.location;
      if (!location || !issue.locationValid || location.page_no !== pageNo) {
        return [];
      }
      if (!location.page_size || location.page_size.w <= 0 || location.page_size.h <= 0) {
        return [];
      }
      // 坐标一律走 coordinates.ts：它吃掉了 scale / rotation / origin 与版本尺寸差异
      const rect = toScreen(location, viewport);
      return [
        {
          id: issue.id,
          severity: issue.severity,
          title: issue.title,
          selected: issue.id === focusedId,
          left: rect.left,
          top: rect.top,
          width: Math.max(6, rect.width),
          height: Math.max(6, rect.height),
        },
      ];
    });
  });

  protected readonly pageIssueCount = computed(
    () => this.issues().filter((issue) => issue.location?.page_no === this.pageNo()).length,
  );

  protected readonly streamBanner = computed<{ text: string; tone: 'warning' | 'danger' } | null>(() => {
    const state = this.streamState();
    if (state === 'reconnecting') {
      return {
        text: `连接中断，正在重连（${Math.min(this.streamAttempt(), 5)}/5）…`,
        tone: 'warning',
      };
    }
    if (state === 'disconnected') {
      return { text: this.streamMessage() ?? '实时连接已断开，可手动重新连接', tone: 'danger' };
    }
    return null;
  });

  private readonly queryParams = toSignal(this.route.queryParamMap, {
    initialValue: this.route.snapshot.queryParamMap,
  });
  private readonly taskIdParam = computed(() => this.queryParams().get('task_id'));

  private readonly connection = signal<SseConnection | null>(null);
  private readonly eventStream = new Subject<ReviewStreamEvent>();
  private thoughtSequence = 0;
  private collapseTimer: ReturnType<typeof setTimeout> | null = null;
  private scrollRaf: number | null = null;
  private readonly resizeObserver = new ResizeObserver((entries) => {
    const height = entries[0]?.contentRect.height;
    if (height) {
      this.viewportHeight.set(height);
    }
  });

  private readonly listViewport = viewChild<ElementRef<HTMLDivElement>>('listViewport');

  constructor() {
    // 路由参数变化时整页重来（/drawings/a/review → /drawings/b/review 会复用实例）
    effect(() => {
      const pid = this.pid();
      const did = this.did();
      const taskId = this.taskIdParam();
      untracked(() => this.bootstrap(pid, did, taskId));
    });

    // 连接状态镜像到页面信号：模板不能持有"会被替换的 signal 实例"
    effect(() => {
      const connection = this.connection();
      if (!connection) {
        return;
      }
      this.streamState.set(connection.state());
      this.streamAttempt.set(connection.attempt());
      this.streamMessage.set(connection.errorMessage());
    });

    effect(() => {
      const element = this.listViewport()?.nativeElement;
      if (!element) {
        return;
      }
      this.resizeObserver.observe(element);
      this.viewportHeight.set(element.clientHeight);
    });

    // 图纸就绪后取短时签名 URL：只有 PDF 才渲染，DXF 保持"暂不支持在线渲染"空态
    effect(() => {
      const drawing = this.drawing();
      this.pdfAttempt();
      if (!drawing || drawing.file_type !== 'pdf') {
        untracked(() => {
          this.pdfUrl.set(null);
          this.pdfViewport.set(null);
          this.pdfStatus.set('idle');
        });
        return;
      }
      untracked(() => this.loadPdfUrl(drawing.id));
    });

    this.eventStream.pipe(takeUntilDestroyed(this.destroyRef)).subscribe((event) => this.onEvent(event));

    this.destroyRef.onDestroy(() => {
      this.closeStream();
      this.resizeObserver.disconnect();
      this.clearTimers();
    });
  }

  ngOnDestroy(): void {
    this.closeStream();
    this.clearTimers();
  }

  /* ==================== 启动链路 ==================== */

  private bootstrap(pid: string, did: string, taskId: string | null): void {
    this.closeStream();
    this.clearTimers();
    this.connection.set(null);
    this.streamState.set('idle');
    this.streamAttempt.set(0);
    this.streamMessage.set(null);
    this.runState.set('idle');
    this.percent.set(0);
    this.stageText.set('');
    this.thoughts.set([]);
    this.thoughtsCollapsed.set(false);
    this.issues.set([]);
    this.selectedIds.set(new Set());
    this.focusedIssueId.set(null);
    this.task.set(null);
    this.taskMissing.set(false);
    this.reportReady.set(false);
    this.riskInterrupt.set(null);
    this.deferredRisk.set(null);
    this.reportInterruptId.set(null);
    this.loading.set(true);
    this.contextError.set(false);
    this.issuesLoading.set(true);
    this.issuesError.set(false);
    this.pageNo.set(1);

    forkJoin({
      project: this.projectService.getById(pid).pipe(catchError(() => of(null))),
      drawing: this.drawingService.getById(did),
    }).subscribe({
      next: ({ project, drawing }) => {
        this.projectName.set(project?.name ?? '');
        this.drawing.set(drawing);
        this.pageNo.set(1);
        this.resolveTask(did, taskId);
      },
      error: () => {
        this.contextError.set(true);
        this.loading.set(false);
        this.issuesLoading.set(false);
      },
    });
  }

  private resolveTask(did: string, taskId: string | null): void {
    if (taskId) {
      this.startSession(did, taskId);
      return;
    }

    this.reviewService.listTasks(did).subscribe({
      next: (tasks) => {
        const latest = [...tasks].sort(
          (left, right) => new Date(right.created_at).getTime() - new Date(left.created_at).getTime(),
        )[0];
        if (!latest) {
          this.taskMissing.set(true);
          this.loading.set(false);
          this.issuesLoading.set(false);
          return;
        }
        this.startSession(did, latest.id);
      },
      error: () => {
        this.taskMissing.set(true);
        this.loading.set(false);
        this.issuesLoading.set(false);
      },
    });
  }

  private startSession(did: string, taskId: string): void {
    this.reviewService.getTask(taskId).subscribe({
      next: (snapshot) => {
        this.loading.set(false);
        this.applySnapshot(snapshot);
        this.loadIssues();
        this.pushThought('system', `${snapshot.status_text || '任务已就绪'} · 引擎 ${snapshot.engine}`, snapshot.created_at);

        if (this.isTerminal()) {
          return;
        }

        // 顺序按 prd-02 §6.6.4：先问有没有待处理中断，再建流，避免漏掉暂停态
        this.reviewService
          .getPendingInterrupt(did)
          .pipe(catchError(() => of(null)))
          .subscribe((interrupt) => {
            this.handlePendingInterrupt(interrupt, snapshot);
            this.connectStream(did, snapshot);
          });
      },
      error: (error: unknown) => {
        this.loading.set(false);
        this.issuesLoading.set(false);
        this.contextError.set(true);
        this.toast.error(readErrorMessage(error, '预审任务加载失败'));
      },
    });
  }

  private handlePendingInterrupt(
    interrupt: InterruptTaskOut | null,
    snapshot: ReviewTaskSnapshot,
  ): void {
    if (!interrupt || interrupt.status !== 'PENDING' || interrupt.thread_id !== snapshot.thread_id) {
      return;
    }
    if (interrupt.type === 'RISK_CONFIRM') {
      this.riskInterrupt.set(interrupt);
      this.runState.set('waiting-risk');
      return;
    }
    if (interrupt.type === 'REPORT_CONFIRM') {
      this.reportInterruptId.set(interrupt.id);
      this.runState.set('waiting-report');
      this.navigateToReport(snapshot.id, interrupt.id, interrupt.report_id);
      return;
    }
    this.toast.show('校内评审确认暂不支持在本页处理，请到评审页继续');
  }

  private connectStream(did: string, snapshot: ReviewTaskSnapshot): void {
    this.closeStream();

    const connection = this.sse.open({
      drawingId: did,
      threadId: snapshot.thread_id,
      fromSeq: snapshot.last_seq,
    });
    connection.events.pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (event) => this.eventStream.next(event),
    });
    this.connection.set(connection);
    // 先订阅再 start，避免重连（同步回放）时丢首个事件
    connection.start();
  }

  private closeStream(): void {
    const connection = this.connection();
    if (connection) {
      connection.close();
    }
  }

  /* ==================== SSE 事件 → UI ==================== */

  private onEvent(event: ReviewStreamEvent): void {
    switch (event.event) {
      case 'run_started': {
        this.runState.set('running');
        this.percent.set(clampPercent(event.data.percent ?? 0));
        this.stageText.set('引擎已启动，正在准备一致性与规范校验');
        this.pushThought(
          'system',
          `[引擎] 已启动（${event.data.engine} / ${event.data.review_mode === 'SCHOOL' ? '校园评审' : '专业审图'}）`,
          event.ts,
        );
        break;
      }
      case 'plan_created': {
        if (typeof event.data.percent === 'number') {
          this.percent.update((current) => Math.max(current, clampPercent(event.data.percent ?? 0)));
        }
        this.stageText.set('正在制定审查计划');
        this.pushThought(
          'plan',
          `计划已生成：${event.data.steps.length} 个步骤`,
          event.ts,
          event.data.rationale,
        );
        break;
      }
      case 'progress': {
        this.applyProgress(event.data.percent, event.data.stage, event.data.text);
        break;
      }
      case 'agent_thought': {
        this.pushThought('thought', event.data.text, event.ts, event.data.node ? `节点 ${event.data.node}` : undefined);
        break;
      }
      case 'issue_found': {
        this.insertLiveIssue(event);
        break;
      }
      case 'risk_interrupt': {
        this.runState.set('waiting-risk');
        this.pushThought('risk', event.data.title, event.ts);
        this.riskError.set(null);
        this.riskInterrupt.set(this.toPendingInterrupt(event));
        this.deferredRisk.set(null);
        break;
      }
      case 'hitl_resolved': {
        const current = this.riskInterrupt();
        if (current && current.id === event.data.interrupt_id) {
          this.riskInterrupt.set(null);
          this.deferredRisk.set(null);
        }
        this.runState.set('running');
        const mix = event.data.item_decisions
          .map((item) =>
            item.decision === 'MARK_EXEMPT' ? `${item.risk_id} 豁免` : `${item.risk_id} 确认`,
          )
          .join(' / ');
        this.pushThought('human', `${event.data.operator} 已处理：${mix || '风险确认'}`, event.ts);
        break;
      }
      case 'report_ready': {
        this.reportReady.set(true);
        this.percent.update((current) => Math.max(current, clampPercent(event.data.percent)));
        this.stageText.set('报告初稿已生成，等待确认');
        this.pushThought(
          'system',
          `报告初稿 v${event.data.version} 已生成（${event.data.issue_total} 个问题）`,
          event.ts,
        );
        break;
      }
      case 'report_interrupt': {
        this.runState.set('waiting-report');
        this.reportInterruptId.set(event.data.interrupt_id);
        this.pushThought('risk', event.data.title, event.ts);
        const taskId = this.task()?.id;
        if (taskId) {
          this.navigateToReport(taskId, event.data.interrupt_id, event.data.report_id);
        }
        break;
      }
      case 'run_returned': {
        this.runState.set('returned');
        this.pushThought('error', '评审已退回，需按退回意见修改后重新提交', event.ts);
        this.closeStream();
        break;
      }
      case 'run_completed': {
        this.runState.set('done');
        this.percent.set(100);
        this.stageText.set('预审完成');
        this.pushThought(
          'plan',
          `预审完成：${event.data.issue_total} 个问题，耗时 ${(event.data.duration_ms / 1000).toFixed(1)}s`,
          event.ts,
        );
        this.closeStream();
        this.loadIssues();
        this.refreshSnapshotOnce();
        this.scheduleCollapse();
        break;
      }
      case 'run_failed': {
        this.runState.set('failed');
        this.pushThought('error', `${event.data.message}（${event.data.reason}）`, event.ts);
        this.closeStream();
        this.toast.error(event.data.message || '预审失败，请重试');
        break;
      }
      case 'stream_reset': {
        this.thoughts.set([]);
        this.toast.show('部分过程记录已过期，已刷新任务与清单');
        this.refreshSnapshotOnce();
        this.loadIssues();
        break;
      }
      case 'heartbeat':
        break;
    }
  }

  private applyProgress(percent: number, stage: ReviewStage, text?: string): void {
    // 进度只增不减（prd-02 §5.2）；退回修改走 run_returned，由后端重新下发阶段
    const next = Math.max(this.percent(), clampPercent(percent));
    this.percent.set(next);
    this.stageText.set(text?.trim() || STAGE_FALLBACK_TEXT[stage] || this.stageText());
    if (this.runState() !== 'waiting-risk' && this.runState() !== 'waiting-report') {
      this.runState.set('running');
    }
  }

  private insertLiveIssue(event: Extract<ReviewStreamEvent, { event: 'issue_found' }>): void {
    const id = event.data.issue_id || `live-${event.seq}`;
    if (this.issues().some((issue) => issue.id === id)) {
      return;
    }
    const row: IssueRow = {
      id,
      severity: event.data.severity,
      discipline: event.data.discipline,
      title: event.data.title,
      description: null,
      ruleCode: null,
      confidence: event.data.confidence,
      status: 'PENDING',
      location: null,
      locationValid: false,
      annotationId: null,
      live: true,
    };
    // 占位行只保证"实时可见"，落库后由 run_completed 的全量刷新替换为带坐标的真实行
    this.issues.update((rows) => sortRows([...rows, row]));
  }

  private toPendingInterrupt(
    event: Extract<ReviewStreamEvent, { event: 'risk_interrupt' }>,
  ): InterruptTaskOut {
    const expiresAt = event.data.expires_at ?? new Date(Date.now() + 24 * 3600 * 1000).toISOString();
    return {
      id: event.data.interrupt_id,
      thread_id: event.thread_id,
      review_task_id: this.task()?.id ?? '',
      drawing_id: this.did(),
      type: 'RISK_CONFIRM',
      type_text: '风险确认',
      status: 'PENDING',
      title: event.data.title,
      items: event.data.items,
      report_id: null,
      assignee: null,
      resolution: null,
      item_decisions: [],
      remark: null,
      expires_at: expiresAt,
      remaining_seconds: event.data.remaining_seconds ?? 0,
      created_at: event.ts,
    };
  }

  /* ==================== HITL 交互 ==================== */

  protected submitRisk(payload: HitlResumeRequest): void {
    const interrupt = this.riskInterrupt();
    if (!interrupt || this.riskBusy()) {
      return;
    }
    this.riskBusy.set(true);
    this.riskError.set(null);

    this.reviewService
      .resumeRisk(this.did(), payload)
      .pipe(finalize(() => this.riskBusy.set(false)))
      .subscribe({
        next: (response) => {
          this.riskInterrupt.set(null);
          this.deferredRisk.set(null);
          this.runState.set(response.run_status === 'RUNNING' ? 'running' : this.runState());
          this.pushThought('human', '已提交人工确认，流程继续执行', new Date().toISOString());
          this.toast.success('已提交，流程继续执行');
        },
        error: (error: unknown) => {
          const code = readErrorCode(error);
          if (code === 'INTERRUPT_ALREADY_RESOLVED') {
            // 他人已处理：关窗 + 提示 + 对齐服务端状态，而不是让用户对着报错重试
            this.riskInterrupt.set(null);
            this.deferredRisk.set(null);
            this.toast.show(readErrorMessage(error, '该风险已由他人处理，流程已继续'));
            this.refreshSnapshotOnce();
            this.loadIssues();
            return;
          }
          if (code === 'INTERRUPT_EXPIRED') {
            this.riskInterrupt.set(null);
            this.deferredRisk.set(null);
            this.runState.set('failed');
            this.toast.error(readErrorMessage(error, '该确认已超时，请重新发起预审'));
            return;
          }
          // 422 等校验错误留在弹窗内，用户改完可以直接再提交
          this.riskError.set(readErrorMessage(error, '提交失败，请重试'));
        },
      });
  }

  protected deferRisk(): void {
    const interrupt = this.riskInterrupt();
    this.riskInterrupt.set(null);
    this.deferredRisk.set(interrupt);
  }

  protected reopenRisk(): void {
    const interrupt = this.deferredRisk();
    if (interrupt) {
      this.riskError.set(null);
      this.riskInterrupt.set(interrupt);
    }
  }

  protected onRiskExpired(): void {
    this.riskInterrupt.set(null);
    this.deferredRisk.set(null);
    this.runState.set('failed');
    this.toast.error('该确认已超时，请重新发起预审');
  }

  private navigateToReport(taskId: string, interruptId: string, reportId: string | null): void {
    void this.router.navigate(
      ['/projects', this.pid(), 'drawings', this.did(), 'review', taskId, 'report'],
      { queryParams: { interrupt: interruptId, report_id: reportId ?? undefined } },
    );
  }

  protected viewReport(): void {
    const taskId = this.task()?.id;
    if (!taskId) {
      return;
    }
    void this.router.navigate(['/projects', this.pid(), 'drawings', this.did(), 'review', taskId, 'report'], {
      queryParams: {
        interrupt: this.reportInterruptId() ?? undefined,
        report_id: this.task()?.report_id ?? undefined,
      },
    });
  }

  /* ==================== 清单数据 ==================== */

  protected loadIssues(): void {
    const taskId = this.task()?.id;
    if (!taskId) {
      return;
    }
    this.issuesLoading.set(true);
    this.issuesError.set(false);

    this.reviewService
      .listIssues(taskId, {
        severity: this.severityFilter(),
        discipline: this.disciplineFilter(),
        status: this.statusFilter(),
        pageSize: 100,
      })
      .pipe(finalize(() => this.issuesLoading.set(false)))
      .subscribe({
        next: (page) => {
          this.issues.set(page.items.map(toRow));
          this.syncSelectionWithRows();
        },
        error: () => this.issuesError.set(true),
      });
  }

  protected onSeverityFilter(values: readonly string[]): void {
    this.severityFilter.set(values as readonly Severity[]);
    this.afterFilterChange();
  }

  protected onDisciplineFilter(values: readonly string[]): void {
    this.disciplineFilter.set(values as readonly Discipline[]);
    this.afterFilterChange();
  }

  protected onStatusFilter(values: readonly string[]): void {
    this.statusFilter.set(values as readonly IssueStatus[]);
    this.afterFilterChange();
  }

  private afterFilterChange(): void {
    this.selectedIds.set(new Set());
    this.scrollTop.set(0);
    const viewport = this.listViewport()?.nativeElement;
    if (viewport) {
      viewport.scrollTop = 0;
    }
    this.loadIssues();
  }

  protected statusLabel(status: IssueStatus): string {
    return ISSUE_STATUS_META[status].label;
  }

  protected statusChip(status: IssueStatus): string {
    return ISSUE_STATUS_META[status].chip;
  }

  protected severityLabel(severity: Severity): string {
    return SEVERITY_META[severity].label;
  }

  protected severityText(severity: Severity): string {
    return SEVERITY_META[severity].text;
  }

  protected severityDot(severity: Severity): string {
    return SEVERITY_META[severity].dot;
  }

  protected disciplineLabel(discipline: string): string {
    return DISCIPLINE_LABEL[discipline as keyof typeof DISCIPLINE_LABEL] ?? discipline;
  }

  protected isSelected(id: string): boolean {
    return this.selectedIds().has(id);
  }

  protected toggleRow(id: string, event: Event): void {
    event.stopPropagation();
    this.selectedIds.update((current) => {
      const next = new Set(current);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  protected toggleAllVisible(): void {
    const rows = this.filteredIssues();
    if (this.allVisibleSelected()) {
      this.selectedIds.set(new Set());
      return;
    }
    this.selectedIds.set(new Set(rows.map((row) => row.id)));
  }

  protected clearSelection(): void {
    this.selectedIds.set(new Set());
  }

  private syncSelectionWithRows(): void {
    const available = new Set(this.issues().map((issue) => issue.id));
    this.selectedIds.update((current) => new Set([...current].filter((id) => available.has(id))));
  }

  protected rowClick(issue: IssueRow): void {
    this.focusedIssueId.set(issue.id);
    if (issue.location && issue.locationValid) {
      this.pageNo.set(issue.location.page_no);
    }
  }

  protected locate(issue: IssueRow, event: Event): void {
    event.stopPropagation();
    this.revealIssue(issue);
  }

  private revealIssue(issue: IssueRow): void {
    const visible = this.filteredIssues().some((row) => row.id === issue.id);
    if (!visible) {
      // 被筛选器藏起来时先放开筛选，否则"定位"点了没反应（prd-02 §6.3）
      this.severityFilter.set([...SEVERITY_ORDER]);
      this.disciplineFilter.set([...DISCIPLINES]);
      this.statusFilter.set([...ISSUE_STATUSES]);
      this.loadIssues();
    }
    this.focusedIssueId.set(issue.id);
    if (issue.location && issue.locationValid) {
      this.pageNo.set(issue.location.page_no);
    }
    // 列表重排 / 重新渲染之后再滚动，否则量到的是旧下标
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        this.scrollRowIntoView(issue.id);
        this.scrollCanvasToFocused();
      });
    });
  }

  /** 把真实 PDF 画布滚到当前高亮框附近：换页后 viewport 重算要等等，故延后两帧 */
  private scrollCanvasToFocused(): void {
    const host = this.canvasViewportRef()?.nativeElement;
    const rect = this.canvasRects().find((item) => item.selected);
    if (!host || !rect) {
      return;
    }
    const targetTop = rect.top + 40 - host.clientHeight / 2; // 留一点上边距，别贴边
    host.scrollTo({ top: Math.max(0, targetTop), behavior: 'smooth' });
  }

  private scrollRowIntoView(id: string): void {
    const index = this.filteredIssues().findIndex((row) => row.id === id);
    if (index < 0) {
      return;
    }
    const viewport = this.listViewport()?.nativeElement;
    if (!viewport) {
      return;
    }
    const targetTop = index * ROW_HEIGHT;
    const targetBottom = targetTop + ROW_HEIGHT;
    if (targetTop >= viewport.scrollTop && targetBottom <= viewport.scrollTop + viewport.clientHeight) {
      return;
    }
    viewport.scrollTop = Math.max(0, targetTop - viewport.clientHeight / 2 + ROW_HEIGHT / 2);
  }

  protected onListScroll(): void {
    if (this.scrollRaf !== null) {
      return;
    }
    this.scrollRaf = requestAnimationFrame(() => {
      this.scrollRaf = null;
      const element = this.listViewport()?.nativeElement;
      if (element) {
        this.scrollTop.set(element.scrollTop);
      }
    });
  }

  protected adopt(issue: IssueRow, event: Event): void {
    event.stopPropagation();
    if (this.adoptBusyId()) {
      return;
    }
    this.adoptBusyId.set(issue.id);
    this.reviewService
      .adopt(issue.id)
      .pipe(finalize(() => this.adoptBusyId.set(null)))
      .subscribe({
        next: () => {
          this.toast.success('已采纳为批注草稿');
          this.loadIssues();
        },
        error: (error: unknown) => this.toast.error(readErrorMessage(error, '采纳失败')),
      });
  }

  protected confirmBatchAdopt(): void {
    if (this.batchBusy()) {
      return;
    }
    const ids = [...this.selectedIds()];
    if (ids.length === 0) {
      return;
    }
    this.batchBusy.set(true);
    this.reviewService
      .batchAdopt(ids)
      .pipe(finalize(() => this.batchBusy.set(false)))
      .subscribe({
        next: (response) => {
          this.batchConfirm.set(false);
          this.selectedIds.set(new Set());
          this.toast.success(`已采纳 ${response.created} 条问题，生成 ${response.annotations.length} 条批注`);
          this.loadIssues();
        },
        error: (error: unknown) => {
          this.batchConfirm.set(false);
          // 422 整批回滚：保留勾选，用户改完可以直接重试
          this.toast.error(readErrorMessage(error, '批量采纳失败，已回滚全部'));
        },
      });
  }

  protected isLive(issue: IssueRow): boolean {
    return issue.live;
  }

  /* ==================== 画布交互 ==================== */

  protected boxClick(id: string): void {
    const issue = this.issues().find((row) => row.id === id);
    if (!issue) {
      return;
    }
    if (!this.filteredIssues().some((row) => row.id === id)) {
      this.severityFilter.set([...SEVERITY_ORDER]);
      this.disciplineFilter.set([...DISCIPLINES]);
      this.statusFilter.set([...ISSUE_STATUSES]);
      this.loadIssues();
    }
    this.focusedIssueId.set(id);
    requestAnimationFrame(() => {
      requestAnimationFrame(() => this.scrollRowIntoView(id));
    });
  }

  protected zoomIn(): void {
    this.zoom.update((value) => Math.min(400, value + 10));
  }

  protected boxClass(severity: Severity, selected: boolean): string {
    const base = SEVERITY_BOX[severity];
    return selected
      ? `${base} z-10 border-[3px] opacity-100 shadow-float ring-2 ring-primary/30`
      : `${base} opacity-70 hover:opacity-100`;
  }

  protected zoomOut(): void {
    this.zoom.update((value) => Math.max(50, value - 10));
  }

  protected fitWidth(): void {
    const host = this.canvasViewportRef()?.nativeElement;
    const viewport = this.pdfViewport();
    if (!host || !viewport) {
      this.zoom.set(100);
      return;
    }
    // viewport.width 是"当前 zoom 下的 CSS 宽"，反推 100% 基准宽再算适应宽度
    const baseWidth = viewport.width / Math.max(this.zoom(), 1);
    const available = host.clientWidth - 48; // 留出 p-6 的左右留白
    if (baseWidth <= 0 || available <= 0) {
      return;
    }
    this.zoom.set(Math.max(50, Math.min(400, Math.round((available / baseWidth) * 100))));
  }

  protected prevPage(): void {
    this.pageNo.update((value) => Math.max(1, value - 1));
  }

  protected nextPage(): void {
    this.pageNo.update((value) => Math.min(this.pageCount(), value + 1));
  }

  /** 画布页面盒的尺寸：真实 viewport 优先，未就绪时给一个占位高度避免布局跳动 */
  protected pageBoxWidth(): number {
    return Math.round(this.pdfViewport()?.width ?? 600);
  }

  protected pageBoxHeight(): number {
    return Math.round(this.pdfViewport()?.height ?? 848);
  }

  protected readonly isPdf = computed(() => this.drawing()?.file_type === 'pdf');
  protected readonly pdfRetryable = computed(() => this.pdfStatus() === 'error');

  protected retryPdf(): void {
    this.pdfAttempt.update((value) => value + 1);
  }

  protected onPdfPageReady(event: { viewport: PageViewport; pageCount: number }): void {
    this.pdfViewport.set(event.viewport);
  }

  protected onPdfStatus(event: PdfStatusEvent): void {
    this.pdfStatus.set(event.status);
    this.pdfError.set(event.message ?? null);
  }

  private loadPdfUrl(drawingId: string): void {
    this.pdfStatus.set('loading');
    this.pdfError.set(null);
    this.drawingService
      .getViewUrl(drawingId)
      .pipe(
        catchError((error: unknown) => {
          this.pdfStatus.set('error');
          this.pdfError.set(readErrorMessage(error, '未取到图纸预览地址'));
          return of(null);
        }),
      )
      .subscribe((view) => {
        if (view?.url) {
          this.pdfUrl.set(view.url);
        }
      });
  }

  /* ==================== 分栏拖拽 ==================== */

  protected startResize(event: PointerEvent): void {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = this.leftWidth();
    const onMove = (moveEvent: PointerEvent): void => {
      const next = Math.min(720, Math.max(360, startWidth + (moveEvent.clientX - startX)));
      this.leftWidth.set(Math.round(next));
    };
    const onUp = (): void => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      this.storeWidth(this.leftWidth());
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp, { once: true });
  }

  protected resizeByKeyboard(event: KeyboardEvent): void {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') {
      return;
    }
    event.preventDefault();
    const delta = event.key === 'ArrowLeft' ? -24 : 24;
    const next = Math.min(720, Math.max(360, this.leftWidth() + delta));
    this.leftWidth.set(next);
    this.storeWidth(next);
  }

  private readStoredWidth(): number {
    if (typeof window === 'undefined') {
      return 480;
    }
    const stored = Number(window.localStorage.getItem('review.center.leftWidth'));
    return Number.isFinite(stored) && stored >= 360 && stored <= 720 ? stored : 480;
  }

  private storeWidth(width: number): void {
    if (typeof window !== 'undefined') {
      window.localStorage.setItem('review.center.leftWidth', String(width));
    }
  }

  /* ==================== 其它 ==================== */

  protected retry(): void {
    this.bootstrap(this.pid(), this.did(), this.taskIdParam());
  }

  protected onReReview(): void {
    this.pendingReReview.set(true);
  }

  protected confirmReReview(): void {
    if (this.reReviewBusy()) {
      return;
    }
    this.reReviewBusy.set(true);
    this.drawingService
      .createReviewTask(this.did())
      .pipe(finalize(() => this.reReviewBusy.set(false)))
      .subscribe({
        next: (task) => {
          this.pendingReReview.set(false);
          this.toast.success('已发起新的预审任务');
          void this.router.navigate(
            ['/projects', this.pid(), 'drawings', this.did(), 'review'],
            { queryParams: { task_id: task.id }, replaceUrl: true },
          );
          if (this.taskIdParam() === task.id) {
            this.retry();
          }
        },
        error: (error: unknown) => {
          this.pendingReReview.set(false);
          this.toast.error(readErrorMessage(error, '发起预审失败'));
        },
      });
  }

  protected reconnect(): void {
    this.connection()?.reconnectNow();
  }

  private applySnapshot(snapshot: ReviewTaskSnapshot): void {
    this.task.set(snapshot);
    this.percent.set(clampPercent(snapshot.progress));
    this.stageText.set(
      snapshot.stage_text?.trim() ||
        (snapshot.stage ? STAGE_FALLBACK_TEXT[snapshot.stage] : '') ||
        snapshot.status_text ||
        '',
    );
    this.runState.set(this.mapRunStatus(snapshot.run_status, snapshot.stage));
    this.reportReady.set(Boolean(snapshot.report_id));
  }

  private mapRunStatus(status: RunStatus, stage: ReviewStage | null): RunUiState {
    switch (status) {
      case 'PENDING':
        return 'idle';
      case 'RUNNING':
        return 'running';
      case 'WAITING_HUMAN':
        return stage === 'WAITING_REPORT' ? 'waiting-report' : 'waiting-risk';
      case 'SUCCEEDED':
        return 'done';
      case 'FAILED':
      case 'CANCELED':
        return 'failed';
      case 'RETURNED':
        return 'returned';
    }
  }

  /** 终态只对齐一次快照，不做轮询（prd-02 §5.3.1 的硬要求）。 */
  private refreshSnapshotOnce(): void {
    const taskId = this.task()?.id;
    if (!taskId) {
      return;
    }
    this.reviewService.getTask(taskId).subscribe({
      next: (snapshot) => this.applySnapshot(snapshot),
      error: () => undefined,
    });
  }

  private pushThought(kind: ThoughtLine['kind'], text: string, iso: string, detail?: string): void {
    if (!text) {
      return;
    }
    const line: ThoughtLine = {
      id: ++this.thoughtSequence,
      kind,
      time: clockOf(iso),
      text: text.length > 200 ? `${text.slice(0, 200)}…` : text,
      detail,
    };
    // 面板最多保留 300 行（prd-02 §5.3.2），超出丢最早的
    this.thoughts.update((lines) => [...lines, line].slice(-300));
  }

  private scheduleCollapse(): void {
    this.clearCollapseTimer();
    this.collapseTimer = setTimeout(() => this.thoughtsCollapsed.set(true), 3000);
  }

  private clearCollapseTimer(): void {
    if (this.collapseTimer !== null) {
      clearTimeout(this.collapseTimer);
      this.collapseTimer = null;
    }
  }

  private clearTimers(): void {
    this.clearCollapseTimer();
    if (this.scrollRaf !== null) {
      cancelAnimationFrame(this.scrollRaf);
      this.scrollRaf = null;
    }
  }

  protected toggleThoughts(collapsed: boolean): void {
    this.clearCollapseTimer();
    this.thoughtsCollapsed.set(collapsed);
  }
}

function readErrorCode(error: unknown): string {
  const body = (error as { error?: unknown }).error;
  if (typeof body === 'object' && body !== null && 'code' in body) {
    const code = (body as { code?: unknown }).code;
    return typeof code === 'string' ? code : '';
  }
  return '';
}
