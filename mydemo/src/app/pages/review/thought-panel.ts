import {
  ChangeDetectionStrategy,
  Component,
  effect,
  ElementRef,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { IconComponent } from '../../shared/icon/icon';

export type ThoughtKind = 'system' | 'plan' | 'thought' | 'human' | 'risk' | 'error';

export interface ThoughtLine {
  readonly id: number;
  readonly kind: ThoughtKind;
  /** HH:mm:ss，取自事件 `ts`（后端时钟），不用本地时钟以免和服务端事件错位 */
  readonly time: string;
  readonly text: string;
  readonly detail?: string;
}

export type ThoughtStatus = 'running' | 'waiting' | 'done' | 'failed' | 'idle';

const STATUS_META: Record<ThoughtStatus, { label: string; chip: string }> = {
  running: { label: '运行中', chip: 'bg-primary-soft text-primary' },
  waiting: { label: '已暂停', chip: 'bg-warning-soft text-warning' },
  done: { label: '已完成', chip: 'bg-success-soft text-success' },
  failed: { label: '失败', chip: 'bg-danger-soft text-danger' },
  idle: { label: '未开始', chip: 'bg-slate-100 text-slate-500' },
};

/**
 * 底部「Agent 思考链路」面板（prd-02 §3.1 / §5.3.2）。
 *
 * 行数据由页面持有：面板只负责渲染、折叠与"是否跟随最新"，
 * 这样 run_completed 后页面还能回看整段链路，不会被组件销毁带走。
 */
@Component({
  selector: 'app-thought-panel',
  imports: [IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="flex h-full flex-col overflow-hidden rounded-card bg-white shadow-card">
      <header
        class="flex h-11 shrink-0 items-center gap-2 border-b border-slate-100 px-4"
        [class.border-transparent]="collapsed()"
      >
        <button
          type="button"
          class="flex min-w-0 items-center gap-2 text-left"
          [attr.aria-expanded]="!collapsed()"
          (click)="toggle()"
        >
          <app-icon
            [name]="collapsed() ? 'chevronRight' : 'chevronDown'"
            [size]="14"
            class="text-slate-400"
          />
          <app-icon name="sparkles" [size]="14" class="text-primary" />
          <span class="text-xs font-semibold text-slate-700">Agent 思考链路</span>
        </button>

        <span
          class="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium"
          [class]="statusMeta().chip"
        >
          <span class="h-1.5 w-1.5 rounded-full bg-current" [class.animate-pulse]="status() === 'running'"></span>
          {{ statusMeta().label }}@if (status() === 'running') {<span class="ml-0.5">{{ percent() }}%</span>}
        </span>

        @if (collapsed()) {
          <p class="min-w-0 flex-1 truncate px-1 text-xs text-slate-400">
            {{ summary() || '暂无过程记录' }}
          </p>
        } @else {
          <span class="flex-1"></span>
          <span class="text-[11px] text-slate-400">{{ lines().length }} 行</span>
        }

        <button
          type="button"
          class="text-[11px] font-medium text-slate-400 transition-colors hover:text-slate-700"
          (click)="toggle()"
        >
          {{ collapsed() ? '展开' : '折叠' }}
        </button>
      </header>

      @if (!collapsed()) {
        <div class="relative min-h-0 flex-1">
          <div
            #body
            class="h-full overflow-y-auto px-4 py-2.5 font-mono text-xs leading-6"
            (scroll)="onScroll()"
          >
            @for (line of lines(); track line.id) {
              <div class="flex gap-3">
                <span class="shrink-0 tabular-nums text-slate-300">{{ line.time }}</span>
                @switch (line.kind) {
                  @case ('thought') {
                    <div class="min-w-0 flex-1 border-l-2 border-primary/30 bg-slate-50/80 px-2 py-0.5">
                      <span class="text-primary">Agent：</span>
                      <span class="text-slate-600">{{ line.text }}</span>
                    </div>
                  }
                  @case ('human') {
                    <div class="min-w-0 flex-1">
                      <span class="mr-1 rounded bg-primary-soft px-1 text-[10px] font-semibold text-primary">人工</span>
                      <span class="text-slate-600">{{ line.text }}</span>
                    </div>
                  }
                  @case ('plan') {
                    <div class="min-w-0 flex-1">
                      <span class="text-success">✓</span>
                      <span class="ml-1 text-slate-600">{{ line.text }}</span>
                    </div>
                  }
                  @case ('risk') {
                    <div class="min-w-0 flex-1">
                      <span class="text-warning">⚠</span>
                      <span class="ml-1 text-warning">{{ line.text }}</span>
                    </div>
                  }
                  @case ('error') {
                    <div class="min-w-0 flex-1 text-danger">{{ line.text }}</div>
                  }
                  @default {
                    <div class="min-w-0 flex-1 text-slate-500">{{ line.text }}</div>
                  }
                }
              </div>
              @if (line.detail; as detail) {
                <div class="flex gap-3">
                  <span class="w-[54px] shrink-0"></span>
                  <p class="min-w-0 flex-1 truncate text-[11px] text-slate-400">{{ detail }}</p>
                </div>
              }
            } @empty {
              <p class="py-6 text-center text-slate-400">任务启动后，Agent 的执行过程会实时显示在这里。</p>
            }
          </div>

          @if (!autoScroll() && lines().length > 0) {
            <button
              type="button"
              class="absolute bottom-2 left-1/2 -translate-x-1/2 rounded-full border border-slate-200 bg-white/95 px-2.5 py-1 text-[11px] font-medium text-slate-600 shadow-float backdrop-blur transition-colors hover:text-slate-900"
              (click)="scrollToLatest()"
            >
              回到最新 ↓
            </button>
          }
        </div>
      }
    </section>
  `,
})
export class ThoughtPanelComponent {
  readonly lines = input.required<readonly ThoughtLine[]>();
  readonly collapsed = input(false);
  readonly status = input<ThoughtStatus>('idle');
  readonly percent = input(0);
  readonly summary = input('');

  readonly collapsedChange = output<boolean>();

  protected readonly autoScroll = signal(true);
  private readonly body = viewChild<ElementRef<HTMLDivElement>>('body');

  constructor() {
    effect(() => {
      const total = this.lines().length;
      if (total === 0 || !this.autoScroll()) {
        return;
      }
      // effect 跑在 DOM 更新之前，rAF 才能读到新行撑开后的 scrollHeight
      requestAnimationFrame(() => {
        const element = this.body()?.nativeElement;
        if (element) {
          element.scrollTop = element.scrollHeight;
        }
      });
    });
  }

  protected statusMeta(): { label: string; chip: string } {
    return STATUS_META[this.status()];
  }

  protected toggle(): void {
    this.collapsedChange.emit(!this.collapsed());
  }

  protected onScroll(): void {
    const element = this.body()?.nativeElement;
    if (!element) {
      return;
    }
    const distance = element.scrollHeight - element.scrollTop - element.clientHeight;
    // 用户主动上滚后停止自动跟随，否则每来一条事件都会把阅读位置拽走
    this.autoScroll.set(distance < 24);
  }

  protected scrollToLatest(): void {
    const element = this.body()?.nativeElement;
    if (element) {
      element.scrollTop = element.scrollHeight;
    }
    this.autoScroll.set(true);
  }
}
