import { computed, Injectable, signal } from '@angular/core';

const COLLAPSED_KEY = 'arch.sidebar.collapsed';
const EXPANDED_WIDTH = 220;
const COLLAPSED_WIDTH = 64;

/** 侧边栏折叠状态：220px ↔ 64px，状态持久化到 localStorage（PRD §3）。 */
@Injectable({ providedIn: 'root' })
export class LayoutService {
  private readonly collapsedState = signal(this.readCollapsed());

  readonly collapsed = this.collapsedState.asReadonly();
  readonly sidebarWidth = computed(() =>
    this.collapsedState() ? COLLAPSED_WIDTH : EXPANDED_WIDTH,
  );

  toggleSidebar(): void {
    this.collapsedState.update((collapsed) => !collapsed);
    try {
      localStorage.setItem(COLLAPSED_KEY, this.collapsedState() ? '1' : '0');
    } catch {
      // 隐私模式下仅内存生效
    }
  }

  private readCollapsed(): boolean {
    try {
      return localStorage.getItem(COLLAPSED_KEY) === '1';
    } catch {
      return false;
    }
  }
}
