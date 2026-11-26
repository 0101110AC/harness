import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { LayoutService } from '../core/services/layout.service';
import { SidebarComponent } from './sidebar';
import { TopbarComponent } from './topbar';

/**
 * 全局框架：固定侧边栏 220px + 顶栏 56px + 内容区（padding 24px，最大宽 1440px 居中）。
 * 侧边栏折叠时主内容区同步左移，过渡时长与侧边栏一致，避免割裂。
 */
@Component({
  selector: 'app-layout',
  imports: [RouterOutlet, SidebarComponent, TopbarComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="min-h-screen bg-canvas">
      <app-sidebar />

      <div
        class="flex min-h-screen flex-col transition-[padding-left] duration-200 ease-standard"
        [style.padding-left.px]="layout.sidebarWidth()"
      >
        <app-topbar />

        <main class="flex-1 px-6 py-6 lg:px-8">
          <div class="mx-auto w-full max-w-content">
            <router-outlet />
          </div>
        </main>
      </div>
    </div>
  `,
})
export class AppLayoutComponent {
  protected readonly layout = inject(LayoutService);
}
