import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  computed,
  effect,
  inject,
  signal,
  untracked,
  viewChild,
} from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router } from '@angular/router';
import { map } from 'rxjs';
import { readErrorMessage } from '../../core/api-error';
import { safeDeeplink } from '../../core/deeplink';
import { NOTIFICATION_TYPE_META } from '../../core/dictionaries';
import {
  NotificationOut,
  NotificationTab,
} from '../../core/models/notification.model';
import { NotificationService } from '../../core/services/notification.service';
import { ToastService } from '../../core/services/toast.service';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { IconComponent } from '../../shared/icon/icon';
import { formatAbsoluteTime, formatRelativeTime } from '../../shared/time';

const PAGE_SIZE = 20;

const TABS: readonly { key: NotificationTab; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'unread', label: '未读' },
  { key: 'system', label: '系统' },
];

/**
 * 消息中心（prd-04 §6）。
 *
 * 分页用**滚动加载**而不是页码：列表里没有"第几页"的概念，
 * 但分页边界必须显式收敛——否则到底后会无限重复请求最后一页。
 */
@Component({
  selector: 'app-notification-center',
  imports: [IconComponent, ConfirmDialogComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './notification-center.html',
})
export class NotificationCenterComponent {
  private readonly api = inject(NotificationService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);
  private readonly toast = inject(ToastService);
  private readonly destroyRef = inject(DestroyRef);

  protected readonly tabs = TABS;
  protected readonly tab = signal<NotificationTab>('all');
  protected readonly items = signal<readonly NotificationOut[]>([]);
  protected readonly total = signal(0);
  protected readonly loading = signal(true);
  protected readonly loadingMore = signal(false);
  protected readonly loadError = signal(false);
  protected readonly busy = signal(false);
  protected readonly confirmReadAll = signal(false);
  protected readonly unread = this.api.unreadCount;
  protected readonly degraded = this.api.degraded;

  // 分页游标必须是信号：`hasMore` 是 computed，读普通字段只会在首次求值时缓存住
  private readonly page = signal(0);
  private readonly pages = signal(1);
  private loadedIds = new Set<string>();
  private lastAppliedTab: NotificationTab | null = null;
  private observer: IntersectionObserver | null = null;

  private readonly scrollRoot = viewChild<ElementRef<HTMLDivElement>>('scrollRoot');
  private readonly sentinel = viewChild<ElementRef<HTMLDivElement>>('sentinel');

  private readonly queryTab = toSignal(
    this.route.queryParamMap.pipe(map((params) => normalizeTab(params.get('tab')))),
    { initialValue: normalizeTab(this.route.snapshot.queryParamMap.get('tab')) },
  );

  protected readonly hasMore = computed(() => this.page() < this.pages());
  protected readonly unreadTabCount = computed(() => this.unread() ?? 0);

  constructor() {
    // 地址栏是 Tab 的单一来源：刷新、分享、从消息跳回来都能落在同一个 Tab
    effect(() => {
      const tab = this.queryTab();
      untracked(() => this.applyTab(tab));
    });

    // 哨兵进入视口就加载下一页：用 IntersectionObserver 而不是 scroll 事件做数学
    effect(() => {
      const root = this.scrollRoot()?.nativeElement;
      const sentinel = this.sentinel()?.nativeElement;
      this.observer?.disconnect();
      if (!root || !sentinel) {
        return;
      }
      this.observer = new IntersectionObserver(
        (entries) => {
          if (entries.some((entry) => entry.isIntersecting)) {
            this.loadMore();
          }
        },
        { root, rootMargin: '160px' },
      );
      this.observer.observe(sentinel);
    });

    this.destroyRef.onDestroy(() => this.observer?.disconnect());
  }

  /* ---------- Tab ---------- */

  protected setTab(tab: NotificationTab): void {
    if (this.tab() === tab) {
      return;
    }
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { tab },
      replaceUrl: true,
    });
  }

  private applyTab(tab: NotificationTab): void {
    if (this.lastAppliedTab === tab) {
      return;
    }
    this.lastAppliedTab = tab;
    this.tab.set(tab);
    this.reload();
  }

  /* ---------- 数据 ---------- */

  protected reload(): void {
    this.page.set(0);
    this.pages.set(1);
    this.loadedIds = new Set<string>();
    this.items.set([]);
    this.total.set(0);
    this.loadError.set(false);
    this.loading.set(true);
    this.loadNext();
  }

  private loadNext(): void {
    if (this.loadingMore()) {
      return;
    }
    const nextPage = this.page() + 1;
    if (nextPage > this.pages()) {
      return;
    }
    this.loadingMore.set(true);

    this.api
      .list({ tab: this.tab(), page: nextPage, pageSize: PAGE_SIZE })
      .subscribe({
        next: (result) => {
          this.loading.set(false);
          this.loadingMore.set(false);
          this.page.set(result.page || nextPage);
          this.total.set(result.total ?? 0);
          this.pages.set(Math.max(1, result.pages ?? 1));

          // 去重：轮询/已读操作可能让同一条消息在相邻页重复出现
          const fresh = (result.items ?? []).filter((item) => !this.loadedIds.has(item.id));
          for (const item of fresh) {
            this.loadedIds.add(item.id);
          }
          this.items.update((list) => [...list, ...fresh]);

          // 边界收敛：返回空页 / 不足一页 / 已到最后一页 → 不再请求（到底显示"没有更多了"）
          if (fresh.length === 0 || (result.items?.length ?? 0) < PAGE_SIZE) {
            this.pages.set(this.page());
          }
        },
        error: () => {
          this.loading.set(false);
          this.loadingMore.set(false);
          this.loadError.set(true);
        },
      });
  }

  protected loadMore(): void {
    if (this.loading() || this.loadingMore() || !this.hasMore()) {
      return;
    }
    this.loadNext();
  }

  /* ---------- 已读 ---------- */

  protected readOne(item: NotificationOut, event: Event): void {
    event.stopPropagation();
    if (item.is_read) {
      return;
    }
    this.markRead(item);
  }

  protected openItem(item: NotificationOut): void {
    if (!item.is_read) {
      this.markRead(item, true);
    }
    const target = safeDeeplink(item.deeplink);
    if (!target) {
      // 非法 deeplink 降级：留在消息中心，不让消息体决定跳到哪儿（prd-04 §7）
      this.toast.show('该消息缺少有效跳转地址，已留在消息中心');
      return;
    }
    void this.router.navigateByUrl(target);
  }

  private markRead(item: NotificationOut, navigate = false): void {
    const applyLocally = (): void => {
      this.items.update((list) =>
        list.map((row) => (row.id === item.id ? { ...row, is_read: true } : row)),
      );
      this.api.applyReadLocally();
      if (this.tab() === 'unread') {
        // 未读 Tab 下已读行要立刻消失，否则用户会以为没点上
        this.items.update((list) => list.filter((row) => row.id !== item.id));
      }
    };

    this.api.markRead(item.id).subscribe({
      next: applyLocally,
      error: () => {
        // 接口失败不阻断跳转：本地先标记，角标由下一轮轮询校正
        applyLocally();
        if (!navigate) {
          this.toast.error('标记已读失败，稍后会自动校正');
        }
      },
    });
  }

  protected confirmReadAllOpen(): void {
    if ((this.unread() ?? 0) <= 0) {
      return;
    }
    this.confirmReadAll.set(true);
  }

  protected readAll(): void {
    if (this.busy()) {
      return;
    }
    this.busy.set(true);
    this.api.markAllRead().subscribe({
      next: (response) => {
        this.busy.set(false);
        this.confirmReadAll.set(false);
        this.api.applyReadAllLocally();
        this.toast.success(`已将 ${response?.updated ?? 0} 条消息标记为已读`);
        this.reload();
      },
      error: (error: unknown) => {
        this.busy.set(false);
        this.confirmReadAll.set(false);
        this.toast.error(readErrorMessage(error, '操作失败，请重试'));
      },
    });
  }

  /* ---------- 展示 ---------- */

  protected metaOf(item: NotificationOut) {
    return NOTIFICATION_TYPE_META[item.type] ?? NOTIFICATION_TYPE_META.SYSTEM;
  }

  protected relative(iso: string): string {
    return formatRelativeTime(iso);
  }

  protected absolute(iso: string): string {
    return formatAbsoluteTime(iso);
  }

  protected emptyText(): string {
    switch (this.tab()) {
      case 'unread':
        return '全部已读，干得漂亮';
      case 'system':
        return '暂无系统消息';
      default:
        return '暂无消息';
    }
  }
}

function normalizeTab(raw: string | null): NotificationTab {
  return raw === 'unread' || raw === 'system' ? raw : 'all';
}
