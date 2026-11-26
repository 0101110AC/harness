import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  OnInit,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { finalize } from 'rxjs';
import { readErrorMessage } from '../../core/api-error';
import { SEVERITY_META, SEVERITY_ORDER } from '../../core/dictionaries';
import {
  ReportDraftOut,
  ReportMetric,
  ReportSection,
  RiskDecisionRecord,
} from '../../core/models/review.model';
import { DrawingService } from '../../core/services/drawing.service';
import { ReviewService } from '../../core/services/review.service';
import { ToastService } from '../../core/services/toast.service';
import { IconComponent } from '../../shared/icon/icon';
import { renderMarkdown } from '../../shared/markdown';

const REJECT_MIN = 10;
const REJECT_MAX = 1000;

const REJECT_TAGS: readonly string[] = ['遗漏问题', '分类不当', '结论有误', '需补充依据'];

/**
 * 卡点二：报告初稿确认页（prd-02 §6.7）。
 *
 * 做成整页而不是弹窗：报告要读完才能确认，弹窗高度锁死了阅读行为。
 * "确认生成"必须勾选阅读声明——防的是"点两下就通过"的盲签。
 */
@Component({
  selector: 'app-report-confirm',
  imports: [RouterLink, IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './report-confirm.html',
})
export class ReportConfirmComponent implements OnInit {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly reviewService = inject(ReviewService);
  private readonly drawingService = inject(DrawingService);
  private readonly toast = inject(ToastService);
  private readonly destroyRef = inject(DestroyRef);

  readonly pid = input.required<string>();
  readonly did = input.required<string>();
  readonly tid = input.required<string>();

  protected readonly report = signal<ReportDraftOut | null>(null);
  protected readonly loading = signal(true);
  protected readonly error = signal(false);
  protected readonly empty = signal(false);
  protected readonly drawingName = signal('');

  protected readonly acknowledged = signal(false);
  protected readonly approveBusy = signal(false);
  protected readonly rejectOpen = signal(false);
  protected readonly rejectBusy = signal(false);
  protected readonly rejectRemark = signal('');
  protected readonly rejectError = signal<string | null>(null);
  protected readonly artifactUrl = signal<string | null>(null);

  protected readonly rejectTags = REJECT_TAGS;
  protected readonly rejectMin = REJECT_MIN;
  protected readonly rejectMax = REJECT_MAX;

  private readonly queryParams = toSignal(this.route.queryParamMap, {
    initialValue: this.route.snapshot.queryParamMap,
  });
  private readonly interruptId = computed(() => this.queryParams().get('interrupt'));

  protected readonly statusText = computed(() => this.report()?.status_text ?? '');
  protected readonly isConfirmed = computed(() => this.report()?.status === 'CONFIRMED');
  protected readonly isRejected = computed(() => this.report()?.status === 'REJECTED');
  /** 确认动作必须带中断号；直接访问 /report（无 query）时按钮不可用，避免点了没反应 */
  protected readonly hasInterrupt = computed(() => Boolean(this.interruptId()));
  protected readonly canApprove = computed(
    () => this.acknowledged() && !this.isConfirmed() && !this.approveBusy() && this.hasInterrupt(),
  );
  protected readonly rejectValid = computed(() => {
    const length = this.rejectRemark().trim().length;
    return length >= REJECT_MIN && length <= REJECT_MAX;
  });
  protected readonly severitySummary = computed(() => {
    const summary = this.report()?.summary;
    return SEVERITY_ORDER.map((severity) => ({
      severity,
      label: SEVERITY_META[severity].label,
      dot: SEVERITY_META[severity].dot,
      count: summary?.by_severity?.[severity] ?? 0,
    }));
  });

  ngOnInit(): void {
    this.drawingService.getById(this.did()).subscribe({
      next: (drawing) => this.drawingName.set(drawing.name),
      error: () => undefined,
    });
    this.load();
  }

  protected load(): void {
    this.loading.set(true);
    this.error.set(false);
    this.empty.set(false);
    this.reviewService
      .getReportDraft(this.tid())
      .pipe(finalize(() => this.loading.set(false)))
      .subscribe({
        next: (report) => {
          this.report.set(report);
          this.artifactUrl.set(report.artifact_url);
          if (report.status === 'CONFIRMED') {
            this.acknowledged.set(true);
          }
        },
        error: (error: unknown) => {
          const status = (error as { status?: number }).status;
          if (status === 404) {
            this.empty.set(true);
            return;
          }
          this.error.set(true);
        },
      });
  }

  protected renderSection(section: ReportSection): string {
    return renderMarkdown(section.body_md);
  }

  protected metricValue(metric: ReportMetric): string {
    const value = metric.value ?? '—';
    const limit = metric.limit === undefined || metric.limit === null ? null : String(metric.limit);
    return limit ? `${value}（限值 ${limit}）` : String(value);
  }

  protected metricLabel(metric: ReportMetric): string {
    return metric.label ?? metric.name ?? metric.key ?? '指标';
  }

  protected decisionLabel(record: RiskDecisionRecord): string {
    return record.decision === 'MARK_EXEMPT' ? '特殊豁免' : '确认问题';
  }

  protected decisionChip(record: RiskDecisionRecord): string {
    return record.decision === 'MARK_EXEMPT'
      ? 'bg-warning-soft text-warning'
      : 'bg-danger-soft text-danger';
  }

  protected backToReview(): void {
    void this.router.navigate(['/projects', this.pid(), 'drawings', this.did(), 'review'], {
      queryParams: { task_id: this.tid() },
    });
  }

  /* ---------- 确认生成 ---------- */

  protected approve(): void {
    const report = this.report();
    const interruptId = this.interruptId();
    if (!report || !interruptId || !this.canApprove()) {
      return;
    }
    this.approveBusy.set(true);
    this.reviewService
      .confirmReport(this.did(), { interrupt_id: interruptId, decision: 'APPROVED', remark: null })
      .pipe(finalize(() => this.approveBusy.set(false)))
      .subscribe({
        next: (response) => {
          this.artifactUrl.set(response.artifact_url);
          this.report.update((current) =>
            current ? { ...current, status: response.status, artifact_url: response.artifact_url } : current,
          );
          this.toast.success('已确认，正式文档已生成');
        },
        error: (error: unknown) => {
          this.toast.error(readErrorMessage(error, '确认失败，请重试'));
          this.load();
        },
      });
  }

  protected downloadArtifact(): void {
    const url = this.artifactUrl();
    if (url) {
      window.open(url, '_blank', 'noopener');
    }
  }

  /* ---------- 退回修改 ---------- */

  protected openReject(): void {
    this.rejectRemark.set('');
    this.rejectError.set(null);
    this.rejectOpen.set(true);
  }

  protected onRejectRemark(event: Event): void {
    this.rejectRemark.set((event.target as HTMLTextAreaElement).value);
  }

  protected appendTag(tag: string): void {
    const current = this.rejectRemark();
    if (current.includes(`【${tag}】`)) {
      return;
    }
    const next = `【${tag}】${current}`.slice(0, REJECT_MAX);
    this.rejectRemark.set(next);
  }

  protected submitReject(): void {
    const interruptId = this.interruptId();
    if (!interruptId || !this.rejectValid() || this.rejectBusy()) {
      return;
    }
    this.rejectBusy.set(true);
    this.rejectError.set(null);
    this.reviewService
      .confirmReport(this.did(), {
        interrupt_id: interruptId,
        decision: 'REJECTED',
        remark: this.rejectRemark().trim(),
      })
      .pipe(finalize(() => this.rejectBusy.set(false)))
      .subscribe({
        next: () => {
          this.rejectOpen.set(false);
          this.toast.success('已退回修改，正在重新分析');
          this.backToReview();
        },
        error: (error: unknown) => {
          const code = readErrorCode(error);
          if (code === 'INTERRUPT_ALREADY_RESOLVED' || code === 'INTERRUPT_EXPIRED') {
            this.rejectOpen.set(false);
            this.toast.show(readErrorMessage(error, '该确认已失效'));
            this.load();
            return;
          }
          this.rejectError.set(readErrorMessage(error, '退回失败，请重试'));
        },
      });
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
