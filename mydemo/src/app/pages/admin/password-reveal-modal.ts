import { ChangeDetectionStrategy, Component, input, output, signal } from '@angular/core';
import { IconComponent } from '../../shared/icon/icon';

/**
 * 一次性密码弹窗：新建用户 / 重置密码后展示新密码。
 *
 * 密码只在这一次可见（后端不提供查询接口），所以这里必须给出明确的一次性提示，
 * 并把"复制"做成主操作，避免用户截图丢字。
 */
@Component({
  selector: 'app-password-reveal-modal',
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '(document:keydown.escape)': 'closed.emit()',
  },
  template: `
    <div
      class="fixed inset-0 z-[75] flex items-center justify-center bg-slate-950/40 px-4 py-10 backdrop-blur-sm"
      (click)="closed.emit()"
    >
      <div
        class="w-full max-w-sm animate-fade-in rounded-card bg-white p-6 shadow-float"
        role="dialog"
        aria-modal="true"
        aria-labelledby="password-reveal-title"
        (click)="$event.stopPropagation()"
      >
        <div class="flex items-start gap-3">
          <span
            class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary-soft text-primary"
          >
            <app-icon name="lock" [size]="17" />
          </span>
          <div class="min-w-0 flex-1">
            <h2 id="password-reveal-title" class="text-sm font-semibold text-slate-900">
              {{ title() }}
            </h2>
            <p class="mt-1 text-xs leading-5 text-slate-500">
              密码只在此处展示一次，关闭后无法再次查看。
            </p>
          </div>
        </div>

        <dl class="mt-5 space-y-2.5 rounded-lg bg-slate-50 px-3.5 py-3">
          <div class="flex items-center gap-3">
            <dt class="w-12 shrink-0 text-[11px] text-slate-400">账号</dt>
            <dd class="min-w-0 flex-1 truncate text-sm font-medium text-slate-800">
              {{ username() }}
            </dd>
          </div>
          <div class="flex items-center gap-3">
            <dt class="w-12 shrink-0 text-[11px] text-slate-400">密码</dt>
            <dd class="min-w-0 flex-1">
              <code
                class="block select-all break-all rounded-md bg-white px-2 py-1 font-mono text-sm tracking-wide text-slate-900 ring-1 ring-slate-200"
                >{{ password() }}</code
              >
            </dd>
            <button
              type="button"
              class="btn-secondary shrink-0 !px-2.5 !py-1.5 text-[11px]"
              (click)="copy()"
            >
              <app-icon [name]="copied() ? 'check' : 'file'" [size]="13" />
              {{ copied() ? '已复制' : '复制' }}
            </button>
          </div>
        </dl>

        @if (note(); as text) {
          <p class="mt-3 flex items-start gap-1.5 text-[11px] leading-4 text-warning">
            <app-icon name="alert" [size]="13" class="mt-0.5 shrink-0" />
            <span>{{ text }}</span>
          </p>
        }

        <div class="mt-5 flex justify-end">
          <button type="button" class="btn-primary" (click)="closed.emit()">我已记录</button>
        </div>
      </div>
    </div>
  `,
})
export class PasswordRevealModalComponent {
  readonly title = input.required<string>();
  readonly username = input.required<string>();
  readonly password = input.required<string>();
  readonly note = input<string | null>(null);

  readonly closed = output<void>();

  protected readonly copied = signal(false);

  protected copy(): void {
    const value = this.password();
    // 内网 http 部署下 clipboard API 不可用（非安全上下文），必须留 execCommand 兜底
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(value).then(
        () => this.markCopied(),
        () => this.legacyCopy(value),
      );
      return;
    }
    this.legacyCopy(value);
  }

  private markCopied(): void {
    this.copied.set(true);
    setTimeout(() => this.copied.set(false), 2000);
  }

  private legacyCopy(value: string): void {
    const textarea = document.createElement('textarea');
    textarea.value = value;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    try {
      document.execCommand('copy');
      this.markCopied();
    } finally {
      document.body.removeChild(textarea);
    }
  }
}
