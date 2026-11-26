import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  computed,
  effect,
  inject,
  input,
  signal,
  untracked,
  viewChild,
} from '@angular/core';
import { takeUntilDestroyed, toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import type { PageViewport } from 'pdfjs-dist';
import { forkJoin, of } from 'rxjs';
import { catchError, finalize } from 'rxjs/operators';
import { readErrorMessage } from '../../core/api-error';
import {
  ANNOTATION_ACTION_META,
  ANNOTATION_STATUS_META,
  DISCIPLINE_LABEL,
  DISCIPLINE_OPTIONS,
  DISCIPLINES,
  SEVERITY_META,
  SEVERITY_OPTIONS,
  SEVERITY_ORDER,
} from '../../core/dictionaries';
import {
  AnnotationAction,
  AnnotationCreateRequest,
  AnnotationOut,
  AnnotationStatus,
  ShapeType,
  TimelineItem,
  TransitionAction,
} from '../../core/models/annotation.model';
import { DrawingOut } from '../../core/models/drawing.model';
import { ProjectDetailOut, ProjectMemberOut } from '../../core/models/project.model';
import { Discipline, Location, Severity } from '../../core/models/review.model';
import { AnnotationService } from '../../core/services/annotation.service';
import { AuthService } from '../../core/services/auth.service';
import { DrawingService } from '../../core/services/drawing.service';
import { ProjectService } from '../../core/services/project.service';
import { ToastService } from '../../core/services/toast.service';
import { toScreen, toLocation, type ScreenRect } from '../../core/pdf/coordinates';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { IconComponent } from '../../shared/icon/icon';
import { MultiSelectComponent } from '../../shared/multi-select';
import { formatAbsoluteTime, formatRelativeTime } from '../../shared/time';
import {
  AnnotationCreateFormComponent,
  AnnotationFormValue,
} from './annotation-create-form';
import { AnnotationTimelineComponent } from './annotation-timeline';
import {
  AnnotationTransitionComponent,
  TransitionDraft,
} from './annotation-transition';
import {
  PdfCanvasComponent,
  type PdfRenderStatus,
  type PdfStatusEvent,
} from '../../shared/pdf/pdf-canvas';

type Tool = 'select' | 'rect' | 'point' | 'text';
type PanelTab = 'all' | AnnotationStatus;

/** 最小可创建尺寸：**屏幕像素**（prd-03 §4.1），不是 pt */
const MIN_DRAW_PX = 12;
const POINT_PX = 20;
const TEXT_BOX = { w: 120, h: 24 };
const ZOOM_MIN = 50;
const ZOOM_MAX = 400;
const HIGHLIGHT_MS = 3000;
/** viewport 尚未就绪时的占位尺寸（竖版 A4 比例），避免画布区塌成 0 高 */
const PLACEHOLDER = { w: 600, h: 800 };

const SEVERITY_COLOR: Record<Severity, string> = {
  BLOCKER: '#F5222D',
  MAJOR: '#FA8C16',
  MINOR: '#FAAD14',
  INFO: '#8C8C8C',
};

const PANEL_TABS: readonly { key: PanelTab; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'PENDING_CONFIRM', label: '待确认' },
  { key: 'ISSUED', label: '已下发' },
  { key: 'MODIFIED', label: '待复核' },
  { key: 'CLOSED', label: '已关闭' },
];

/** 画布上已落库的批注框 */
interface CanvasShape {
  id: string;
  seq: number;
  severity: Severity;
  status: AnnotationStatus;
  shapeType: ShapeType;
  title: string;
  statusLabel: string;
  assigneeName: string;
  color: string;
  fill: string;
  /** 屏幕 px：由 `toScreen(location, viewport)` 算出，缩放/旋转时随 viewport 重算 */
  rect: ScreenRect;
  closed: boolean;
  selected: boolean;
}

/** 未提交的绘制草稿：表单提交成功后由服务端数据接管 */
interface DraftShape {
  id: string;
  shapeType: ShapeType;
  location: Location;
  summary: string;
}

/** 新建批注表单的焦点位置（TEXT 工具就地输入用） */
interface TextSpot {
  x: number;
  y: number;
}

function hexToRgba(hex: string, alpha: number): string {
  const value = hex.replace('#', '');
  const r = parseInt(value.slice(0, 2), 16);
  const g = parseInt(value.slice(2, 4), 16);
  const b = parseInt(value.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

/**
 * 图纸批注主页（prd-03 §3.1）：48px 工具条 + 画布 + 380px 批注面板。
 *
 * 状态所有权：
 * - 批注列表是唯一的「形状真相」：画布与面板都从 `ordered()` 派生，序号同源；
 * - 未提交的绘制是 `drafts()`（草稿），提交成功后由服务端返回的行接管；
 * - 面板按钮只渲染后端 `allowed_actions`，前端不做身份判断（prd-03 §5.5.5）。
 */
@Component({
  selector: 'app-annotation-canvas',
  imports: [
    RouterLink,
    IconComponent,
    MultiSelectComponent,
    AnnotationCreateFormComponent,
    AnnotationTimelineComponent,
    AnnotationTransitionComponent,
    ConfirmDialogComponent,
    PdfCanvasComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '(document:keydown)': 'onDocumentKeydown($event)',
  },
  templateUrl: './annotation-canvas.html',
})
export class AnnotationCanvasComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly annotationsApi = inject(AnnotationService);
  private readonly drawingService = inject(DrawingService);
  private readonly projectService = inject(ProjectService);
  private readonly auth = inject(AuthService);
  private readonly toast = inject(ToastService);
  private readonly destroyRef = inject(DestroyRef);

  /** 由 `withComponentInputBinding()` 从 `/projects/:pid/drawings/:did` 注入 */
  readonly pid = input.required<string>();
  readonly did = input.required<string>();

  /* ---------- 上下文 ---------- */
  protected readonly project = signal<ProjectDetailOut | null>(null);
  protected readonly drawing = signal<DrawingOut | null>(null);
  protected readonly loading = signal(true);
  protected readonly loadError = signal(false);
  protected readonly annotations = signal<readonly AnnotationOut[]>([]);
  protected readonly listError = signal(false);

  /* ---------- 画布 ---------- */
  protected readonly tool = signal<Tool>('select');
  protected readonly zoom = signal(100);
  protected readonly pan = signal({ x: 0, y: 0 });
  protected readonly pageNo = signal(1);
  protected readonly dragRect = signal<ScreenRect | null>(null);
  protected readonly drafts = signal<readonly DraftShape[]>([]);
  protected readonly createDraftId = signal<string | null>(null);
  protected readonly createTitle = signal('');
  protected readonly createBusy = signal(false);
  protected readonly createError = signal<string | null>(null);
  protected readonly textSpot = signal<TextSpot | null>(null);
  protected readonly textValue = signal('');

  /* ---------- PDF 渲染层 ---------- */
  protected readonly pdfViewport = signal<PageViewport | null>(null);
  protected readonly pdfPageCount = signal<number | null>(null);
  protected readonly pdfUrl = signal<string | null>(null);
  protected readonly pdfAttempt = signal(0);
  protected readonly pdfStatus = signal<PdfRenderStatus>('idle');
  protected readonly pdfMessage = signal<string | null>(null);
  protected readonly downloading = signal(false);

  /* ---------- 面板 ---------- */
  protected readonly tab = signal<PanelTab>('all');
  protected readonly severityFilter = signal<readonly Severity[]>([...SEVERITY_ORDER]);
  protected readonly disciplineFilter = signal<readonly Discipline[]>([...DISCIPLINES]);
  protected readonly assigneeFilter = signal<string>('');
  protected readonly showClosed = signal(true);
  protected readonly selectedId = signal<string | null>(null);
  protected readonly highlightId = signal<string | null>(null);
  /** 悬停浮层：延迟 200ms 出现，移出 300ms 消失（prd-03 §4.2） */
  protected readonly hoverCard = signal<{
    id: string;
    title: string;
    statusLabel: string;
    assigneeName: string;
    x: number;
    y: number;
  } | null>(null);

  /* ---------- 时间线 ---------- */
  protected readonly timeline = signal<readonly TimelineItem[]>([]);
  protected readonly timelineLoading = signal(false);
  protected readonly timelineError = signal(false);
  protected readonly commentBusy = signal(false);
  protected readonly commentError = signal<string | null>(null);

  /* ---------- 流转 ---------- */
  protected readonly transitionAction = signal<TransitionAction | null>(null);
  protected readonly transitionTarget = signal<AnnotationOut | null>(null);
  protected readonly transitionBusy = signal(false);
  protected readonly transitionError = signal<string | null>(null);
  protected readonly deleteConfirm = signal<AnnotationOut | null>(null);

  protected readonly severityOptions = SEVERITY_OPTIONS;
  protected readonly disciplineOptions = DISCIPLINE_OPTIONS;
  protected readonly tabs = PANEL_TABS;

  /* ---------- 派生 ---------- */
  protected readonly isPdf = computed(() => this.drawing()?.file_type === 'pdf');
  /** 画布尺寸 = PDF viewport 的 CSS 尺寸；加载中给骨架尺寸，避免容器塌陷 */
  protected readonly pageWidth = computed(() =>
    this.pdfViewport() ? Math.max(1, Math.round(this.pdfViewport()!.width)) : PLACEHOLDER.w,
  );
  protected readonly pageHeight = computed(() =>
    this.pdfViewport() ? Math.max(1, Math.round(this.pdfViewport()!.height)) : PLACEHOLDER.h,
  );
  protected readonly pageCount = computed(() => {
    if (!this.isPdf()) {
      return 1;
    }
    // 文档加载后以 PDF 真实页数为准：drawing.page_count 可能是解析前的旧值
    return Math.max(1, this.pdfPageCount() ?? this.drawing()?.page_count ?? 1);
  });
  protected readonly members = computed<readonly ProjectMemberOut[]>(
    () => this.project()?.members ?? [],
  );
  /** 新建批注默认指派给当前用户（PRD §4.3「V1.0 默认自己」） */
  protected readonly myId = computed(() => this.auth.currentUser()?.id ?? null);
  protected readonly myDiscipline = signal<Discipline>('GENERAL');

  /** 排序口径：severity DESC, updated_at DESC（prd-03 §6.1）；序号由它派生，画布与列表同一套 */
  protected readonly ordered = computed(() =>
    [...this.annotations()].sort((left, right) => {
      const diff =
        SEVERITY_ORDER.indexOf(left.severity) - SEVERITY_ORDER.indexOf(right.severity);
      return diff !== 0
        ? diff
        : new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime();
    }),
  );

  private readonly seqById = computed(() => {
    const map = new Map<string, number>();
    this.ordered().forEach((annotation, index) => map.set(annotation.id, index + 1));
    return map;
  });

  protected readonly tabCounts = computed(() => {
    const counts: Record<PanelTab, number> = {
      all: this.annotations().length,
      PENDING_CONFIRM: 0,
      ISSUED: 0,
      MODIFIED: 0,
      CLOSED: 0,
    };
    for (const annotation of this.annotations()) {
      counts[annotation.status] += 1;
    }
    return counts;
  });

  protected readonly visible = computed(() => {
    const severities = new Set(this.severityFilter());
    const disciplines = new Set(this.disciplineFilter());
    const assignee = this.assigneeFilter();
    const tab = this.tab();
    return this.ordered().filter((annotation) => {
      if (tab !== 'all' && annotation.status !== tab) {
        return false;
      }
      if (annotation.status === 'CLOSED' && !this.showClosed() && tab !== 'CLOSED') {
        return false;
      }
      if (!severities.has(annotation.severity) || !disciplines.has(annotation.discipline)) {
        return false;
      }
      return !assignee || annotation.assignee?.id === assignee;
    });
  });

  protected readonly shapes = computed<CanvasShape[]>(() => {
    const viewport = this.pdfViewport();
    const pageNo = this.pageNo();
    const selectedId = this.selectedId();
    const highlightId = this.highlightId();
    if (!viewport) {
      return [];
    }
    return this.visible().flatMap((annotation) => {
      const location = annotation.location;
      // 位置失效不渲染框，只在列表里标记（prd-03 §4.2）
      if (!location || !annotation.location_valid || location.page_no !== pageNo) {
        return [];
      }
      const size = location.page_size;
      if (!size || size.w <= 0 || size.h <= 0) {
        return [];
      }
      const color = SEVERITY_COLOR[annotation.severity];
      return [
        {
          id: annotation.id,
          seq: this.seqById().get(annotation.id) ?? 0,
          severity: annotation.severity,
          status: annotation.status,
          shapeType: annotation.shape_type,
          title: annotation.title,
          statusLabel: ANNOTATION_STATUS_META[annotation.status].label,
          assigneeName: annotation.assignee?.name ?? '未指派',
          color,
          fill: hexToRgba(color, 0.08),
          rect: toScreen(location, viewport),
          closed: annotation.status === 'CLOSED',
          selected: annotation.id === selectedId || annotation.id === highlightId,
        },
      ];
    });
  });

  /** 草稿只在这里换算屏幕坐标：缩放/翻页后自动跟随，不落任何百分比 */
  protected readonly draftShapes = computed(() => {
    const viewport = this.pdfViewport();
    const pageNo = this.pageNo();
    if (!viewport) {
      return [];
    }
    return this.drafts()
      .filter((draft) => draft.location.page_no === pageNo)
      .map((draft) => ({ id: draft.id, rect: toScreen(draft.location, viewport) }));
  });

  protected readonly openCount = computed(
    () => this.annotations().filter((item) => item.status !== 'CLOSED').length,
  );

  /** 拖拽中的矩形实时尺寸：直接就是屏幕像素，和最小尺寸口径一致 */
  protected readonly dragLabel = computed(() => {
    const rect = this.dragRect();
    if (!rect) {
      return '';
    }
    return `${Math.round(rect.width)} × ${Math.round(rect.height)} px`;
  });

  protected readonly pageShapeCount = computed(
    () => this.ordered().filter((item) => item.location?.page_no === this.pageNo()).length,
  );

  protected readonly selected = computed(
    () => this.annotations().find((item) => item.id === this.selectedId()) ?? null,
  );

  protected readonly createDraft = computed(
    () => this.drafts().find((draft) => draft.id === this.createDraftId()) ?? null,
  );

  /** 存在更新的图纸版本时才问「关联到 vN？」（prd-03 §5.3 第 3 条） */
  protected readonly newerVersion = computed(() => {
    const target = this.transitionTarget();
    const drawing = this.drawing();
    if (!target || !drawing) {
      return null;
    }
    return drawing.version > target.drawing_version ? drawing.version : null;
  });

  private readonly queryParams = toSignal(this.route.queryParamMap, {
    initialValue: this.route.snapshot.queryParamMap,
  });

  private readonly pageEl = viewChild<ElementRef<HTMLDivElement>>('pageEl');
  private readonly scrollEl = viewChild<ElementRef<HTMLDivElement>>('scrollEl');
  private readonly textInput = viewChild<ElementRef<HTMLInputElement>>('textInput');
  private readonly timelinePanel = viewChild(AnnotationTimelineComponent);

  private dragStart: { x: number; y: number } | null = null;
  private panStart: { x: number; y: number; panX: number; panY: number } | null = null;
  private clickStart: { x: number; y: number } | null = null;
  private undoStack: string[] = [];
  private draftSeq = 0;
  private highlightTimer: ReturnType<typeof setTimeout> | null = null;
  private hoverInTimer: ReturnType<typeof setTimeout> | null = null;
  private hoverOutTimer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    // 路由参数变化 → 整页重来（同组件会被复用）
    effect(() => {
      const pid = this.pid();
      const did = this.did();
      untracked(() => this.bootstrap(pid, did));
    });

    // 就地输入的输入框出现后立刻聚焦（元素是条件渲染的，afterNextRender 抓不到）
    effect(() => {
      const input = this.textInput()?.nativeElement;
      if (input) {
        input.focus();
      }
    });

    // 通知跳转可能落在「已经在本页」的情形：只有 query 变、组件不重建，必须单独监听
    effect(() => {
      const targetId = this.queryParams().get('annotation');
      if (!targetId) {
        return;
      }
      untracked(() => {
        if (this.loading() || this.annotations().length === 0) {
          return; // 数据还没到，交给 bootstrap 收尾
        }
        this.focusAnnotation(targetId);
      });
    });

    this.destroyRef.onDestroy(() => {
      this.clearHighlightTimer();
      this.clearHoverTimers();
    });
  }

  /* ==================== 数据加载 ==================== */

  private bootstrap(pid: string, did: string): void {
    this.loading.set(true);
    this.loadError.set(false);
    this.annotations.set([]);
    this.selectedId.set(null);
    this.timeline.set([]);
    this.drafts.set([]);
    this.createDraftId.set(null);
    this.undoStack = [];
    this.pageNo.set(1);
    this.zoom.set(100);
    this.pan.set({ x: 0, y: 0 });
    this.tool.set('select');
    this.transitionAction.set(null);
    this.transitionTarget.set(null);
    this.pdfViewport.set(null);
    this.pdfPageCount.set(null);
    this.pdfUrl.set(null);
    this.pdfStatus.set('idle');
    this.pdfMessage.set(null);
    this.pdfAttempt.set(0);

    forkJoin({
      project: this.projectService.getById(pid).pipe(catchError(() => of(null))),
      drawing: this.drawingService.getById(did),
      annotations: this.annotationsApi.listByDrawing(did),
    }).subscribe({
      next: ({ project, drawing, annotations }) => {
        this.project.set(project);
        this.drawing.set(drawing);
        this.annotations.set(annotations);
        this.loading.set(false);
        this.applyPdfSource();
        this.focusFromQuery();
      },
      error: () => {
        this.loadError.set(true);
        this.loading.set(false);
      },
    });
  }

  private reloadAnnotations(onDone?: () => void): void {
    this.annotationsApi
      .listByDrawing(this.did())
      .pipe(finalize(() => onDone?.()))
      .subscribe({
        next: (list) => {
          this.annotations.set(list);
          this.listError.set(false);
        },
        error: () => this.listError.set(true),
      });
  }

  /** 取短时签名 URL：PDF.js 直接以这个地址做 Range 请求，不需要带 Authorization 头 */
  private applyPdfSource(): void {
    const drawing = this.drawing();
    if (!drawing || drawing.file_type !== 'pdf') {
      this.pdfUrl.set(null);
      this.pdfStatus.set('idle');
      return;
    }
    this.pdfStatus.set('loading');
    this.pdfMessage.set(null);
    this.drawingService
      .getViewUrl(drawing.id)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (view) => this.pdfUrl.set(view.url),
        error: (error: unknown) => {
          this.pdfStatus.set('error');
          this.pdfMessage.set(readErrorMessage(error, '获取预览地址失败'));
        },
      });
  }

  protected onPdfPageReady(info: { viewport: PageViewport; pageCount: number }): void {
    this.pdfViewport.set(info.viewport);
    this.pdfPageCount.set(info.pageCount);
    if (this.pageNo() > info.pageCount) {
      this.pageNo.set(info.pageCount);
    }
  }

  protected onPdfStatus(event: PdfStatusEvent): void {
    this.pdfStatus.set(event.status);
    this.pdfMessage.set(event.status === 'error' ? (event.message ?? 'PDF 渲染失败') : null);
  }

  protected retryPdf(): void {
    this.pdfAttempt.update((value) => value + 1);
    this.applyPdfSource();
  }

  /** DXF 无法在线渲染：只提供下载（短时签名 URL，仍然走 view-url） */
  protected downloadDrawing(): void {
    const drawing = this.drawing();
    if (!drawing || this.downloading()) {
      return;
    }
    this.downloading.set(true);
    this.drawingService
      .getViewUrl(drawing.id)
      .pipe(finalize(() => this.downloading.set(false)))
      .subscribe({
        next: (view) => window.open(view.url, '_blank', 'noopener'),
        error: (error: unknown) => this.toast.error(readErrorMessage(error, '获取下载地址失败')),
      });
  }

  /** 消息跳转：`?annotation=<id>&focus=1` → 选中该批注、翻到它所在页、高亮 3s */
  private focusFromQuery(): void {
    const targetId = this.queryParams().get('annotation');
    if (targetId) {
      this.focusAnnotation(targetId);
    }
  }

  private focusAnnotation(targetId: string): void {
    const target = this.annotations().find((item) => item.id === targetId);
    if (!target || !target.location_valid) {
      this.toast.show('该批注已不存在或位置失效');
      return;
    }
    this.pageNo.set(target.location?.page_no ?? 1);
    this.selectedId.set(targetId);
    this.highlightId.set(targetId);
    this.loadTimeline(targetId);
    this.clearHighlightTimer();
    this.highlightTimer = setTimeout(() => this.highlightId.set(null), HIGHLIGHT_MS);
  }

  private clearHighlightTimer(): void {
    if (this.highlightTimer !== null) {
      clearTimeout(this.highlightTimer);
      this.highlightTimer = null;
    }
  }

  /* ==================== 画布：指针交互 ==================== */

  protected onPointerDown(event: PointerEvent): void {
    if (event.button !== 0) {
      return;
    }
    const element = this.pageEl()?.nativeElement;
    if (!element) {
      return;
    }
    const point = this.relativePoint(event, element);
    const tool = this.tool();

    if (tool === 'select') {
      this.panStart = {
        x: event.clientX,
        y: event.clientY,
        panX: this.pan().x,
        panY: this.pan().y,
      };
      this.capturePointer(element, event.pointerId);
      return;
    }
    // 没有 viewport 就没有坐标系（PDF 未就绪 / DXF）：不接受绘制
    if (!this.pdfViewport()) {
      return;
    }
    if (tool === 'rect') {
      this.dragStart = point;
      this.dragRect.set(null);
      this.capturePointer(element, event.pointerId);
      return;
    }
    // 点标记 / 文字：抬起时才算落点，位移超过 4px 视为误拖
    this.clickStart = point;
  }

  protected onPointerMove(event: PointerEvent): void {
    const element = this.pageEl()?.nativeElement;
    if (!element) {
      return;
    }
    if (this.panStart) {
      this.pan.set({
        x: clamp(this.panStart.panX + (event.clientX - this.panStart.x), -800, 800),
        y: clamp(this.panStart.panY + (event.clientY - this.panStart.y), -800, 800),
      });
      return;
    }
    if (this.dragStart) {
      const current = this.relativePoint(event, element);
      const rect = this.normalize(this.dragStart, current);
      this.dragRect.set(rect);
    }
  }

  protected onPointerUp(event: PointerEvent): void {
    const element = this.pageEl()?.nativeElement;
    if (!element) {
      return;
    }
    if (this.panStart) {
      this.panStart = null;
      this.releaseCapture(event, element);
      return;
    }
    if (this.dragStart) {
      this.finishRect(event, element);
      this.releaseCapture(event, element);
      return;
    }
    const tool = this.tool();
    if ((tool === 'point' || tool === 'text') && this.clickStart) {
      const current = this.relativePoint(event, element);
      const moved = Math.hypot(current.x - this.clickStart.x, current.y - this.clickStart.y);
      this.clickStart = null;
      if (moved > 4) {
        return;
      }
      if (tool === 'point') {
        this.createPointDraft(current);
      } else {
        this.textSpot.set(current);
        this.textValue.set('');
      }
    }
  }

  protected onPointerCancel(event: PointerEvent): void {
    const element = this.pageEl()?.nativeElement;
    this.dragStart = null;
    this.panStart = null;
    this.clickStart = null;
    this.dragRect.set(null);
    if (element) {
      this.releaseCapture(event, element);
    }
  }

  protected onWheel(event: WheelEvent): void {
    if (!this.pdfViewport()) {
      return;
    }
    event.preventDefault();
    const delta = event.deltaY > 0 ? -10 : 10;
    this.zoom.update((value) => clamp(value + delta, ZOOM_MIN, ZOOM_MAX));
  }

  /** 捕获指针：拖到画布外也能继续收 move/up；部分环境不接受（合成事件 / 老浏览器）就退回普通事件 */
  private capturePointer(element: HTMLElement, pointerId: number): void {
    try {
      element.setPointerCapture(pointerId);
    } catch {
      // 不支持指针捕获时退化为元素内事件，不影响正常绘制
    }
  }

  private releaseCapture(event: PointerEvent, element: HTMLElement): void {
    try {
      if (element.hasPointerCapture(event.pointerId)) {
        element.releasePointerCapture(event.pointerId);
      }
    } catch {
      // 忽略：指针已结束
    }
  }

  /**
   * 屏幕坐标 → 页面内坐标。
   * 必须用**实时 getBoundingClientRect()**：页面的宽高随缩放变、位置随平移与滚动变，
   * 任何一处缓存都会让批注"画到别处去"。clamp 到页面内 = 自动裁剪越界框（prd-03 §10）。
   */
  private relativePoint(event: PointerEvent, element: HTMLElement): { x: number; y: number } {
    const rect = element.getBoundingClientRect();
    return {
      x: clamp(event.clientX - rect.left, 0, rect.width),
      y: clamp(event.clientY - rect.top, 0, rect.height),
    };
  }

  private normalize(
    start: { x: number; y: number },
    end: { x: number; y: number },
  ): ScreenRect {
    return {
      left: Math.min(start.x, end.x),
      top: Math.min(start.y, end.y),
      width: Math.abs(end.x - start.x),
      height: Math.abs(end.y - start.y),
    };
  }

  /** 屏幕 px → PDF 坐标：单位与旋转只在 `coordinates.ts` 里处理 */
  private rectToLocation(rect: ScreenRect): Location {
    const drawing = this.drawing();
    return toLocation(rect, this.pdfViewport()!, {
      pageNo: this.pageNo(),
      fileType: drawing?.file_type ?? 'pdf',
    });
  }

  private finishRect(event: PointerEvent, element: HTMLElement): void {
    const start = this.dragStart;
    this.dragStart = null;
    this.dragRect.set(null);
    if (!start) {
      return;
    }
    const viewport = this.pdfViewport();
    if (!viewport) {
      return;
    }
    const end = this.relativePoint(event, element);
    const width = Math.abs(end.x - start.x);
    const height = Math.abs(end.y - start.y);
    // 最小尺寸按屏幕像素判定：换成 pt 判会让 200% 缩放下的合法框被误杀
    if (width < MIN_DRAW_PX || height < MIN_DRAW_PX) {
      this.toast.error('框太小，请重新绘制');
      return;
    }
    const location = this.rectToLocation(this.normalize(start, end));
    this.pushDraft(
      'RECT',
      location,
      `第 ${this.pageNo()} 页 · 框 ${Math.round(location.bbox.w)}×${Math.round(location.bbox.h)}pt`,
    );
  }

  private createPointDraft(point: { x: number; y: number }): void {
    if (!this.pdfViewport()) {
      return;
    }
    const rect = {
      left: point.x - POINT_PX / 2,
      top: point.y - POINT_PX / 2,
      width: POINT_PX,
      height: POINT_PX,
    };
    const location = this.rectToLocation(rect);
    this.pushDraft('POINT', location, `第 ${this.pageNo()} 页 · 点标记`);
  }

  private commitTextDraft(): void {
    const spot = this.textSpot();
    const viewport = this.pdfViewport();
    const text = this.textValue().trim();
    if (!spot) {
      return;
    }
    if (!text || !viewport) {
      this.cancelTextDraft();
      return;
    }
    const rect = { left: spot.x, top: spot.y, width: TEXT_BOX.w, height: TEXT_BOX.h };
    const location = this.rectToLocation(rect);
    this.textSpot.set(null);
    this.pushDraft('TEXT', location, `第 ${this.pageNo()} 页 · 文字`, text);
  }

  protected onTextInput(event: Event): void {
    this.textValue.set((event.target as HTMLInputElement).value.slice(0, 60));
  }

  protected onTextKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter') {
      event.preventDefault();
      this.commitTextDraft();
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      this.cancelTextDraft();
    }
  }

  protected cancelTextDraft(): void {
    this.textSpot.set(null);
    this.textValue.set('');
  }

  /* ==================== 草稿与撤销 ==================== */

  private pushDraft(
    shapeType: ShapeType,
    location: Location,
    summary: string,
    initialTitle: string | null = null,
  ): void {
    const id = `draft-${++this.draftSeq}`;
    const draft: DraftShape = {
      id,
      shapeType,
      location,
      summary,
    };
    this.drafts.update((list) => [...list, draft]);
    // 撤销栈只保留最近 10 步（prd-03 §4.1）
    this.undoStack = [...this.undoStack, id].slice(-10);
    this.createTitle.set(initialTitle ?? '');
    this.createDraftId.set(id);
    this.createError.set(null);
    // 表单在面板内联展开：从详情切回列表，否则用户看不到表单
    this.selectedId.set(null);
    this.tool.set(shapeType === 'RECT' ? 'rect' : this.tool());
  }

  /** Ctrl+Z：撤销本次会话最后一次未提交的绘制 */
  protected undo(): void {
    const lastId = this.undoStack[this.undoStack.length - 1];
    if (!lastId) {
      this.toast.show('没有可撤销的绘制');
      return;
    }
    this.undoStack = this.undoStack.slice(0, -1);
    this.drafts.update((list) => list.filter((draft) => draft.id !== lastId));
    if (this.createDraftId() === lastId) {
      this.createDraftId.set(null);
      this.createError.set(null);
    }
  }

  protected cancelCreate(): void {
    const draftId = this.createDraftId();
    if (draftId) {
      this.drafts.update((list) => list.filter((draft) => draft.id !== draftId));
      this.undoStack = this.undoStack.filter((id) => id !== draftId);
    }
    this.createDraftId.set(null);
    this.createError.set(null);
  }

  protected setTool(tool: Tool): void {
    this.tool.set(tool);
    this.dragStart = null;
    this.dragRect.set(null);
    this.clickStart = null;
    this.cancelTextDraft();
  }

  protected onDocumentKeydown(event: KeyboardEvent): void {
    // 输入框里的一切按键交还给浏览器（否则打字会触发工具切换）
    const target = event.target;
    if (target instanceof HTMLElement && target.closest('input, textarea, select, [contenteditable="true"]')) {
      return;
    }
    if (event.key === 'Escape') {
      this.cancelTextDraft();
      if (this.createDraftId()) {
        // Esc 取消当前绘制并回到选择工具（prd-03 §4.1）
        this.cancelCreate();
        this.setTool('select');
        return;
      }
      if (this.selectedId()) {
        this.selectedId.set(null);
        return;
      }
      this.setTool('select');
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
      event.preventDefault();
      this.undo();
      return;
    }
    switch (event.key.toLowerCase()) {
      case 'v':
        this.setTool('select');
        break;
      case 'r':
        this.setTool('rect');
        break;
      case 'p':
        this.setTool('point');
        break;
      case 't':
        this.setTool('text');
        break;
      default:
        break;
    }
  }

  /* ==================== 新建批注 ==================== */

  protected submitCreate(value: AnnotationFormValue): void {
    const draft = this.createDraft();
    const project = this.project();
    if (!draft || this.createBusy()) {
      return;
    }
    this.createBusy.set(true);
    this.createError.set(null);

    const payload: AnnotationCreateRequest = {
      project_id: project?.id ?? this.pid(),
      drawing_id: this.did(),
      shape_type: draft.shapeType,
      location: draft.location,
      title: value.title,
      content: value.content,
      severity: value.severity,
      discipline: value.discipline,
      assignee_id: value.assignee_id,
      due_date: value.due_date,
    };

    this.annotationsApi
      .create(payload)
      .pipe(finalize(() => this.createBusy.set(false)))
      .subscribe({
        next: (created) => {
          this.drafts.update((list) => list.filter((item) => item.id !== draft.id));
          this.undoStack = this.undoStack.filter((id) => id !== draft.id);
          this.createDraftId.set(null);
          this.annotations.update((list) => [...list, created]);
          // 表单清空但保持工具选中，方便连续标注（prd-03 §4.3 提交行为）
          this.tool.set(draft.shapeType === 'RECT' ? 'rect' : this.tool());
          this.toast.success('批注已创建');
        },
        error: (error: unknown) => {
          // 失败保留表单内容与草稿，用户改完可直接重试（prd-03 §10）
          this.createError.set(readErrorMessage(error, '保存失败，请检查网络'));
        },
      });
  }

  /* ==================== 选中与时间线 ==================== */

  protected selectAnnotation(annotation: AnnotationOut): void {
    if (this.selectedId() === annotation.id) {
      return;
    }
    this.selectedId.set(annotation.id);
    if (annotation.location_valid && annotation.location) {
      this.pageNo.set(annotation.location.page_no);
    }
    this.commentError.set(null);
    this.loadTimeline(annotation.id);
  }

  protected backToList(): void {
    this.selectedId.set(null);
    this.timeline.set([]);
  }

  private loadTimeline(id: string): void {
    this.timelineLoading.set(true);
    this.timelineError.set(false);
    this.annotationsApi
      .timeline(id)
      .pipe(finalize(() => this.timelineLoading.set(false)))
      .subscribe({
        next: (items) => {
          // 时间正序（最早在上），后端分页顺序不作假设
          this.timeline.set(
            [...items].sort(
              (left, right) =>
                new Date(left.created_at).getTime() - new Date(right.created_at).getTime(),
            ),
          );
        },
        error: () => this.timelineError.set(true),
      });
  }

  protected submitComment(content: string): void {
    const id = this.selectedId();
    if (!id || this.commentBusy()) {
      return;
    }
    this.commentBusy.set(true);
    this.commentError.set(null);
    this.annotationsApi
      .comment(id, content)
      .pipe(finalize(() => this.commentBusy.set(false)))
      .subscribe({
        next: () => {
          this.timelinePanel()?.clearDraft();
          this.loadTimeline(id);
          this.reloadAnnotations();
        },
        error: (error: unknown) => {
          this.commentError.set(readErrorMessage(error, '评论发送失败'));
        },
      });
  }

  /* ==================== 状态流转 ==================== */

  protected openTransition(
    annotation: AnnotationOut,
    action: AnnotationAction,
    event?: Event,
  ): void {
    event?.stopPropagation();
    if (action === 'DELETE') {
      this.deleteConfirm.set(annotation);
      return;
    }
    if (action === 'EDIT' || action === 'COMMENT') {
      return;
    }
    this.transitionTarget.set(annotation);
    this.transitionAction.set(action as TransitionAction);
    this.transitionError.set(null);
  }

  protected closeTransition(): void {
    this.transitionAction.set(null);
    this.transitionTarget.set(null);
    this.transitionError.set(null);
  }

  protected submitTransition(draft: TransitionDraft): void {
    const target = this.transitionTarget();
    const action = this.transitionAction();
    if (!target || !action || this.transitionBusy()) {
      return;
    }
    this.transitionBusy.set(true);
    this.transitionError.set(null);

    this.annotationsApi
      .transition(target.id, {
        action,
        row_version: target.row_version,
        assignee_id: draft.assignee_id ?? null,
        version_drawing_id: draft.version_drawing_id ?? null,
        close_remark: draft.close_remark ?? null,
        reject_reason: draft.reject_reason ?? null,
      })
      .pipe(finalize(() => this.transitionBusy.set(false)))
      .subscribe({
        next: (updated) => {
          this.annotations.update((list) =>
            list.map((item) => (item.id === updated.id ? updated : item)),
          );
          this.closeTransition();
          this.toast.success(`已${this.actionLabel(action)}`);
          this.reloadAnnotations();
          if (this.selectedId() === updated.id) {
            this.loadTimeline(updated.id);
          }
        },
        error: (error: unknown) => {
          const status = (error as { status?: number }).status;
          if (status === 409) {
            // 乐观锁冲突：继续留在弹窗里只会让用户反复失败，关窗并刷新
            this.closeTransition();
            this.toast.error('该批注已被他人修改，已为你刷新');
            this.reloadAnnotations(() => this.refreshSelectedFromList());
            return;
          }
          // 403 等身份变化：按钮是后端下发的，报错说明权限已变，刷新后按钮会跟着变
          this.transitionError.set(readErrorMessage(error, '操作失败，请重试'));
          if (status === 403) {
            this.reloadAnnotations();
          }
        },
      });
  }

  protected confirmDelete(): void {
    const target = this.deleteConfirm();
    if (!target) {
      return;
    }
    this.transitionBusy.set(true);
    this.annotationsApi
      .remove(target.id)
      .pipe(finalize(() => this.transitionBusy.set(false)))
      .subscribe({
        next: () => {
          this.deleteConfirm.set(null);
          this.annotations.update((list) => list.filter((item) => item.id !== target.id));
          if (this.selectedId() === target.id) {
            this.backToList();
          }
          this.toast.success('批注已作废');
        },
        error: (error: unknown) => {
          this.deleteConfirm.set(null);
          this.toast.error(readErrorMessage(error, '作废失败'));
        },
      });
  }

  private refreshSelectedFromList(): void {
    const id = this.selectedId();
    if (!id) {
      return;
    }
    if (!this.annotations().some((item) => item.id === id)) {
      this.backToList();
      return;
    }
    this.loadTimeline(id);
  }

  private actionLabel(action: TransitionAction): string {
    return ANNOTATION_ACTION_META[action]?.label ?? '操作';
  }

  /* ==================== 面板辅助 ==================== */

  protected actionsOf(annotation: AnnotationOut): AnnotationAction[] {
    // 只渲染 D5 已落地的动作；EDIT 需要编辑表单，留到 D6（见复盘「已知边界」）
    return (annotation.allowed_actions ?? []).filter(
      (action) => action !== 'EDIT' && action !== 'COMMENT',
    );
  }

  protected actionClass(action: AnnotationAction): string {
    const meta = ANNOTATION_ACTION_META[action];
    const base = 'rounded-md px-2 py-1 text-[11px] font-medium transition-colors';
    switch (meta?.kind) {
      case 'primary':
        return `${base} bg-primary text-white hover:bg-primary-hover`;
      case 'danger':
        return `${base} bg-danger text-white hover:bg-danger-hover`;
      default:
        return `${base} bg-slate-100 text-slate-600 hover:bg-slate-200`;
    }
  }

  protected actionLabelOf(action: AnnotationAction): string {
    return ANNOTATION_ACTION_META[action]?.label ?? action;
  }

  protected statusLabel(status: AnnotationStatus): string {
    return ANNOTATION_STATUS_META[status].label;
  }

  protected statusChip(status: AnnotationStatus): string {
    return ANNOTATION_STATUS_META[status].chip;
  }

  protected statusText(status: AnnotationStatus): string {
    return ANNOTATION_STATUS_META[status].text;
  }

  protected severityBar(severity: Severity): string {
    switch (severity) {
      case 'BLOCKER':
        return 'bg-severity-blocker';
      case 'MAJOR':
        return 'bg-severity-major';
      case 'MINOR':
        return 'bg-severity-minor';
      default:
        return 'bg-severity-info';
    }
  }

  protected severityLabel(severity: Severity): string {
    return SEVERITY_META[severity].label;
  }

  protected disciplineLabel(discipline: Discipline): string {
    return DISCIPLINE_LABEL[discipline] ?? discipline;
  }

  protected seqOf(id: string): number {
    return this.seqById().get(id) ?? 0;
  }

  protected relative(iso: string): string {
    return formatRelativeTime(iso);
  }

  protected absolute(iso: string): string {
    return formatAbsoluteTime(iso);
  }

  /** 逾期：超过截止日期且未关闭（prd-03 §6.1 徽标） */
  protected overdue(annotation: AnnotationOut): boolean {
    if (!annotation.due_date || annotation.status === 'CLOSED') {
      return false;
    }
    return new Date(`${annotation.due_date}T23:59:59`).getTime() < Date.now();
  }

  protected onTab(tab: PanelTab): void {
    this.tab.set(tab);
  }

  protected onSeverityFilter(values: readonly string[]): void {
    this.severityFilter.set(values as readonly Severity[]);
  }

  protected onDisciplineFilter(values: readonly string[]): void {
    this.disciplineFilter.set(values as readonly Discipline[]);
  }

  protected onAssigneeFilter(event: Event): void {
    this.assigneeFilter.set((event.target as HTMLSelectElement).value);
  }

  /* ==================== 画布辅助 ==================== */

  protected zoomIn(): void {
    this.zoom.update((value) => clamp(value + 10, ZOOM_MIN, ZOOM_MAX));
  }

  protected zoomOut(): void {
    this.zoom.update((value) => clamp(value - 10, ZOOM_MIN, ZOOM_MAX));
  }

  /** 适应宽度：按滚动容器可用宽度反算百分比，而不是写死 100% */
  protected fitWidth(): void {
    const viewport = this.pdfViewport();
    const container = this.scrollEl()?.nativeElement;
    if (!viewport || !container) {
      return;
    }
    const padding = 48; // p-6 左右各 24px
    const baseWidth = viewport.width / (viewport.scale || 1);
    const available = Math.max(120, container.clientWidth - padding);
    const percent = clamp(Math.round((available / baseWidth) * 100), ZOOM_MIN, ZOOM_MAX);
    this.zoom.set(percent);
    this.pan.set({ x: 0, y: 0 });
  }

  protected prevPage(): void {
    this.pageNo.update((value) => Math.max(1, value - 1));
  }

  protected nextPage(): void {
    this.pageNo.update((value) => Math.min(this.pageCount(), value + 1));
  }

  protected onShapeEnter(shape: CanvasShape): void {
    this.clearHoverTimers();
    this.hoverInTimer = setTimeout(() => {
      this.hoverCard.set({
        id: shape.id,
        title: shape.title,
        statusLabel: shape.statusLabel,
        assigneeName: shape.assigneeName,
        x: shape.rect.left + shape.rect.width / 2,
        y: shape.rect.top,
      });
    }, 200);
  }

  protected onShapeLeave(): void {
    this.clearHoverTimers();
    this.hoverOutTimer = setTimeout(() => this.hoverCard.set(null), 300);
  }

  private clearHoverTimers(): void {
    if (this.hoverInTimer !== null) {
      clearTimeout(this.hoverInTimer);
      this.hoverInTimer = null;
    }
    if (this.hoverOutTimer !== null) {
      clearTimeout(this.hoverOutTimer);
      this.hoverOutTimer = null;
    }
  }

  /** 绘制态下点击已有批注不选中它（prd-03 §4.1 通用规则 1），也不打断这次拉框 */
  protected onShapePointerDown(event: PointerEvent): void {
    if (this.tool() === 'select') {
      event.stopPropagation();
    }
  }

  protected boxClick(shape: CanvasShape, event: MouseEvent): void {
    event.stopPropagation();
    const annotation = this.annotations().find((item) => item.id === shape.id);
    if (annotation) {
      this.selectAnnotation(annotation);
    }
  }

  protected retry(): void {
    this.bootstrap(this.pid(), this.did());
  }
}
