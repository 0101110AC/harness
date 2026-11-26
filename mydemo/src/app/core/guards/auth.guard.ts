import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { map } from 'rxjs';
import { AuthService } from '../services/auth.service';

/**
 * 受保护路由的统一入口：
 * - 已登录 → 放行
 * - 无 token / token 失效 → `/login?redirect={原路径}`
 * - 拿到 403 → `/403` 空状态页（不再只弹 Toast 留在原地）
 */
export const authGuard: CanActivateFn = (_route, state) => {
  const auth = inject(AuthService);
  const router = inject(Router);

  return auth.ensureSession().pipe(
    map((session) => {
      if (session === 'authenticated') {
        return true;
      }
      if (session === 'forbidden') {
        return router.createUrlTree(['/403']);
      }
      return router.createUrlTree(['/login'], {
        queryParams: state.url && state.url !== '/' ? { redirect: state.url } : {},
      });
    }),
  );
};
