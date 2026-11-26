import { HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { Router } from '@angular/router';
import { catchError, switchMap, throwError } from 'rxjs';
import { API_BASE_URL, RETRIED } from '../api';
import { AuthService } from '../services/auth.service';
import { JwtService } from '../services/jwt.service';

/** 认证入口不携带旧 token，也不参与静默续期，避免自我递归。 */
const AUTH_ENDPOINTS = ['/auth/login', '/auth/register', '/auth/refresh'];

/**
 * 全局认证拦截器。
 *
 * - 注入 `Authorization: Bearer {access_token}`（仅限本站 API，且不含登录/续期接口）。
 * - 401 `TOKEN_EXPIRED`：静默续期并重放原请求，同一请求只重放一次。
 * - 续期失败 / 其他 401：清态并带 `redirect` 回登录页。
 * - 403 `FORBIDDEN`：读请求跳 `/403` 空状态页（PRD §4.4）；写请求不动路由，
 *   把错误交回调用方——提交中被踢走会丢用户刚填的表单。
 */
export const authInterceptor: HttpInterceptorFn = (request, next) => {
  const jwt = inject(JwtService);
  const auth = inject(AuthService);
  const router = inject(Router);

  const isApiRequest = request.url.startsWith(API_BASE_URL);
  const isAuthEndpoint = AUTH_ENDPOINTS.some((path) => request.url.endsWith(path));
  const accessToken = jwt.accessToken;

  const authorizedRequest =
    isApiRequest && !isAuthEndpoint && accessToken
      ? request.clone({ setHeaders: { Authorization: `Bearer ${accessToken}` } })
      : request;

  return next(authorizedRequest).pipe(
    catchError((error: unknown) => {
      if (!(error instanceof HttpErrorResponse)) {
        return throwError(() => error);
      }

      const code = readErrorCode(error);

      if (error.status === 401 && isAuthEndpoint) {
        // 登录失败 / 续期失败由调用方自己翻译文案
        return throwError(() => error);
      }

      if (error.status === 401 && code === 'TOKEN_EXPIRED' && !request.context.get(RETRIED)) {
        return auth.refresh().pipe(
          switchMap((tokens) =>
            next(
              request.clone({
                setHeaders: { Authorization: `Bearer ${tokens.access_token}` },
                context: request.context.set(RETRIED, true),
              }),
            ),
          ),
          catchError((refreshError: unknown) => {
            auth.expireSession();
            return throwError(() => refreshError);
          }),
        );
      }

      if (error.status === 401) {
        auth.expireSession();
        return throwError(() => error);
      }

      // 403 的错误体由调用方展示（表单内联 / 页面空状态），这里只负责"该不该换页"
      if (error.status === 403 && code === 'FORBIDDEN' && request.method === 'GET') {
        void router.navigate(['/403']);
      }

      return throwError(() => error);
    }),
  );
};

function readErrorCode(error: HttpErrorResponse): string {
  const body: unknown = error.error;
  if (typeof body === 'object' && body !== null && 'code' in body) {
    const code = (body as { code?: unknown }).code;
    return typeof code === 'string' ? code : '';
  }
  return '';
}
