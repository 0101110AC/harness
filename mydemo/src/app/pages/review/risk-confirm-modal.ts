import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  OnInit,
  computed,
  input,
  output,
  signal,
} from '@angular/core';
import { RISK_LEVEL_META } from '../../core/dictionaries';
import {
  HitlDecision,
  HitlResumeRequest,
  InterruptTaskOut,
  RiskItem,
  RiskItemDecision,
} from '../../core/models/review.model';
import { IconComponent } from '../../shared/icon/icon';

const EXEMPT_REMARK_MIN = 5;
const EXEMPT_REMARK_MAX = 500;
const OVERALL_REMARK_MAX = 1000;

/**
 * 卡点一：风险确认弹窗（prd-02 §6.6）。
 *
 * 三条硬约束都在这里落地：
 * 1. 处置**不预选**——避免手滑一路"确认"把风险放过；
 * 2. 选"特殊豁免"必须填 ≥5 字依据，未填则提交按钮直接禁用；
 * 3. 不可点遮罩关闭，Esc 需要二次确认（流程是暂停的，关掉不等于放弃）。
 */
@Component({
  selector: 'app-risk-confirm-modal',
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '(document:keydown.escape)': 'onEscape()',
  },
  template: `
    <div class="fixed inset-0 z-[70] flex items-center justify-center bg-slate-950/50 px-4 py-8 backdrop-blur-sm">
      <div
        class="flex max-h-full w-full max-w-3xl animate-fade-in flex-col overflow-hidden rounded-card bg-white shadow-float"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="risk-modal-title"
      >
        <!-- 头部 -->
        <header class="flex shrink-0 items-start gap-3 border-b border-slate-100 px-6 py-4">
          <span class="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-warning-soft text-warning">
            <app-icon name="alert" [size]="17" />
          </span>
          <div class="min-w-0 flex-1">
            <h2 id="risk-modal-title" class="text-sm font-semibold text-slate-900">
              {{ interrupt().title }}
            </h2>
            <p class="mt-1 text-xs text-slate-500">
              流程已暂停，请逐项判断这是「图纸问题」还是「已获豁免」。
            </p>
          </div>
          <span
            class="inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium tabular-nums"
            [class]="remainingSeconds() <= 3600 ? 'bg-danger-soft text-danger' : 'bg-slate-100 text-slate-500'"
          >
            <app-icon name="clock" [size]="12" />
            {{ countdownText() }}
          </span>
        </header>

        <!-- 风险卡片 -->
        <div class="min-h-0 flex-1 space-y-3 overflow-y-auto px-6 py-4">
          @for (item of interrupt().items; track item.risk_id) {
            <article class="rounded-lg border border-slate-200 p-4">
              <div class="flex flex-wrap items-center gap-2">
                <span class="inline-flex items-center gap-1.5 text-xs font-semibold" [class]="levelOf(item).text">
                  <span class="h-2 w-2 rounded-full" [class]="levelOf(item).dot"></span>
                  {{ levelOf(item).label }}
                </span>
                <h3 class="min-w-0 flex-1 text-sm font-medium text-slate-900">{{ item.title }}</h3>
                @if (item.detected_value !== null || item.threshold !== null) {
                  <span class="rounded bg-slate-50 px-2 py-0.5 font-mono text-[11px] text-slate-500">
                    当前 {{ valueText(item.detected_value) }} / 限值 {{ valueText(item.threshold) }}
                  </span>
                }
              </div>

              @if (item.clause_ref) {
                <p class="mt-2 text-xs text-slate-500">依据：{{ item.clause_ref }}</p>
              }
              @if (item.why_suspicious) {
                <p class="mt-1 text-xs text-slate-500">判断理由：{{ item.why_suspicious }}</p>
              }

              <div class="mt-3 rounded-lg bg-slate-50/70 p-3">
                <p class="mb-2 text-[11px] font-medium text-slate-400">处置</p>
                <div class="flex flex-col gap-2 sm:flex-row sm:gap-6">
                  <label class="flex cursor-pointer items-center gap-2 text-xs text-slate-700">
                    <input
                      type="radio"
                      class="accent-primary"
                      [name]="'decision-' + item.risk_id"
                      [checked]="decisionOf(item.risk_id) === 'CONFIRM_AS_ISSUE'"
                      (change)="setDecision(item.risk_id, 'CONFIRM_AS_ISSUE')"
                    />
                    确认是图纸问题
                  </label>
                  <label class="flex cursor-pointer items-center gap-2 text-xs text-slate-700">
                    <input
                      type="radio"
                      class="accent-primary"
                      [name]="'decision-' + item.risk_id"
                      [checked]="decisionOf(item.risk_id) === 'MARK_EXEMPT'"
                      (change)="setDecision(item.risk_id, 'MARK_EXEMPT')"
                    />
                    标记为特殊豁免
                  </label>
                </div>

                @if (decisionOf(item.risk_id) === 'MARK_EXEMPT') {
                  <div class="mt-2.5">
                    <label class="mb-1 block text-[11px] font-medium text-slate-600" [attr.for]="'remark-' + item.risk_id">
                      豁免依据 <span class="text-danger">*</span>
                      <span class="font-normal text-slate-400">（5~500 字，将写入报告豁免说明）</span>
                    </label>
                    <input
                      [id]="'remark-' + item.risk_id"
                      type="text"
                      class="field-input !py-2 text-xs"
                      [class.field-input-invalid]="remarkInvalid(item.risk_id)"
                      [attr.maxlength]="exemptMax"
                      placeholder="例如：已取得规划部门超限批复"
                      [value]="remarkOf(item.risk_id)"
                      (input)="setRemark(item.risk_id, $event)"
                    />
                    @if (remarkInvalid(item.risk_id)) {
                      <p class="field-error">特殊豁免必须填写依据（至少 {{ exemptMin }} 字）</p>
                    }
                  </div>
                } @else if (decisionOf(item.risk_id) === 'CONFIRM_AS_ISSUE') {
                  <p class="mt-2 text-[11px] text-slate-400">确认后会自动转为批注草稿，可再走采纳 / 驳回流程。</p>
                }
              </div>
            </article>
          }

          <div>
            <label class="mb-1 block text-[11px] font-medium text-slate-600" for="overall-remark">
              整体备注（可选，≤{{ overallMax }} 字）
            </label>
            <textarea
              id="overall-remark"
              rows="2"
              class="field-input resize-none text-xs"
              [attr.maxlength]="overallMax"
              placeholder="给后续处理人留一句话…"
              [value]="overallRemark()"
              (input)="onOverallRemark($event)"
            ></textarea>
          </div>
        </div>

        <!-- 底部 -->
        <footer class="shrink-0 border-t border-slate-100 px-6 py-3.5">
          @if (submitError()) {
            <p class="mb-2 text-xs text-danger">{{ submitError() }}</p>
          }
          @if (confirmLate()) {
            <div class="mb-2 flex flex-wrap items-center justify-between gap-2 rounded-lg bg-warning-soft px-3 py-2">
              <p class="text-xs text-slate-700">确认稍后再处理？流程将保持暂停状态。</p>
              <div class="flex gap-2">
                <button type="button" class="text-xs font-medium text-slate-500 hover:text-slate-700" (click)="confirmLate.set(false)">
                  继续处理
                </button>
                <button type="button" class="text-xs font-medium text-warning hover:underline" (click)="dismissed.emit()">
                  稍后处理
                </button>
              </div>
            </div>
          }
          <div class="flex items-center justify-between gap-3">
            <button
              type="button"
              class="text-xs text-slate-400 transition-colors hover:text-slate-600"
              [disabled]="busy()"
              (click)="onEscape()"
            >
              稍后处理
            </button>
            <div class="flex items-center gap-2">
              <span class="text-[11px] text-slate-400">已选 {{ decidedCount() }} / {{ interrupt().items.length }}</span>
              <button type="button" class="btn-primary" [disabled]="!canSubmit() || busy()" (click)="submit()">
                {{ busy() ? '正在恢复流程…' : '提交并继续执行' }}
              </button>
            </div>
          </div>
        </footer>
      </div>
    </div>
  `,
})
export class RiskConfirmModalComponent implements OnInit, OnDestroy {
  readonly interrupt = input.required<InterruptTaskOut>();
  readonly busy = input(false);
  readonly submitError = input<string | null>(null);

  readonly submitted = output<HitlResumeRequest>();
  readonly dismissed = output<void>();
  readonly expired = output<void>();

  protected readonly exemptMin = EXEMPT_REMARK_MIN;
  protected readonly exemptMax = EXEMPT_REMARK_MAX;
  protected readonly overallMax = OVERALL_REMARK_MAX;

  protected readonly decisions = signal<Record<string, HitlDecision>>({});
  protected readonly remarks = signal<Record<string, string>>({});
  protected readonly overallRemark = signal('');
  protected readonly confirmLate = signal(false);
  protected readonly remainingSeconds = signal(0);

  private timer: ReturnType<typeof setInterval> | null = null;
  private expiredEmitted = false;

  protected readonly decidedCount = computed(() => Object.keys(this.decisions()).length);

  protected readonly canSubmit = computed(() => {
    const decisions = this.decisions();
    const remarks = this.remarks();
    return this.interrupt().items.every((item) => {
      const decision = decisions[item.risk_id];
      if (!decision) {
        return false;
      }
      if (decision === 'MARK_EXEMPT') {
        const remark = (remarks[item.risk_id] ?? '').trim();
        return remark.length >= EXEMPT_REMARK_MIN && remark.length <= EXEMPT_REMARK_MAX;
      }
      return true;
    });
  });

  ngOnInit(): void {
    this.startCountdown();
  }

  ngOnDestroy(): void {
    this.stopCountdown();
  }

  /* ---------- 处置 ---------- */

  protected decisionOf(riskId: string): HitlDecision | undefined {
    return this.decisions()[riskId];
  }

  protected setDecision(riskId: string, decision: HitlDecision): void {
    this.decisions.update((current) => ({ ...current, [riskId]: decision }));
  }

  protected remarkOf(riskId: string): string {
    return this.remarks()[riskId] ?? '';
  }

  protected remarkInvalid(riskId: string): boolean {
    if (this.decisionOf(riskId) !== 'MARK_EXEMPT') {
      return false;
    }
    return (this.remarkOf(riskId) ?? '').trim().length < EXEMPT_REMARK_MIN;
  }

  protected setRemark(riskId: string, event: Event): void {
    const value = (event.target as HTMLInputElement).value;
    this.remarks.update((current) => ({ ...current, [riskId]: value }));
  }

  protected onOverallRemark(event: Event): void {
    this.overallRemark.set((event.target as HTMLTextAreaElement).value);
  }

  /* ---------- 提交 ---------- */

  protected submit(): void {
    if (!this.canSubmit() || this.busy()) {
      return;
    }
    const decisions = this.decisions();
    const remarks = this.remarks();
    const itemDecisions: RiskItemDecision[] = this.interrupt().items.map((item) => {
      const decision = decisions[item.risk_id] as HitlDecision;
      const remark = (remarks[item.risk_id] ?? '').trim();
      return {
        risk_id: item.risk_id,
        decision,
        remark: decision === 'MARK_EXEMPT' ? remark : remark || null,
      };
    });

    this.submitted.emit({
      interrupt_id: this.interrupt().id,
      item_decisions: itemDecisions,
      remark: this.overallRemark().trim() || null,
    });
  }

  protected onEscape(): void {
    if (this.busy()) {
      return;
    }
    if (!this.confirmLate()) {
      this.confirmLate.set(true);
      return;
    }
    this.dismissed.emit();
  }

  /* ---------- 倒计时 ---------- */

  protected countdownText(): string {
    const total = Math.max(0, this.remainingSeconds());
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    return [hours, minutes, seconds].map((part) => String(part).padStart(2, '0')).join(':');
  }

  protected levelOf(item: RiskItem): { label: string; text: string; dot: string } {
    return RISK_LEVEL_META[item.level] ?? RISK_LEVEL_META.HIGH;
  }

  protected valueText(value: number | null): string {
    return value === null ? '—' : String(value);
  }

  private startCountdown(): void {
    const interrupt = this.interrupt();
    const fromServer = interrupt.remaining_seconds > 0 ? interrupt.remaining_seconds : null;
    const expiresAt = new Date(interrupt.expires_at).getTime();
    const fromExpiry = Number.isNaN(expiresAt) ? null : Math.floor((expiresAt - Date.now()) / 1000);
    // 绝对时间优先：remaining_seconds 是事件发生时的瞬时值，稍后重新打开弹窗会"续命"
    this.remainingSeconds.set(Math.max(0, fromExpiry ?? fromServer ?? 0));

    this.timer = setInterval(() => {
      this.remainingSeconds.update((value) => Math.max(0, value - 1));
      if (this.remainingSeconds() === 0 && !this.expiredEmitted) {
        this.expiredEmitted = true;
        this.stopCountdown();
        this.expired.emit();
      }
    }, 1000);
  }

  private stopCountdown(): void {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}
