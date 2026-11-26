import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { IconComponent } from './icon/icon';

/**
 * 极简二次确认弹窗。删除图纸、重新预审这类不可逆操作统一走它，
 * 组件不持开关状态：父级用 `@if` 控制存在性。
 */
@Component({
  selector: 'app-confirm-dialog',
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '(document:keydown.escape)': 'dismissed.emit()',
  },
  template: `
    <div
      class="fixed inset-0 z-[70] flex items-center justify-center bg-slate-950/40 px-4 py-10 backdrop-blur-sm"
      (click)="dismissed.emit()"
    >
      <div
        class="w-full max-w-sm animate-fade-in rounded-card bg-white p-6 shadow-float"
        role="alertdialog"
        aria-modal="true"
        [attr.aria-label]="title()"
        (click)="$event.stopPropagation()"
      >
        <div class="flex items-start gap-3">
          <span
            class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full"
            [class.bg-danger-soft]="tone() === 'danger'"
            [class.text-danger]="tone() === 'danger'"
            [class.bg-primary-soft]="tone() === 'primary'"
            [class.text-primary]="tone() === 'primary'"
          >
            <app-icon [name]="tone() === 'danger' ? 'alert' : 'info'" [size]="18" />
          </span>
          <div class="min-w-0 flex-1">
            <h2 class="text-sm font-semibold text-slate-900">{{ title() }}</h2>
            <p class="mt-1.5 text-sm leading-5 text-slate-500">{{ message() }}</p>
          </div>
        </div>

        <div class="mt-6 flex justify-end gap-2">
          <button type="button" class="btn-secondary" [disabled]="busy()" (click)="dismissed.emit()">
            取消
          </button>
          <button
            type="button"
            [class]="tone() === 'danger' ? 'btn-danger' : 'btn-primary'"
            [disabled]="busy()"
            (click)="confirmed.emit()"
          >
            {{ busy() ? '处理中…' : confirmLabel() }}
          </button>
        </div>
      </div>
    </div>
  `,
})
export class ConfirmDialogComponent {
  readonly title = input.required<string>();
  readonly message = input.required<string>();
  readonly confirmLabel = input('确认');
  readonly tone = input<'danger' | 'primary'>('primary');
  readonly busy = input(false);

  readonly confirmed = output<void>();
  readonly dismissed = output<void>();
}
