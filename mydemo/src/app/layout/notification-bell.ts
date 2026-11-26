import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { readErrorMessage } from '../core/api-error';
import { safeDeeplink } from '../core/deeplink';
import { NOTIFICATION_TYPE_META } from '../core/dictionaries';
import { NotificationOut } from '../core/models/notification.model';
import { NotificationService } from '../core/services/notification.service';
import { ToastService } from '../core/services/toast.service';
import { IconComponent } from '../shared/icon/icon';
import { formatRelativeTime } from '../shared/time';

const RECENT_LIMIT = 5;

/**
 * 顶栏铃铛 + 最近 5 条浮层（prd-04 §3）。
 *
 * 未读轮询的所有权在 `NotificationService`（全局单例），这里只负责
 * 展示角标、按需拉 `recent`、以及「点一条 → 标记已读 → 跳 deeplink」。
 */
@Component({
  selector: 'app-notification-bell',
  imports: [RouterLink, IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '(document:keydown.escape)': 'close()',
  },
  template: `
    <div class="relative">
      <button
        type="button"
        class="icon-btn relative"
        aria-label="消息通知"
        aria-haspopup="dialog"
        [attr.aria-expanded]="open()"
        (click)="toggle()"
      >
        <app-icon name="bell" [size]="18" />
        @if (badge(); as text) {
          <span
            class="absolute -right-0.5 -top-0.5 flex h-4 min-w-[16px] items-center justify-center rounded-full bg-danger px-1 text-[10px] font-semibold leading-none text-white ring-2 ring-white"
          >
            {{ text }}
          </span>
        }
      </button>

      @if (open()) {
        <div class="fixed inset-0 z-30" (click)="close()"></div>

        <div
          class="absolute right-0 top-full z-40 mt-2 w-[360px] animate-fade-in overflow-hidden rounded-card border border-slate-100 bg-white shadow-float"
          role="dialog"
          aria-label="通知"
        >
          <header class="flex h-11 items-center gap-2 border-b border-slate-100 px-3">
            <p class="text-sm font-medium text-slate-900">通知</p>
            @if (degraded()) {
              <span class="rounded bg-warning-soft px-1 py-0.5 text-[10px] text-warning" title="连续刷新失败，已暂停轮询">
                刷新已暂停
              </span>
            }
            <button
              type="button"
              class="ml-auto text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:text-slate-300"
              [class]="hasUnread() && !busy() ? 'text-primary hover:text-primary-hover' : 'text-slate-300'"
              [disabled]="!hasUnread() || busy()"
              (click)="readAll()"
            >
              全部已读
            </button>
          </header>

          <div class="max-h-[400px] overflow-y-auto">
            @if (loading()) {
              @for (placeholder of [0, 1, 2]; track placeholder) {
                <div class="flex gap-2 border-b border-slate-50 px-3 py-3">
                  <div class="h-7 w-7 shrink-0 animate-pulse rounded-full bg-slate-100"></div>
                  <div class="flex-1 space-y-1.5">
                    <div class="h-3 w-2/3 animate-pulse rounded bg-slate-100"></div>
                    <div class="h-2.5 w-full animate-pulse rounded bg-slate-100"></div>
                  </div>
                </div>
              }
            } @else if (loadFailed()) {
              <div class="flex flex-col items-center gap-2 px-4 py-8 text-center">
                <app-icon name="alert" [size]="18" class="text-slate-300" />
                <p class="text-xs text-slate-500">通知加载失败</p>
                <button type="button" class="text-action" (click)="loadRecent()">重试</button>
              </div>
            } @else if (recent().length === 0) {
              <p class="px-4 py-8 text-center text-xs text-slate-400">暂无消息</p>
            } @else {
              @for (item of recent(); track item.id) {
                <button
                  type="button"
                  class="flex w-full items-start gap-2.5 border-b border-slate-50 px-3 py-2.5 text-left transition-colors hover:bg-slate-50/70"
                  (click)="openItem(item)"
                >
                  <span
                    class="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full"
                    [class]="metaOf(item).tone"
                  >
                    <app-icon [name]="metaOf(item).icon" [size]="14" />
                  </span>
                  <span class="min-w-0 flex-1">
                    <span class="flex items-center gap-1.5">
                      @if (!item.is_read) {
                        <span class="h-1.5 w-1.5 shrink-0 rounded-full bg-primary"></span>
                      }
                      <span class="truncate text-xs" [class.font-semibold]="!item.is_read" [class.text-slate-900]="!item.is_read" [class.text-slate-600]="item.is_read">
                        {{ item.title }}
                      </span>
                    </span>
                    <span class="mt-0.5 block truncate text-[11px] text-slate-400">{{ item.content }}</span>
                    <span class="mt-0.5 block text-[10px] text-slate-300">{{ relative(item.created_at) }}</span>
                  </span>
                </button>
              }
            }
          </div>

          <footer class="flex h-10 items-center justify-center border-t border-slate-100">
            <a
              routerLink="/notifications"
              class="text-xs font-medium text-primary transition-colors hover:text-primary-hover"
              (click)="close()"
            >
              查看全部
            </a>
          </footer>
        </div>
      }
    </div>
  `,
})
export class NotificationBellComponent implements OnInit, OnDestroy {
  private readonly notifications = inject(NotificationService);
  private readonly router = inject(Router);
  private readonly toast = inject(ToastService);

  protected readonly open = signal(false);
  protected readonly recent = signal<readonly NotificationOut[]>([]);
  protected readonly loading = signal(false);
  protected readonly loadFailed = signal(false);
  protected readonly busy = signal(false);

  protected readonly badge = this.notifications.badgeText;
  protected readonly degraded = this.notifications.degraded;
  protected readonly hasUnread = computed(() => (this.notifications.unreadCount() ?? 0) > 0);

  ngOnInit(): void {
    // 铃铛是全局常驻组件：轮询随它启停，登出后顶栏销毁，轮询自然停
    this.notifications.start();
  }

  ngOnDestroy(): void {
    this.notifications.stop();
  }

  protected toggle(): void {
    if (this.open()) {
      this.close();
      return;
    }
    this.open.set(true);
    // 打开浮层视为一次手动刷新：这也是降级状态的两个出口之一（prd-04 §5）
    this.notifications.retryNow();
    this.loadRecent();
  }

  protected close(): void {
    this.open.set(false);
  }

  protected loadRecent(): void {
    this.loading.set(true);
    this.loadFailed.set(false);
    this.notifications.recent(RECENT_LIMIT).subscribe({
      next: (items) => {
        this.recent.set(items);
        this.loading.set(false);
      },
      error: () => {
        this.loadFailed.set(true);
        this.loading.set(false);
      },
    });
  }

  protected metaOf(item: NotificationOut) {
    return NOTIFICATION_TYPE_META[item.type] ?? NOTIFICATION_TYPE_META.SYSTEM;
  }

  protected relative(iso: string): string {
    return formatRelativeTime(iso);
  }

  protected readAll(): void {
    if (this.busy() || !this.hasUnread()) {
      return;
    }
    this.busy.set(true);
    this.notifications.markAllRead().subscribe({
      next: (response) => {
        this.busy.set(false);
        this.notifications.applyReadAllLocally();
        this.recent.update((items) => items.map((item) => ({ ...item, is_read: true })));
        this.toast.success(`已将 ${response?.updated ?? 0} 条消息标记为已读`);
      },
      error: (error: unknown) => {
        this.busy.set(false);
        this.toast.error(readErrorMessage(error, '操作失败，请重试'));
      },
    });
  }

  protected openItem(item: NotificationOut): void {
    if (!item.is_read) {
      // 失败不阻断跳转：角标由下一轮轮询校正（prd-04 §11）
      this.notifications.markRead(item.id).subscribe({
        next: () => this.notifications.applyReadLocally(),
        error: () => undefined,
      });
      this.recent.update((items) =>
        items.map((row) => (row.id === item.id ? { ...row, is_read: true } : row)),
      );
    }
    this.close();

    const target = safeDeeplink(item.deeplink);
    if (!target) {
      // 非法或缺失 deeplink：降级到消息中心，绝不把消息体里的字符串直接交给 Router
      void this.router.navigate(['/notifications']);
      return;
    }
    void this.router.navigateByUrl(target);
  }
}
