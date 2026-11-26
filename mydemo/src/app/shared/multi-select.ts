import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';
import { IconComponent } from './icon/icon';

export interface SelectOption {
  readonly value: string;
  readonly label: string;
}

/**
 * 多选下拉（筛选器用）。
 *
 * 展开态归组件自己，选中值归父级（受控）：筛选条件要和地址栏 / 请求参数
 * 保持单一来源，组件不能私存一份副本。
 */
@Component({
  selector: 'app-multi-select',
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    class: 'relative inline-block',
    '(document:click)': 'open.set(false)',
  },
  template: `
    <button
      type="button"
      class="inline-flex h-9 items-center gap-1.5 rounded-lg border px-2.5 text-xs font-medium transition-colors"
      [class.border-primary]="active()"
      [class.bg-primary-soft]="active()"
      [class.text-primary]="active()"
      [class.border-slate-200]="!active()"
      [class.bg-white]="!active()"
      [class.text-slate-600]="!active()"
      [attr.aria-expanded]="open()"
      aria-haspopup="listbox"
      (click)="$event.stopPropagation(); open.set(!open())"
    >
      {{ placeholder() }}
      @if (active()) {
        <span class="rounded bg-white/70 px-1 text-[11px] leading-4">{{ selected().length }}</span>
      }
      <app-icon name="chevronDown" [size]="13" />
    </button>

    @if (open()) {
      <div
        class="absolute left-0 top-full z-30 mt-1.5 w-44 animate-fade-in rounded-lg border border-slate-100 bg-white p-1.5 shadow-float"
        role="listbox"
        aria-multiselectable="true"
        (click)="$event.stopPropagation()"
      >
        <div class="flex items-center justify-between px-1.5 pb-1.5">
          <button type="button" class="text-[11px] font-medium text-primary hover:underline" (click)="selectAll()">
            全选
          </button>
          <button type="button" class="text-[11px] text-slate-400 hover:text-slate-600" (click)="clear()">
            清空
          </button>
        </div>
        @for (option of options(); track option.value) {
          <label
            class="flex cursor-pointer items-center gap-2 rounded-md px-1.5 py-1.5 text-xs text-slate-600 hover:bg-slate-50"
          >
            <input
              type="checkbox"
              class="checkbox"
              name="filter-option"
              [attr.value]="option.value"
              [checked]="isChecked(option.value)"
              (change)="toggle(option.value)"
            />
            {{ option.label }}
          </label>
        }
      </div>
    }
  `,
})
export class MultiSelectComponent {
  readonly options = input.required<readonly SelectOption[]>();
  readonly selected = input.required<readonly string[]>();
  readonly placeholder = input('筛选');

  readonly selectionChange = output<readonly string[]>();

  protected readonly open = signal(false);
  /** 默认全选：只有"筛选生效（少选了）"才高亮，避免全选时满屏蓝色 */
  protected readonly active = computed(
    () => this.selected().length > 0 && this.selected().length < this.options().length,
  );

  protected isChecked(value: string): boolean {
    return this.selected().includes(value);
  }

  protected toggle(value: string): void {
    const next = this.isChecked(value)
      ? this.selected().filter((item) => item !== value)
      : [...this.selected(), value];
    this.selectionChange.emit(next);
  }

  protected selectAll(): void {
    this.selectionChange.emit(this.options().map((option) => option.value));
  }

  protected clear(): void {
    this.selectionChange.emit([]);
  }
}
