import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  OnDestroy,
  OnInit,
  afterNextRender,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';

/**
 * 单输入框弹窗（重命名这类"只改一个字段"的操作）。
 * 与 ConfirmDialog 一样不持开关状态：父级 `@if` 控制存在性，销毁即重置。
 */
@Component({
  selector: 'app-prompt-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '(document:keydown.escape)': 'dismissed.emit()',
  },
  template: `
    <div
      class="fixed inset-0 z-[70] flex items-center justify-center bg-slate-950/40 px-4 py-10 backdrop-blur-sm"
      (click)="dismissed.emit()"
    >
      <form
        class="w-full max-w-sm animate-fade-in rounded-card bg-white p-6 shadow-float"
        role="dialog"
        aria-modal="true"
        [attr.aria-label]="title()"
        (click)="$event.stopPropagation()"
        (submit)="$event.preventDefault(); submit()"
      >
        <h2 class="text-sm font-semibold text-slate-900">{{ title() }}</h2>
        @if (message(); as text) {
          <p class="mt-1.5 text-sm leading-5 text-slate-500">{{ text }}</p>
        }

        <label class="field-label mt-4" for="prompt-input">{{ label() }}</label>
        <input
          #field
          id="prompt-input"
          type="text"
          class="field-input"
          autocomplete="off"
          [attr.maxlength]="maxlength()"
          [attr.placeholder]="placeholder()"
          [value]="value()"
          (input)="value.set($any($event.target).value)"
        />
        @if (error(); as text) {
          <p class="field-error">{{ text }}</p>
        }

        <div class="mt-6 flex justify-end gap-2">
          <button type="button" class="btn-secondary" [disabled]="busy()" (click)="dismissed.emit()">
            取消
          </button>
          <button type="submit" class="btn-primary" [disabled]="busy()">
            {{ busy() ? '处理中…' : confirmLabel() }}
          </button>
        </div>
      </form>
    </div>
  `,
})
export class PromptDialogComponent implements OnInit, OnDestroy {
  readonly title = input.required<string>();
  readonly message = input<string | null>(null);
  readonly label = input('名称');
  readonly placeholder = input('');
  readonly initialValue = input('');
  readonly maxlength = input(128);
  readonly confirmLabel = input('保存');
  readonly busy = input(false);

  readonly submitted = output<string>();
  readonly dismissed = output<void>();

  protected readonly value = signal('');
  protected readonly error = signal<string | null>(null);

  private readonly field = viewChild<ElementRef<HTMLInputElement>>('field');

  constructor() {
    afterNextRender(() => {
      const input = this.field()?.nativeElement;
      input?.focus();
      input?.select();
    });
  }

  ngOnInit(): void {
    this.value.set(this.initialValue());
    document.body.style.overflow = 'hidden';
  }

  ngOnDestroy(): void {
    document.body.style.overflow = '';
  }

  protected submit(): void {
    const value = this.value().trim();
    if (!value) {
      this.error.set('不能为空');
      return;
    }
    this.error.set(null);
    this.submitted.emit(value);
  }
}
