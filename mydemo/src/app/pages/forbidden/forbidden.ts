import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink } from '@angular/router';
import { IconComponent } from '../../shared/icon/icon';

/**
 * 403 空状态页。挂在守卫之外：守卫判定无权限后跳这里，
 * 若把 /403 也放进受保护区域，就会 403 → 守卫 → 403 无限循环。
 */
@Component({
  selector: 'app-forbidden',
  imports: [RouterLink, IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="flex min-h-screen flex-col items-center justify-center bg-canvas px-5 py-12">
      <div
        class="w-full max-w-md rounded-card border border-slate-100 bg-white p-10 text-center shadow-card"
      >
        <span
          class="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-danger-soft text-danger"
        >
          <app-icon name="lock" [size]="20" />
        </span>
        <h1 class="mt-6 text-lg font-semibold tracking-tight text-slate-900">无权限访问该资源</h1>
        <p class="mt-2 text-sm leading-6 text-slate-500">
          当前账号没有该页面的访问权限。如需开通，请联系系统管理员。
        </p>
        <a routerLink="/dashboard" class="btn-primary mt-8 w-full">
          <app-icon name="arrowLeft" [size]="16" />
          返回首页
        </a>
      </div>
      <p class="mt-6 text-[11px] text-slate-400">Arch Harness · 建筑 AI 预审工作台</p>
    </div>
  `,
})
export class ForbiddenComponent {}
