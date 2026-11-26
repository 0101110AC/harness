import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { map } from 'rxjs';
import { AuthService } from '../services/auth.service';

/**
 * 页面级权限守卫（prd-05 §4.4）：有权限放行，没有就整页去 403，而不是弹个 Toast 留在原地。
 *
 * 时序说明：子路由守卫在父路由 `authGuard` 的 Observable 解析完成之后才执行，
 * 但"刷新内页"时仍要自己 `ensureSession()` 一次——守卫不应该依赖父级的执行顺序，
 * 否则单测里单独挂这条守卫时 `currentUser()` 还是 null，会把有权限的人挡在门外。
 */
export function permissionGuard(...required: string[]): CanActivateFn {
  return () => {
    const auth = inject(AuthService);
    const router = inject(Router);

    return auth.ensureSession().pipe(
      map((session) => {
        if (session !== 'authenticated') {
          return router.createUrlTree(['/login'], {
            queryParams: { redirect: router.url },
          });
        }
        const allowed = required.every((permission) => auth.hasPermission(permission));
        // 403 页在守卫之外：这里只负责把无权限的人送过去，不再做二次判断
        return allowed ? true : router.createUrlTree(['/403']);
      }),
    );
  };
}
