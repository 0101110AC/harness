import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterLink } from '@angular/router';
import { filter, map, startWith } from 'rxjs';
import { AuthService } from '../core/services/auth.service';
import { IconComponent } from '../shared/icon/icon';
import { NotificationBellComponent } from './notification-bell';
import { findMenuTitle } from './menu';

/** 顶部栏 56px：面包屑 + 全局搜索 + 消息铃铛 + 用户菜单。 */
@Component({
  selector: 'app-topbar',
  imports: [IconComponent, RouterLink, NotificationBellComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <header
      class="sticky top-0 z-20 flex h-topbar items-center gap-3 border-b border-slate-200/70 bg-white/85 px-6 backdrop-blur"
    >
      <!-- 面包屑：首页 + 当前菜单标题（后端菜单驱动，最长前缀匹配） -->
      <nav class="flex min-w-0 items-center gap-2 text-sm" aria-label="面包屑">
        <a routerLink="/dashboard" class="shrink-0 text-slate-400 transition-colors hover:text-slate-700">
          首页
        </a>
        <app-icon name="chevronRight" [size]="14" class="text-slate-300" />
        <span class="truncate font-medium text-slate-900">{{ breadcrumb() }}</span>
      </nav>

      <!-- 全局搜索 -->
      <div
        class="ml-auto hidden w-64 items-center gap-2 rounded-lg border border-transparent bg-slate-100/80 px-3 py-2 transition-colors focus-within:border-primary/40 focus-within:bg-white md:flex"
      >
        <app-icon name="search" [size]="16" class="text-slate-400" />
        <input
          type="search"
          class="w-full bg-transparent text-sm text-slate-900 outline-none placeholder:text-slate-400"
          placeholder="搜索项目、图纸、批注"
          aria-label="全局搜索"
        />
      </div>

      <!-- 消息铃铛：接入 GET /notifications/unread-count（30s 轮询 + 切回前台刷新） -->
      <app-notification-bell class="ml-auto md:ml-0" />

      <!-- 用户菜单 -->
      <div class="relative">
        <button
          type="button"
          class="flex items-center gap-2 rounded-lg py-1 pl-1 pr-2 transition-colors hover:bg-slate-100"
          [attr.aria-expanded]="menuOpen()"
          aria-haspopup="menu"
          (click)="menuOpen.set(!menuOpen())"
        >
          <span
            class="flex h-8 w-8 items-center justify-center rounded-full bg-slate-900 text-xs font-semibold text-white"
          >
            {{ auth.initials() }}
          </span>
          <span class="hidden max-w-[120px] truncate text-sm font-medium text-slate-700 md:block">
            {{ auth.currentUser()?.name ?? '未登录' }}
          </span>
          <app-icon name="chevronDown" [size]="14" class="text-slate-400" />
        </button>

        @if (menuOpen()) {
          <div class="fixed inset-0 z-30" (click)="menuOpen.set(false)"></div>
          <div
            class="absolute right-0 top-full z-40 mt-2 w-60 animate-fade-in overflow-hidden rounded-card border border-slate-100 bg-white p-1.5 shadow-float"
            role="menu"
          >
            <div class="px-3 py-2.5">
              <p class="truncate text-sm font-medium text-slate-900">
                {{ auth.currentUser()?.name ?? '未登录' }}
              </p>
              <p class="truncate text-xs text-slate-400">
                {{ auth.currentUser()?.email ?? '—' }}
              </p>
            </div>
            <div class="divider my-1"></div>
            <button
              type="button"
              class="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-sm text-danger transition-colors hover:bg-danger-soft"
              role="menuitem"
              (click)="logout()"
            >
              <app-icon name="logout" [size]="16" />
              退出登录
            </button>
          </div>
        }
      </div>
    </header>
  `,
})
export class TopbarComponent {
  protected readonly auth = inject(AuthService);
  private readonly router = inject(Router);

  protected readonly menuOpen = signal(false);

  private readonly currentUrl = toSignal(
    this.router.events.pipe(
      filter((event): event is NavigationEnd => event instanceof NavigationEnd),
      map((event) => event.urlAfterRedirects),
      startWith(this.router.url),
    ),
    { initialValue: this.router.url },
  );

  protected readonly breadcrumb = computed(
    () => findMenuTitle(this.auth.menus(), this.currentUrl()) ?? '概览',
  );

  protected logout(): void {
    this.menuOpen.set(false);
    this.auth.logout();
  }
}
