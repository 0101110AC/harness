import { NgTemplateOutlet } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { finalize } from 'rxjs';
import { ANNOTATION_STATUS_META, SEVERITY_META } from '../../core/dictionaries';
import { AnnotationOut } from '../../core/models/annotation.model';
import { DashboardOverview } from '../../core/models/dashboard.model';
import { ProjectOut } from '../../core/models/project.model';
import { AnnotationService } from '../../core/services/annotation.service';
import { AuthService } from '../../core/services/auth.service';
import { DashboardService } from '../../core/services/dashboard.service';
import { ProjectService } from '../../core/services/project.service';
import { IconComponent, IconName } from '../../shared/icon/icon';
import { formatAbsoluteTime, formatRelativeTime } from '../../shared/time';

interface StatCard {
  readonly key: 'drawings' | 'annotations' | 'confirmed';
  readonly label: string;
  readonly icon: IconName;
  readonly loading: boolean;
  readonly error: boolean;
  readonly value: string;
  readonly hint: string;
  readonly trendText: string | null;
  readonly trendTone: 'up' | 'down' | null;
  /** 下钻目标：统计卡不是死数字（prd-01 §4.3） */
  readonly link: readonly string[];
  readonly queryParams: Record<string, string>;
}

const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

@Component({
  selector: 'app-dashboard',
  imports: [RouterLink, IconComponent, NgTemplateOutlet],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './dashboard.html',
})
export class DashboardComponent {
  private readonly dashboardService = inject(DashboardService);
  private readonly projectService = inject(ProjectService);
  private readonly annotationService = inject(AnnotationService);
  private readonly auth = inject(AuthService);
  private readonly router = inject(Router);

  private readonly now = new Date();

  protected readonly userName = computed(() => this.auth.currentUser()?.name ?? '');
  /** 按钮级权限：没有 project:write 的人看不到"新建项目"，而不是点完等 403 */
  protected readonly canCreateProject = computed(() => this.auth.hasPermission('project:write'));
  protected readonly greeting = ((): string => {
    const hour = this.now.getHours();
    if (hour < 6) {
      return '夜深了';
    }
    return hour < 12 ? '早上好' : hour < 18 ? '下午好' : '晚上好';
  })();
  protected readonly todayText = `${this.now.getFullYear()} 年 ${this.now.getMonth() + 1} 月 ${this.now.getDate()} 日 · ${WEEKDAYS[this.now.getDay()]}`;

  protected readonly overview = signal<DashboardOverview | null>(null);
  protected readonly overviewLoading = signal(true);
  protected readonly overviewError = signal(false);

  protected readonly recentProjects = signal<readonly ProjectOut[]>([]);
  protected readonly recentTotal = signal(0);
  protected readonly recentLoading = signal(true);
  protected readonly recentError = signal(false);

  /** 三张卡共用一份总览数据；loading / error 状态下仍渲染三张卡，避免布局跳动。 */
  protected readonly cards = computed<StatCard[]>(() => {
    const overview = this.overview();
    const loading = this.overviewLoading();
    const failed = this.overviewError();
    const ready = !loading && !failed && overview !== null;

    const value = (pick: (data: DashboardOverview) => number): string =>
      ready && overview ? String(pick(overview)) : '—';
    const hint = (pick: (data: DashboardOverview) => string, fallback: string): string =>
      ready && overview ? pick(overview) : fallback;

    const change = ready && overview ? overview.wow_change : null;
    const trendText =
      change === null ? null : `${change >= 0 ? '+' : '-'}${Math.round(Math.abs(change) * 100)}%`;
    const trendTone = change === null ? null : change >= 0 ? ('up' as const) : ('down' as const);

    const result: StatCard[] = [
      {
        key: 'drawings',
        label: '待审图纸',
        icon: 'file',
        loading,
        error: failed,
        value: value((data) => data.pending_drawings),
        hint: hint((data) => `其中 ${data.pending_drawings_in_review} 张预审中`, '图纸状态与预审进度'),
        trendText: null,
        trendTone: null,
        link: ['/projects'],
        queryParams: { filter: 'pending-review' },
      },
      {
        key: 'annotations',
        label: '待处理批注',
        icon: 'annotation',
        loading,
        error: failed,
        value: value((data) => data.pending_annotations),
        hint: hint((data) => `其中 ${data.pending_annotations_to_review} 条待复核`, '指派给我的未关闭批注'),
        trendText: null,
        trendTone: null,
        link: ['/annotations'],
        queryParams: { assignee: 'me' },
      },
      {
        key: 'confirmed',
        label: '本周确认问题',
        icon: 'check',
        loading,
        error: failed,
        value: value((data) => data.confirmed_issues_this_week),
        hint: hint(
          (data) => (data.confirmed_issues_last_week > 0 ? `上周 ${data.confirmed_issues_last_week} 条` : '上周无数据'),
          'AI 采纳并关闭的批注',
        ),
        trendText,
        trendTone,
        link: ['/projects'],
        queryParams: { closed_this_week: '1' },
      },
    ];

    return result;
  });

  constructor() {
    this.loadOverview();
    this.loadRecentProjects();
    this.loadTodos();
  }

  protected loadOverview(): void {
    this.overviewLoading.set(true);
    this.overviewError.set(false);
    this.dashboardService
      .getOverview()
      .pipe(finalize(() => this.overviewLoading.set(false)))
      .subscribe({
        next: (overview) => this.overview.set(overview),
        error: () => this.overviewError.set(true),
      });
  }

  protected loadRecentProjects(): void {
    this.recentLoading.set(true);
    this.recentError.set(false);
    this.projectService
      .getAll({ sort: 'recent', pageSize: 3 })
      .pipe(finalize(() => this.recentLoading.set(false)))
      .subscribe({
        next: (page) => {
          this.recentProjects.set(page.items.slice(0, 3));
          this.recentTotal.set(page.total);
        },
        error: () => this.recentError.set(true),
      });
  }

  protected timeAgo(iso: string): string {
    return formatRelativeTime(iso);
  }

  protected absoluteTime(iso: string): string {
    return formatAbsoluteTime(iso);
  }

  /* ---------- 待办中心（指派给我的未关闭批注） ---------- */

  protected readonly todos = signal<readonly AnnotationOut[]>([]);
  protected readonly todoLoading = signal(true);
  protected readonly todoError = signal(false);
  protected readonly todoCount = computed(() => this.todos().length);

  protected loadTodos(): void {
    const userId = this.auth.currentUser()?.id;
    if (!userId) {
      this.todoLoading.set(false);
      return;
    }
    this.todoLoading.set(true);
    this.todoError.set(false);
    this.annotationService
      .list({ assigneeId: userId, pageSize: 20 })
      .pipe(finalize(() => this.todoLoading.set(false)))
      .subscribe({
        next: (page) => {
          // 后端只支持单状态筛选，这里排除已关闭后取前 5 条：
          // 排序已在后端按"严重度 → 更新时间"做过，前端不再重排
          this.todos.set((page.items ?? []).filter((item) => item.status !== 'CLOSED').slice(0, 5));
        },
        error: () => this.todoError.set(true),
      });
  }

  protected severityDot(severity: AnnotationOut['severity']): string {
    return SEVERITY_META[severity].dot;
  }

  protected severityText(severity: AnnotationOut['severity']): string {
    return SEVERITY_META[severity].text;
  }

  protected severityLabel(severity: AnnotationOut['severity']): string {
    return SEVERITY_META[severity].label;
  }

  protected statusChip(status: AnnotationOut['status']): string {
    return ANNOTATION_STATUS_META[status].chip;
  }

  protected statusLabel(status: AnnotationOut['status']): string {
    return ANNOTATION_STATUS_META[status].label;
  }

  protected openTodo(annotation: AnnotationOut): void {
    void this.router.navigate(
      ['/projects', annotation.project_id, 'drawings', annotation.drawing_id],
      { queryParams: { annotation: annotation.id, focus: '1' } },
    );
  }

  /** 上传入口没有项目上下文：落到最近更新的项目详情（上传按钮在那儿），没有项目就去列表。 */
  protected goUpload(): void {
    const target = this.recentProjects()[0];
    if (target) {
      void this.router.navigate(['/projects', target.id]);
      return;
    }
    void this.router.navigate(['/projects']);
  }
}
