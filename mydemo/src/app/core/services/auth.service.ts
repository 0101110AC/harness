import { computed, inject, Injectable, signal } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Router } from '@angular/router';
import {
  catchError,
  finalize,
  map,
  Observable,
  of,
  shareReplay,
  switchMap,
  tap,
  throwError,
} from 'rxjs';
import { API_BASE_URL } from '../api';
import { CurrentUser, LoginPayload, MenuItem, TokenResponse } from '../models/api.model';
import { JwtService } from './jwt.service';

export type SessionState = 'authenticated' | 'anonymous' | 'forbidden';

/**
 * 全局登录态。状态用 Signals 表达，模板直接读 `isLoggedIn()`，
 * 不需要手动 markForCheck（项目为 zoneless 变更检测）。
 */
@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly http = inject(HttpClient);
  private readonly router = inject(Router);
  private readonly jwt = inject(JwtService);

  private readonly userState = signal<CurrentUser | null>(null);
  /** 同一时刻只允许一个 refresh 在飞，其余请求共享结果（PRD §4.4 静默续期）。 */
  private refreshRequest: Observable<TokenResponse> | null = null;

  readonly currentUser = this.userState.asReadonly();
  readonly isLoggedIn = computed(() => this.userState() !== null);
  readonly permissions = computed<readonly string[]>(() => this.userState()?.permissions ?? []);
  /** 后端已按权限过滤过的菜单树（prd-01 §3），侧边栏与面包屑共用同一来源。 */
  readonly menus = computed<readonly MenuItem[]>(() => this.userState()?.menus ?? []);
  readonly initials = computed(() => {
    const name = this.userState()?.name.trim();
    return name ? Array.from(name)[0].toUpperCase() : '未';
  });

  constructor() {
    this.listenForCrossTabLogout();
  }

  /**
   * 登录：先落 token，再拉 `/auth/me` 拿权限与菜单。
   * 只有两步都成功才算登录成功，避免带着空权限进内页。
   */
  login(email: string, password: string, rememberMe: boolean): Observable<void> {
    const payload: LoginPayload = { email: email.trim(), password, remember_me: rememberMe };

    return this.http.post<TokenResponse>(`${API_BASE_URL}/auth/login`, payload).pipe(
      switchMap((tokens) => {
        this.jwt.save(tokens, rememberMe);
        return this.fetchMe();
      }),
      map(() => void 0),
      // 拿到 token 但 /auth/me 失败（权限接口异常）时回滚，避免留下"有 token 没用户"的半登录态
      catchError((error: unknown) => {
        this.clearLocalSession();
        return throwError(() => error);
      }),
    );
  }

  getMe(): Observable<CurrentUser> {
    return this.fetchMe();
  }

  /** 守卫需要区分三种结局：已登录 / 未登录 / 有权登录但无权限渲染该模块。 */
  ensureSession(): Observable<SessionState> {
    if (this.userState()) {
      return of('authenticated');
    }
    if (!this.jwt.accessToken) {
      return of('anonymous');
    }
    return this.fetchMe().pipe(
      map((): SessionState => 'authenticated'),
      catchError((error: unknown) =>
        of<SessionState>(
          error instanceof HttpErrorResponse && error.status === 403 ? 'forbidden' : 'anonymous',
        ),
      ),
    );
  }

  refresh(): Observable<TokenResponse> {
    if (this.refreshRequest) {
      return this.refreshRequest;
    }

    const refreshToken = this.jwt.refreshToken;
    if (!refreshToken) {
      return throwError(() => new Error('NO_REFRESH_TOKEN'));
    }

    const rememberMe = this.jwt.isPersistent;
    this.refreshRequest = this.http
      .post<TokenResponse>(`${API_BASE_URL}/auth/refresh`, { refresh_token: refreshToken })
      .pipe(
        tap((tokens) => this.jwt.save(tokens, rememberMe)),
        finalize(() => {
          this.refreshRequest = null;
        }),
        shareReplay({ bufferSize: 1, refCount: false }),
      );

    return this.refreshRequest;
  }

  /**
   * 登录态彻底失效：清本地态并带 `redirect` 回登录页，登录后回到原页面。
   *
   * 取地址栏而不是 `router.url`：首屏导航（刷新内页）时 router 还没走完，
   * `router.url` 仍是 `/`，会丢掉 redirect。
   */
  expireSession(): void {
    this.clearLocalSession();

    const current = `${window.location.pathname}${window.location.search}`;
    const shouldRedirectBack = current !== '/' && !current.startsWith('/login');
    void this.router.navigate(['/login'], {
      queryParams: shouldRedirectBack ? { redirect: current } : {},
      replaceUrl: true,
    });
  }

  /** 主动登出：先通知后端吊销 refresh token，无论成败都清本地态。 */
  logout(): void {
    const refreshToken = this.jwt.refreshToken;
    const finish = (): void => {
      this.clearLocalSession();
      void this.router.navigateByUrl('/login', { replaceUrl: true });
    };

    if (!refreshToken) {
      finish();
      return;
    }

    this.http
      .post(`${API_BASE_URL}/auth/logout`, { refresh_token: refreshToken })
      .pipe(finalize(finish))
      .subscribe({ error: () => undefined });
  }

  hasPermission(permission: string): boolean {
    const codes = this.permissions();
    return codes.includes('*') || codes.includes(permission);
  }

  private fetchMe(): Observable<CurrentUser> {
    return this.http
      .get<CurrentUser>(`${API_BASE_URL}/auth/me`)
      .pipe(tap((user) => this.userState.set(user)));
  }

  private clearLocalSession(): void {
    this.jwt.clear();
    this.userState.set(null);
  }

  /** 多标签页登出同步（PRD §4.4）：任一标签页清空 token，其余标签页回到登录页。 */
  private listenForCrossTabLogout(): void {
    if (typeof window === 'undefined') {
      return;
    }

    window.addEventListener('storage', (event) => {
      if (event.key !== JwtService.ACCESS_TOKEN_KEY || event.newValue) {
        return;
      }
      this.userState.set(null);
      if (!this.router.url.startsWith('/login')) {
        void this.router.navigate(['/login'], { replaceUrl: true });
      }
    });
  }
}
