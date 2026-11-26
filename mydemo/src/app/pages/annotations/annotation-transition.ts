import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  computed,
  input,
  output,
  signal,
} from '@angular/core';
import { ANNOTATION_STATUS_META } from '../../core/dictionaries';
import { AnnotationOut, TransitionAction } from '../../core/models/annotation.model';
import { ProjectMemberOut } from '../../core/models/project.model';
import { IconComponent } from '../../shared/icon/icon';

/** 弹窗只负责「用户填了什么」，`action` 与 `row_version` 由页面拼（契约在 prd-07 §6.7）。 */
export interface TransitionDraft {
  assignee_id?: string | null;
  version_drawing_id?: string | null;
  reject_reason?: string | null;
  close_remark?: string | null;
}

const REASON_MIN = 10;
const REASON_MAX = 500;
const QUICK_TAGS = ['仍不符合规范', '修改不彻底', '改错位置'] as const;

/**
 * 流转弹窗（prd-03 §5.3）。
 *
 * 三种动作共用一套外壳，但校验完全不同：
 * - 下发：必选责任人；
 * - 复核不通过：原因 10~500 字，附快捷标签；
 * - 标记已修改：若存在更新的图纸版本，问「关联到 vN？」。
 * 「复核通过 / 作废」是确认型，不收集原因（作废的 remark 走 DELETE 查询参数）。
 */
@Component({
  selector: 'app-annotation-transition',
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '(document:keydown.escape)': 'onEscape()',
  },
  template: `
    <div
      class="fixed inset-0 z-[70] flex items-center justify-center bg-slate-950/40 px-4 py-8 backdrop-blur-sm"
      (click)="dismiss()"
    >
      <div
        class="w-full max-w-md animate-fade-in overflow-hidden rounded-card bg-white shadow-float"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="transition-title"
        (click)="$event.stopPropagation()"
      >
        <header class="flex items-start gap-3 border-b border-slate-100 px-5 py-4">
          <span class="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full" [class]="iconTone()">
            <app-icon [name]="iconName()" [size]="16" />
          </span>
          <div class="min-w-0 flex-1">
            <h2 id="transition-title" class="text-sm font-semibold text-slate-900">{{ heading() }}</h2>
            <p class="mt-1 truncate text-xs text-slate-500">
              #{{ annotation().title }}
            </p>
          </div>
          <span class="shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium" [class]="statusMeta().chip">
            {{ statusMeta().label }}
          </span>
        </header>

        <div class="px-5 py-4">
          @switch (action()) {
            @case ('ISSUE') {
              <label class="field-label !mb-1.5 !text-xs" for="transition-assignee">
                责任人 <span class="text-danger">*</span>
              </label>
              <select
                id="transition-assignee"
                class="field-input !py-2 text-xs"
                [class.field-input-invalid]="assigneeMissing()"
                [value]="assigneeId()"
                (change)="onAssignee($event)"
              >
                <option value="" [selected]="!assigneeId()">请选择项目成员</option>
                @for (member of members(); track member.user.id) {
                  <option [value]="member.user.id" [selected]="member.user.id === assigneeId()">{{ member.user.name }}</option>
                }
              </select>
              @if (assigneeMissing()) {
                <p class="field-error">下发批注必须指定责任人</p>
              }
              <p class="mt-2 text-[11px] text-slate-400">
                下发后状态变为「已下发」，责任人会收到站内信。
              </p>
            }

            @case ('REJECT_AND_REISSUE') {
              <label class="field-label !mb-1.5 !text-xs" for="transition-reason">
                复核不通过原因 <span class="text-danger">*</span>
                <span class="font-normal text-slate-400">（{{ reasonMin }}~{{ reasonMax }} 字）</span>
              </label>
              <textarea
                id="transition-reason"
                rows="4"
                class="field-input resize-none !py-2 text-xs"
                [class.field-input-invalid]="reasonInvalid()"
                [attr.maxlength]="reasonMax"
                placeholder="说明哪里还没改到位，责任人会收到站内信"
                [value]="reason()"
                (input)="onReason($event)"
              ></textarea>
              <div class="mt-1.5 flex items-center justify-between">
                <div class="flex flex-wrap gap-1">
                  @for (tag of quickTags; track tag) {
                    <button
                      type="button"
                      class="rounded-md bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600 transition-colors hover:bg-slate-200"
                      (click)="applyTag(tag)"
                    >
                      {{ tag }}
                    </button>
                  }
                </div>
                <span class="text-[11px] tabular-nums text-slate-400">{{ reason().trim().length }}/{{ reasonMax }}</span>
              </div>
              @if (reasonInvalid()) {
                <p class="field-error">原因至少 {{ reasonMin }} 字，太短责任人不知道要改什么</p>
              }
            }

            @case ('MARK_MODIFIED') {
              @if (newerVersion(); as version) {
                <p class="text-xs text-slate-600">
                  当前图纸已是 <span class="font-medium text-slate-900">v{{ version }}</span>，
                  该批注创建于 v{{ annotation().drawing_version }}。要把这次修改关联到新版本吗？
                </p>
                <div class="mt-3 space-y-2">
                  <label class="flex cursor-pointer items-start gap-2 rounded-lg border border-slate-200 p-2.5 text-xs">
                    <input
                      type="radio"
                      class="mt-0.5 accent-primary"
                      name="version-link"
                      [checked]="linkVersion()"
                      (change)="linkVersion.set(true)"
                    />
                    <span>
                      <span class="font-medium text-slate-900">关联到 v{{ version }}（新版本）</span>
                      <span class="mt-0.5 block text-[11px] text-slate-400">时间线会出现「关联到图纸 v{{ version }}」记录</span>
                    </span>
                  </label>
                  <label class="flex cursor-pointer items-start gap-2 rounded-lg border border-slate-200 p-2.5 text-xs">
                    <input
                      type="radio"
                      class="mt-0.5 accent-primary"
                      name="version-link"
                      [checked]="!linkVersion()"
                      (change)="linkVersion.set(false)"
                    />
                    <span>
                      <span class="font-medium text-slate-900">仅标记状态</span>
                      <span class="mt-0.5 block text-[11px] text-slate-400">不建立版本关联，适用于原位修改</span>
                    </span>
                  </label>
                </div>
              } @else {
                <p class="text-xs text-slate-600">
                  确认已按批注要求修改完成？标记后状态变为「已修改待复核」，创建者会收到站内信。
                </p>
              }
            }

            @case ('CLOSE') {
              <label class="field-label !mb-1.5 !text-xs" for="transition-close">
                关闭说明 <span class="font-normal text-slate-400">（可选，≤500 字）</span>
              </label>
              <textarea
                id="transition-close"
                rows="3"
                class="field-input resize-none !py-2 text-xs"
                [attr.maxlength]="reasonMax"
                placeholder="例如：已按 v2 图纸补全疏散宽度"
                [value]="closeRemark()"
                (input)="onCloseRemark($event)"
              ></textarea>
              <p class="mt-2 text-[11px] text-slate-400">关闭后批注进入终态，统计口径按关闭时间计算。</p>
            }

            @default {
              <p class="text-xs text-slate-600">
                作废后该批注不再出现在列表中（软删除，可追溯）。此操作不可撤销。
              </p>
            }
          }

          @if (submitError()) {
            <p class="mt-3 rounded-lg bg-danger-soft px-3 py-2 text-[11px] text-danger">{{ submitError() }}</p>
          }
        </div>

        <footer class="flex items-center justify-end gap-2 border-t border-slate-100 px-5 py-3">
          <button type="button" class="btn-secondary !px-3 !py-1.5 text-xs" [disabled]="busy()" (click)="dismiss()">
            取消
          </button>
          <button
            type="button"
            [class]="confirmClass()"
            [disabled]="busy() || !canSubmit()"
            (click)="submit()"
          >
            {{ busy() ? '处理中…' : confirmLabel() }}
          </button>
        </footer>
      </div>
    </div>
  `,
})
export class AnnotationTransitionComponent implements OnInit {
  readonly action = input.required<TransitionAction>();
  readonly annotation = input.required<AnnotationOut>();
  readonly members = input<readonly ProjectMemberOut[]>([]);
  /** 当前正在查看的图纸 id：`关联到新版` 时写进 `version_drawing_id` */
  readonly drawingId = input<string | null>(null);
  /** 存在更新的图纸版本时传版本号，用于「关联到 vN？」选择框 */
  readonly newerVersion = input<number | null>(null);
  readonly busy = input(false);
  readonly submitError = input<string | null>(null);

  readonly submitted = output<TransitionDraft>();
  readonly dismissed = output<void>();

  protected readonly quickTags = QUICK_TAGS;
  protected readonly reasonMin = REASON_MIN;
  protected readonly reasonMax = REASON_MAX;

  protected readonly assigneeId = signal<string | null>(null);
  protected readonly reason = signal('');
  protected readonly closeRemark = signal('');
  protected readonly linkVersion = signal(true);

  protected readonly statusMeta = computed(() => ANNOTATION_STATUS_META[this.annotation().status]);
  protected readonly assigneeMissing = computed(() => this.action() === 'ISSUE' && !this.assigneeId());
  protected readonly reasonInvalid = computed(
    () =>
      this.action() === 'REJECT_AND_REISSUE' &&
      (this.reason().trim().length < REASON_MIN || this.reason().trim().length > REASON_MAX),
  );

  protected readonly canSubmit = computed(() => {
    if (this.action() === 'ISSUE') {
      return Boolean(this.assigneeId());
    }
    if (this.action() === 'REJECT_AND_REISSUE') {
      return !this.reasonInvalid();
    }
    return true;
  });

  protected readonly heading = computed(() => {
    switch (this.action()) {
      case 'ISSUE':
        return '下发批注';
      case 'MARK_MODIFIED':
        return '标记已修改';
      case 'CLOSE':
        return '复核通过并关闭';
      case 'REJECT_AND_REISSUE':
        return '复核不通过，退回修改';
      default:
        return '作废批注';
    }
  });

  protected readonly confirmLabel = computed(() => {
    switch (this.action()) {
      case 'ISSUE':
        return '确认下发';
      case 'MARK_MODIFIED':
        return '标记已修改';
      case 'CLOSE':
        return '复核通过';
      case 'REJECT_AND_REISSUE':
        return '退回修改';
      default:
        return '确认作废';
    }
  });

  protected readonly iconName = computed<'send' | 'check' | 'refresh' | 'trash'>(() => {
    switch (this.action()) {
      case 'ISSUE':
        return 'send';
      case 'MARK_MODIFIED':
        return 'refresh';
      case 'CLOSE':
        return 'check';
      case 'REJECT_AND_REISSUE':
        return 'refresh';
      default:
        return 'trash';
    }
  });

  protected readonly iconTone = computed(() => {
    switch (this.action()) {
      case 'CLOSE':
        return 'bg-success-soft text-success';
      case 'DELETE':
        return 'bg-danger-soft text-danger';
      case 'REJECT_AND_REISSUE':
        return 'bg-warning-soft text-warning';
      default:
        return 'bg-primary-soft text-primary';
    }
  });

  protected readonly confirmClass = computed(() =>
    this.action() === 'DELETE' ? 'btn-danger !px-3 !py-1.5 text-xs' : 'btn-primary !px-3 !py-1.5 text-xs',
  );

  ngOnInit(): void {
    // 下发默认带上当前责任人（若有），避免每次都要重新选一遍；
    // 但「是否已选」由用户自己确认——空值不会被自动填充成自己。
    this.assigneeId.set(this.annotation().assignee?.id ?? null);
  }

  protected onAssignee(event: Event): void {
    this.assigneeId.set((event.target as HTMLSelectElement).value || null);
  }

  protected onReason(event: Event): void {
    this.reason.set((event.target as HTMLTextAreaElement).value);
  }

  protected onCloseRemark(event: Event): void {
    this.closeRemark.set((event.target as HTMLTextAreaElement).value);
  }

  protected applyTag(tag: string): void {
    const current = this.reason().trim();
    if (current.includes(tag)) {
      return;
    }
    const next = current ? `${current}；${tag}` : tag;
    this.reason.set(next.slice(0, REASON_MAX));
  }

  protected submit(): void {
    if (!this.canSubmit() || this.busy()) {
      return;
    }
    switch (this.action()) {
      case 'ISSUE':
        this.submitted.emit({ assignee_id: this.assigneeId() });
        return;
      case 'REJECT_AND_REISSUE':
        this.submitted.emit({ reject_reason: this.reason().trim() });
        return;
      case 'MARK_MODIFIED':
        this.submitted.emit({
          version_drawing_id: this.linkVersion() ? this.versionDrawingId() : null,
        });
        return;
      case 'CLOSE':
        this.submitted.emit({ close_remark: this.closeRemark().trim() || null });
        return;
      default:
        this.submitted.emit({});
    }
  }

  protected onEscape(): void {
    if (!this.busy()) {
      this.dismissed.emit();
    }
  }

  protected dismiss(): void {
    if (!this.busy()) {
      this.dismissed.emit();
    }
  }

  /** 由父级注入当前图纸 id：关联版本的落点就是它（当前版本 = 新版本）。 */
  private versionDrawingId(): string | null {
    return this.drawingId();
  }
}
