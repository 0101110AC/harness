import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { ToastKind, ToastService } from '../../core/services/toast.service';
import { IconComponent, IconName } from '../icon/icon';

const ICON_BY_KIND: Record<ToastKind, IconName> = {
  error: 'alert',
  success: 'check',
  info: 'info',
};

const COLOR_BY_KIND: Record<ToastKind, string> = {
  error: 'text-danger',
  success: 'text-success',
  info: 'text-primary',
};

/** 全局 Toast 出口，挂在根组件，任何页面都能收到。 */
@Component({
  selector: 'app-toast-host',
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div
      class="pointer-events-none fixed left-1/2 top-4 z-[60] flex w-full max-w-sm -translate-x-1/2 flex-col gap-2 px-4"
      aria-live="polite"
    >
      @for (toast of toastService.toasts(); track toast.id) {
        <div
          class="pointer-events-auto flex animate-toast-in items-start gap-2.5 rounded-card border border-slate-100 bg-white px-3.5 py-3 shadow-float"
        >
          <span class="mt-0.5 flex" [class]="colorOf(toast.kind)">
            <app-icon [name]="iconOf(toast.kind)" [size]="16" />
          </span>
          <p class="flex-1 text-sm leading-5 text-slate-700">{{ toast.message }}</p>
          <button
            type="button"
            class="-mr-1 -mt-0.5 inline-flex h-6 w-6 items-center justify-center rounded-md text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-600"
            aria-label="关闭提示"
            (click)="toastService.dismiss(toast.id)"
          >
            <app-icon name="close" [size]="14" />
          </button>
        </div>
      }
    </div>
  `,
})
export class ToastHostComponent {
  protected readonly toastService = inject(ToastService);

  protected iconOf(kind: ToastKind): IconName {
    return ICON_BY_KIND[kind];
  }

  protected colorOf(kind: ToastKind): string {
    return COLOR_BY_KIND[kind];
  }
}
