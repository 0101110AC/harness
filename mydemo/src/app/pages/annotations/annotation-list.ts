import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  signal,
  untracked,
} from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router } from '@angular/router';
import { finalize } from 'rxjs';
import { map } from 'rxjs/operators';
import { ANNOTATION_STATUS_META, SEVERITY_META, SEVERITY_ORDER } from '../../core/dictionaries';
import {
  AnnotationOut,
  AnnotationStatus,
} from '../../core/models/annotation.model';
import { Severity } from '../../core/models/review.model';
import { AnnotationService } from '../../core/services/annotation.service';
import { AuthService } from '../../core/services/auth.service';
import { IconComponent } from '../../shared/icon/icon';
import { formatAbsoluteTime, formatRelativeTime } from '../../shared/time';

const PAGE_SIZE = 20;
type Tab = 'all' | 'mine';

interface SeverityOption {
  readonly value: Severity;
  readonly label: string;
}

/**
 * 全局批注页（prd-03 §6.1 的跨项目视图）。
 *
 * 与画布页的分工：这里只做"找批注"，所有定位都跳回 D5 画布并用 `?annotation=`
 * 交给画布高亮——批注的编辑、流转、时间线仍然只有画布页一个入口，避免两套交互。
 */
@Component({
  selector: 'app-annotation-list',
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './annotation-list.html',
})
export class AnnotationListComponent {
  private readonly api = inject(AnnotationService);
  private readonly auth = inject(AuthService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  protected readonly items = signal<readonly AnnotationOut[]>([]);
  protected readonly total = signal(0);
  protected readonly page = signal(1);
  protected readonly pages = signal(1);
  protected readonly loading = signal(true);
  protected readonly loadError = signal(false);

  protected readonly tab = signal<Tab>('all');
  protected readonly severity = signal<Severity | ''>('');
  protected readonly status = signal<AnnotationStatus | ''>('');

  protected readonly severityOptions: readonly SeverityOption[] = SEVERITY_ORDER.map((value) => ({
    value,
    label: SEVERITY_META[value].label,
  }));
  protected readonly statusOptions: readonly { value: AnnotationStatus; label: string }[] = (
    ['PENDING_CONFIRM', 'ISSUED', 'MODIFIED', 'CLOSED'] as AnnotationStatus[]
  ).map((value) => ({ value, label: ANNOTATION_STATUS_META[value].label }));

  protected readonly hasFilter = computed(
    () => this.severity() !== '' || this.status() !== '',
  );
  protected readonly rangeStart = computed(() =>
    this.total() === 0 ? 0 : (this.page() - 1) * PAGE_SIZE + 1,
  );
  protected readonly rangeEnd = computed(() => Math.min(this.page() * PAGE_SIZE, this.total()));
  protected readonly myName = computed(() => this.auth.currentUser()?.name ?? '');

  private readonly queryTab = toSignal(
    this.route.queryParamMap.pipe(
      map((params) => (params.get('assignee') === 'me' ? 'mine' : 'all') as Tab),
    ),
    { initialValue: (this.route.snapshot.queryParamMap.get('assignee') === 'me' ? 'mine' : 'all') as Tab },
  );

  private lastAppliedTab: Tab | null = null;

  constructor() {
    // 地址栏是 Tab 的唯一来源：/?assignee=me 可以被消息或工作台直接深链
    effect(() => {
      const tab = this.queryTab();
      untracked(() => this.applyTab(tab));
    });
  }

  private applyTab(tab: Tab): void {
    if (this.lastAppliedTab === tab) {
      return;
    }
    this.lastAppliedTab = tab;
    this.tab.set(tab);
    this.reloadFromFirstPage();
  }

  protected setTab(tab: Tab): void {
    if (this.tab() === tab) {
      return;
    }
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { assignee: tab === 'mine' ? 'me' : null },
      queryParamsHandling: 'merge',
      replaceUrl: true,
    });
  }

  /* ---------- 数据 ---------- */

  protected load(assigneeId: string | null = this.assigneeId()): void {
    this.loading.set(true);
    this.loadError.set(false);
    this.api
      .list({
        assigneeId: assigneeId ?? undefined,
        severity: this.severity() || undefined,
        status: this.status() || undefined,
        page: this.page(),
        pageSize: PAGE_SIZE,
      })
      .pipe(finalize(() => this.loading.set(false)))
      .subscribe({
        next: (result) => {
          this.items.set(result.items ?? []);
          this.total.set(result.total ?? 0);
          this.pages.set(Math.max(1, result.pages ?? 1));
        },
        error: () => this.loadError.set(true),
      });
  }

  private assigneeId(): string | null {
    return this.tab() === 'mine' ? (this.auth.currentUser()?.id ?? null) : null;
  }

  protected reloadFromFirstPage(): void {
    this.page.set(1);
    this.load();
  }

  protected onSeverityChange(event: Event): void {
    this.severity.set((event.target as HTMLSelectElement).value as Severity | '');
    this.reloadFromFirstPage();
  }

  protected onStatusChange(event: Event): void {
    this.status.set((event.target as HTMLSelectElement).value as AnnotationStatus | '');
    this.reloadFromFirstPage();
  }

  protected clearFilters(): void {
    this.severity.set('');
    this.status.set('');
    this.reloadFromFirstPage();
  }

  protected prevPage(): void {
    if (this.page() <= 1) {
      return;
    }
    this.page.update((value) => value - 1);
    this.load();
  }

  protected nextPage(): void {
    if (this.page() >= this.pages()) {
      return;
    }
    this.page.update((value) => value + 1);
    this.load();
  }

  /* ---------- 定位 ---------- */

  /** 行点击 / 标题点击 / 定位按钮走同一条跳转：交给画布页选中并高亮 */
  protected open(annotation: AnnotationOut, event?: Event): void {
    event?.stopPropagation();
    void this.router.navigate(
      ['/projects', annotation.project_id, 'drawings', annotation.drawing_id],
      { queryParams: { annotation: annotation.id, focus: '1' } },
    );
  }

  protected openProjects(): void {
    void this.router.navigate(['/projects']);
  }

  /* ---------- 展示 ---------- */

  protected severityMeta(severity: Severity): { label: string; text: string; dot: string } {
    return SEVERITY_META[severity];
  }

  protected statusChip(status: AnnotationStatus): string {
    return ANNOTATION_STATUS_META[status].chip;
  }

  protected statusLabel(status: AnnotationStatus): string {
    return ANNOTATION_STATUS_META[status].label;
  }

  protected relative(iso: string): string {
    return formatRelativeTime(iso);
  }

  protected absolute(iso: string): string {
    return formatAbsoluteTime(iso);
  }

  protected dueText(annotation: AnnotationOut): string {
    if (!annotation.due_date) {
      return '无截止';
    }
    return annotation.due_date;
  }
}
