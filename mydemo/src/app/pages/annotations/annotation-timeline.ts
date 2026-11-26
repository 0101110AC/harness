import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';
import { ANNOTATION_STATUS_META, TIMELINE_LOG_META } from '../../core/dictionaries';
import { AnnotationStatus, TimelineItem } from '../../core/models/annotation.model';
import { formatAbsoluteTime, formatRelativeTime } from '../../shared/time';
import { IconComponent } from '../../shared/icon/icon';

const COMMENT_MAX = 1000;

interface TimelineLine {
  readonly id: string;
  readonly icon: ReturnType<typeof iconOf>;
  readonly tone: string;
  readonly text: string;
  readonly detail: string | null;
  readonly time: string;
  readonly absolute: string;
  readonly operator: string;
  /** 状态变更条目的竖线颜色（prd-03 §6.2） */
  readonly line: string;
}

function iconOf(item: TimelineItem) {
  return TIMELINE_LOG_META[item.log_type]?.icon ?? 'annotation';
}

function statusLabel(status: AnnotationStatus | null): string {
  return status ? ANNOTATION_STATUS_META[status].label : '—';
}

/**
 * 时间线 + 评论（prd-03 §6.2 / §6.3）。
 *
 * 时间线是**只读审计流**：状态变更、指派、版本关联、关闭都由后端写日志，
 * 前端不提供编辑入口；唯一可写的是底部评论框。
 * 排序按时间正序（最早在上），最新一条贴着输入框。
 */
@Component({
  selector: 'app-annotation-timeline',
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="flex min-h-0 flex-1 flex-col">
      <div class="min-h-0 flex-1 overflow-y-auto px-3 py-3">
        @if (loading()) {
          @for (placeholder of [0, 1, 2]; track placeholder) {
            <div class="mb-3 flex gap-2">
              <div class="h-6 w-6 shrink-0 animate-pulse rounded-full bg-slate-100"></div>
              <div class="h-3 flex-1 animate-pulse rounded bg-slate-100"></div>
            </div>
          }
        } @else if (loadError()) {
          <p class="py-6 text-center text-xs text-slate-400">时间线加载失败</p>
        } @else if (lines().length === 0) {
          <p class="py-6 text-center text-xs text-slate-400">还没有讨论记录</p>
        } @else {
          <ol class="relative">
            @for (line of lines(); track line.id) {
              <li class="relative flex gap-2.5 pb-3">
                <!-- 状态变更用状态色竖线，形成视觉流向 -->
                <span
                  class="absolute left-[11px] top-6 h-[calc(100%-16px)] w-px"
                  [class]="line.line"
                  aria-hidden="true"
                ></span>
                <span class="relative z-10 flex h-6 w-6 shrink-0 items-center justify-center rounded-full" [class]="line.tone">
                  <app-icon [name]="line.icon" [size]="12" />
                </span>
                <div class="min-w-0 flex-1">
                  <p class="text-[11px] leading-5 text-slate-700">{{ line.text }}</p>
                  @if (line.detail) {
                    <p class="mt-1 whitespace-pre-wrap rounded-lg bg-slate-50 px-2 py-1.5 text-[11px] leading-5 text-slate-600">
                      {{ line.detail }}
                    </p>
                  }
                  <p class="mt-1 text-[10px] text-slate-400" [attr.title]="line.absolute">{{ line.time }}</p>
                </div>
              </li>
            }
          </ol>
        }
      </div>

      <div class="shrink-0 border-t border-slate-100 px-3 py-2.5">
        @if (closed()) {
          <p class="rounded-lg bg-slate-50 px-2.5 py-2 text-[11px] text-slate-500">
            批注已关闭，如需补充请重新打开（V1.0 不提供重新打开入口）。
          </p>
        } @else {
          <div class="rounded-lg border border-slate-200 focus-within:border-primary">
            <textarea
              rows="2"
              class="w-full resize-none rounded-t-lg px-2.5 py-2 text-xs text-slate-900 outline-none placeholder:text-slate-400"
              [attr.maxlength]="commentMax"
              placeholder="回复…（Enter 发送，Shift+Enter 换行）"
              [value]="draft()"
              (input)="onInput($event)"
              (keydown)="onKeydown($event)"
            ></textarea>
            <div class="flex items-center justify-between border-t border-slate-100 px-2 py-1.5">
              <span class="text-[10px] tabular-nums text-slate-400">{{ draft().length }}/{{ commentMax }}</span>
              <button
                type="button"
                class="inline-flex items-center gap-1 rounded-md bg-primary px-2 py-1 text-[11px] font-medium text-white transition-colors hover:bg-primary-hover disabled:opacity-50"
                [disabled]="busy() || !draft().trim()"
                (click)="send()"
              >
                <app-icon name="send" [size]="11" />
                {{ busy() ? '发送中…' : '发送' }}
              </button>
            </div>
          </div>
          @if (submitError()) {
            <p class="mt-1 text-[11px] text-danger">{{ submitError() }}</p>
          }
        }
      </div>
    </div>
  `,
})
export class AnnotationTimelineComponent {
  readonly items = input.required<readonly TimelineItem[]>();
  readonly loading = input(false);
  readonly busy = input(false);
  /** 关闭后评论框禁用（prd-03 §6.3） */
  readonly closed = input(false);
  readonly loadError = input(false);
  readonly submitError = input<string | null>(null);

  readonly submitted = output<string>();

  protected readonly commentMax = COMMENT_MAX;
  protected readonly draft = signal('');

  protected readonly lines = computed<TimelineLine[]>(() =>
    this.items().map((item) => {
      const meta = TIMELINE_LOG_META[item.log_type] ?? TIMELINE_LOG_META.COMMENT;
      const tone = item.log_type === 'STATUS_CHANGED' && item.to_status
        ? ANNOTATION_STATUS_META[item.to_status].chip
        : meta.tone;
      const line = item.log_type === 'STATUS_CHANGED' && item.to_status
        ? ANNOTATION_STATUS_META[item.to_status].line
        : 'bg-slate-100';
      return {
        id: item.id,
        icon: iconOf(item),
        tone,
        line,
        text: this.textOf(item),
        // ASSIGNED / VERSION_LINKED 的目标已经拼进正文，再展示 remark 会重复一遍
        detail:
          item.log_type === 'COMMENT'
            ? item.content
            : item.log_type === 'ASSIGNED' || item.log_type === 'VERSION_LINKED'
              ? null
              : item.remark,
        time: formatRelativeTime(item.created_at),
        absolute: formatAbsoluteTime(item.created_at),
        operator: item.operator?.name ?? '系统',
      };
    }),
  );

  /** 文案模板（prd-03 §6.2）。后端也下发 `log_type_text`，但模板需要拼状态与目标人。 */
  private textOf(item: TimelineItem): string {
    const operator = item.operator?.name ?? '系统';
    switch (item.log_type) {
      case 'CREATED':
        return `${operator} 创建了批注`;
      case 'STATUS_CHANGED':
        return `${operator} 将状态从 ${statusLabel(item.from_status)} 改为 ${statusLabel(item.to_status)}`;
      case 'ASSIGNED':
        return `${operator} 将批注指派给 ${item.remark || '责任人'}`;
      case 'COMMENT':
        return `${operator}：`;
      case 'VERSION_LINKED':
        return `${operator} 关联到图纸 ${item.remark || '新版本'}`;
      case 'CLOSED':
        return `${operator} 关闭了批注`;
      case 'DELETED':
        return `${operator} 作废了批注`;
      default:
        return item.log_type_text || operator;
    }
  }

  protected onInput(event: Event): void {
    this.draft.set((event.target as HTMLTextAreaElement).value.slice(0, COMMENT_MAX));
  }

  /** Enter 发送、Shift+Enter 换行（prd-03 §6.3） */
  protected onKeydown(event: KeyboardEvent): void {
    if (event.key !== 'Enter' || event.shiftKey) {
      return;
    }
    event.preventDefault();
    this.send();
  }

  protected send(): void {
    const content = this.draft().trim();
    if (!content || this.busy()) {
      return;
    }
    this.submitted.emit(content);
  }

  /** 发送成功后由父级清空：失败时保留用户输入（prd-03 §10） */
  clearDraft(): void {
    this.draft.set('');
  }
}
