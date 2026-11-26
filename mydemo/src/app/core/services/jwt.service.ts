import { Injectable } from '@angular/core';
import { TokenResponse } from '../models/api.model';

const ACCESS_TOKEN_KEY = 'arch.access_token';
const REFRESH_TOKEN_KEY = 'arch.refresh_token';

/**
 * Token 存取。
 *
 * - 勾选"记住我"：落 localStorage，关浏览器仍保持 7 天登录（US-01-1）。
 * - 未勾选：落 sessionStorage，标签页关闭即失效。
 * - 同一时刻只保留一份 token，切换存储时清空另一边，避免读到过期副本。
 */
@Injectable({ providedIn: 'root' })
export class JwtService {
  static readonly ACCESS_TOKEN_KEY = ACCESS_TOKEN_KEY;
  static readonly REFRESH_TOKEN_KEY = REFRESH_TOKEN_KEY;

  get accessToken(): string | null {
    return this.read(ACCESS_TOKEN_KEY);
  }

  get refreshToken(): string | null {
    return this.read(REFRESH_TOKEN_KEY);
  }

  /** token 当前是否落在持久化 storage，续期后写回同一存储。 */
  get isPersistent(): boolean {
    return this.safeGet(localStorage, ACCESS_TOKEN_KEY) !== null;
  }

  save(tokens: TokenResponse, rememberMe: boolean): void {
    const target = rememberMe ? localStorage : sessionStorage;
    const other = rememberMe ? sessionStorage : localStorage;

    this.safeSet(target, ACCESS_TOKEN_KEY, tokens.access_token);
    this.safeSet(target, REFRESH_TOKEN_KEY, tokens.refresh_token);
    this.remove(other, ACCESS_TOKEN_KEY);
    this.remove(other, REFRESH_TOKEN_KEY);
  }

  updateAccessToken(accessToken: string): void {
    for (const storage of [localStorage, sessionStorage]) {
      if (this.safeGet(storage, REFRESH_TOKEN_KEY) !== null) {
        this.safeSet(storage, ACCESS_TOKEN_KEY, accessToken);
      }
    }
  }

  clear(): void {
    for (const storage of [localStorage, sessionStorage]) {
      this.remove(storage, ACCESS_TOKEN_KEY);
      this.remove(storage, REFRESH_TOKEN_KEY);
    }
  }

  private read(key: string): string | null {
    return this.safeGet(localStorage, key) ?? this.safeGet(sessionStorage, key);
  }

  private safeGet(storage: Storage, key: string): string | null {
    try {
      return storage.getItem(key);
    } catch {
      return null;
    }
  }

  private safeSet(storage: Storage, key: string, value: string): void {
    try {
      storage.setItem(key, value);
    } catch {
      // 隐私模式 / 配额超限：降级为内存态登录，不阻断流程
    }
  }

  private remove(storage: Storage, key: string): void {
    try {
      storage.removeItem(key);
    } catch {
      // 忽略
    }
  }
}
