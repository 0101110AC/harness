import { Injectable, signal } from '@angular/core';

export type ToastKind = 'error' | 'success' | 'info';

export interface Toast {
  readonly id: number;
  readonly kind: ToastKind;
  readonly message: string;
}

/**
 * 全局轻提示。拦截器（403 无权限）与页面共用同一个队列，
 * 同一时刻最多展示 3 条，避免错误风暴糊住界面。
 */
@Injectable({ providedIn: 'root' })
export class ToastService {
  private readonly items = signal<readonly Toast[]>([]);
  private readonly timers = new Map<number, ReturnType<typeof setTimeout>>();
  private sequence = 0;

  readonly toasts = this.items.asReadonly();

  show(message: string, kind: ToastKind = 'info', durationMs = 3200): number {
    // 同一时刻重复的同文案提示直接吞掉：并发请求失败时常见"三条一模一样的错误"
    const duplicated = this.items().some((item) => item.kind === kind && item.message === message);
    if (duplicated) {
      return 0;
    }
    const id = ++this.sequence;
    this.items.update((list) => [...list, { id, kind, message }].slice(-3));

    if (durationMs > 0) {
      this.timers.set(
        id,
        setTimeout(() => this.dismiss(id), durationMs),
      );
    }
    return id;
  }

  error(message: string): number {
    return this.show(message, 'error', 3600);
  }

  success(message: string): number {
    return this.show(message, 'success');
  }

  dismiss(id: number): void {
    const timer = this.timers.get(id);
    if (timer !== undefined) {
      clearTimeout(timer);
      this.timers.delete(id);
    }
    this.items.update((list) => list.filter((toast) => toast.id !== id));
  }
}
