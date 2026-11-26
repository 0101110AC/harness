import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { NgTemplateOutlet } from '@angular/common';
import { NavigationEnd, Router, RouterLink, RouterLinkActive } from '@angular/router';
import { filter, map, startWith } from 'rxjs';
import { MenuItem } from '../core/models/api.model';
import { AuthService } from '../core/services/auth.service';
import { LayoutService } from '../core/services/layout.service';
import { IconComponent, IconName, resolveIconName } from '../shared/icon/icon';
import { isMenuImplemented } from './menu';

/**
 * 左侧导航：菜单来自 `/auth/me` 的 `menus`（后端已按权限过滤）。
 * 两级结构：一级可分组的展开/收起，没有对应路由的模块仅展示。
 */
@Component({
  selector: 'app-sidebar',
  imports: [RouterLink, RouterLinkActive, NgTemplateOutlet, IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <aside
      class="fixed inset-y-0 left-0 z-30 flex flex-col border-r border-slate-200/70 bg-white transition-[width] duration-200 ease-standard"
      [style.width.px]="layout.sidebarWidth()"
    >
      <div class="flex h-topbar shrink-0 items-center gap-2.5 px-4">
        <div
          class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-slate-950 text-white"
        >
          <app-icon name="brand" [size]="17" />
        </div>
        @if (!layout.collapsed()) {
          <div class="min-w-0">
            <p class="truncate text-sm font-semibold tracking-tight text-slate-900">
              Arch Harness
            </p>
            <p class="truncate text-[11px] leading-4 text-slate-400">建筑 AI 预审工作台</p>
          </div>
        }
      </div>

      <nav class="flex-1 space-y-1 overflow-y-auto px-3 py-4" aria-label="主导航">
        @if (!layout.collapsed()) {
          <p class="px-3 pb-2 text-[11px] font-medium uppercase tracking-wider text-slate-400">
            工作空间
          </p>
        }

        @for (menu of menus(); track menu.key) {
          @if (menu.children.length > 0) {
            <button
              type="button"
              class="nav-item w-full"
              [class.text-slate-900]="groupActive(menu)"
              [class.justify-center]="layout.collapsed()"
              [class.px-0]="layout.collapsed()"
              [attr.title]="layout.collapsed() ? menu.title : null"
              [attr.aria-expanded]="isExpanded(menu.key)"
              (click)="toggleGroup(menu.key)"
            >
              <app-icon [name]="iconOf(menu)" [size]="18" />
              @if (!layout.collapsed()) {
                <span class="truncate">{{ menu.title }}</span>
                @if (!available(menu)) {
                  <span
                    class="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium leading-3 text-slate-400"
                  >
                    待开放
                  </span>
                }
                <app-icon
                  name="chevronDown"
                  [size]="14"
                  class="ml-auto text-slate-400 transition-transform duration-200"
                  [class.rotate-180]="!isExpanded(menu.key)"
                />
              }
            </button>

            @if (isExpanded(menu.key) && !layout.collapsed()) {
              <div class="mt-1 space-y-1 pl-4">
                @for (child of menu.children; track child.key) {
                  <ng-container
                    [ngTemplateOutlet]="navItem"
                    [ngTemplateOutletContext]="{ $implicit: child }"
                  />
                }
              </div>
            }
          } @else {
            <ng-container
              [ngTemplateOutlet]="navItem"
              [ngTemplateOutletContext]="{ $implicit: menu }"
            />
          }
        } @empty {
          @if (!layout.collapsed()) {
            <p class="px-3 py-2 text-xs text-slate-400">暂无可访问模块</p>
          }
        }
      </nav>

      <div class="border-t border-slate-100 p-3">
        <button
          type="button"
          class="nav-item w-full"
          [class.justify-center]="layout.collapsed()"
          [class.px-0]="layout.collapsed()"
          [attr.aria-label]="layout.collapsed() ? '展开侧边栏' : '收起侧边栏'"
          [attr.title]="layout.collapsed() ? '展开侧边栏' : null"
          (click)="layout.toggleSidebar()"
        >
          <app-icon
            [name]="layout.collapsed() ? 'expand' : 'collapse'"
            [size]="18"
            class="text-slate-400"
          />
          @if (!layout.collapsed()) {
            <span class="text-slate-500">收起侧边栏</span>
          }
        </button>
      </div>
    </aside>

    <!-- 一级/子级共用一个模板：可用则路由跳转，未实现则置灰 + 提示 -->
    <ng-template #navItem let-menu>
      @if (available(menu)) {
        <a
          [routerLink]="menu.path"
          routerLinkActive="nav-item-active"
          [routerLinkActiveOptions]="{ exact: true }"
          class="nav-item"
          [class.justify-center]="layout.collapsed()"
          [class.px-0]="layout.collapsed()"
          [attr.title]="layout.collapsed() ? menu.title : null"
        >
          <app-icon [name]="iconOf(menu)" [size]="18" />
          @if (!layout.collapsed()) {
            <span class="truncate">{{ menu.title }}</span>
          }
        </a>
      } @else {
        <!-- 没有对应路由的菜单只做展示：不可点、不弹"开发中"Toast -->
        <span
          class="nav-item cursor-not-allowed text-slate-400 hover:bg-transparent hover:text-slate-400"
          aria-disabled="true"
          [class.justify-center]="layout.collapsed()"
          [class.px-0]="layout.collapsed()"
          [attr.title]="menu.title + '（暂未开放）'"
        >
          <app-icon [name]="iconOf(menu)" [size]="18" />
          @if (!layout.collapsed()) {
            <span class="truncate">{{ menu.title }}</span>
            <span
              class="ml-auto rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium leading-3 text-slate-400"
            >
              待开放
            </span>
          }
        </span>
      }
    </ng-template>
  `,
})
export class SidebarComponent {
  protected readonly layout = inject(LayoutService);
  private readonly auth = inject(AuthService);
  private readonly router = inject(Router);

  protected readonly menus = this.auth.menus;

  private readonly currentUrl = toSignal(
    this.router.events.pipe(
      filter((event): event is NavigationEnd => event instanceof NavigationEnd),
      map((event) => event.urlAfterRedirects),
      startWith(this.router.url),
    ),
    { initialValue: this.router.url },
  );

  /** 只记录被用户折叠的分组，默认全部展开。 */
  private readonly collapsedGroups = signal<ReadonlySet<string>>(new Set());

  protected isExpanded(key: string): boolean {
    return !this.collapsedGroups().has(key);
  }

  protected toggleGroup(key: string): void {
    if (this.layout.collapsed()) {
      // 收起态没有子菜单可看，先把侧边栏展开
      this.layout.toggleSidebar();
      return;
    }
    this.collapsedGroups.update((keys) => {
      const next = new Set(keys);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }

  protected groupActive(menu: MenuItem): boolean {
    const url = this.currentUrl();
    return menu.children.some(
      (child) => child.path !== null && (url === child.path || url.startsWith(`${child.path}/`)),
    );
  }

  protected available(menu: MenuItem): boolean {
    return isMenuImplemented(menu);
  }

  protected iconOf(menu: MenuItem): IconName {
    return resolveIconName(menu.icon);
  }
}
