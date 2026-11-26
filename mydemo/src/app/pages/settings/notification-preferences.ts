import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import {
  NOTIFICATION_CHANNEL_META,
  NOTIFICATION_EVENT_ROWS,
} from '../../core/dictionaries';
import {
  ChannelPreference,
  NotificationChannel,
  PreferencesOut,
} from '../../core/models/notification.model';
import { NotificationService } from '../../core/services/notification.service';
import { ToastService } from '../../core/services/toast.service';
import { IconComponent } from '../../shared/icon/icon';

/** 后端不可用时的兜底展示：V1.0 三个渠道的既定状态（prd-04 §8） */
const FALLBACK_CHANNELS: readonly ChannelPreference[] = [
  { channel: 'INAPP', enabled: true, available: true, target_masked: null, event_types: [] },
  { channel: 'FEISHU', enabled: false, available: false, target_masked: null, event_types: [] },
  { channel: 'EMAIL', enabled: false, available: false, target_masked: null, event_types: [] },
];

const CHANNEL_ORDER: readonly NotificationChannel[] = ['INAPP', 'FEISHU', 'EMAIL'];

/**
 * 通知偏好（prd-04 §8）。
 *
 * 这一页在 MVP 的价值是**证明架构预留**，不是让用户配置：
 * 所有控件都是禁用态，但禁用必须有明确文案，不能点了没反应。
 */
@Component({
  selector: 'app-notification-preferences',
  imports: [RouterLink, IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="mx-auto flex w-full max-w-3xl flex-col gap-4">
      <header>
        <h1 class="page-title">通知偏好</h1>
        <p class="mt-1 text-xs text-slate-500">
          V1.0 仅开放站内信。飞书与邮件摘要为架构预留，开关禁用属于既定设计。
        </p>
      </header>

      <section class="card overflow-hidden">
        <div class="border-b border-slate-100 px-5 py-3">
          <h2 class="text-sm font-semibold text-slate-900">通知渠道</h2>
          <p class="mt-1 text-[11px] text-slate-400">渠道级开关；事件级订阅见下一区块。</p>
        </div>

        @for (channel of channels(); track channel.channel) {
          <div class="flex items-center gap-3 border-b border-slate-50 px-5 py-3.5 last:border-b-0">
            <span class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg" [class]="toneOf(channel.channel)">
              <app-icon [name]="iconOf(channel.channel)" [size]="17" />
            </span>

            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2">
                <p class="text-sm font-medium text-slate-900">{{ labelOf(channel.channel) }}</p>
                <span class="rounded px-1.5 py-0.5 text-[10px] font-medium" [class]="badgeToneOf(channel.channel)">
                  {{ badgeOf(channel.channel) }}
                </span>
              </div>
              <p class="mt-0.5 text-[11px] text-slate-400">{{ hintOf(channel.channel) }}</p>
            </div>

            <button
              type="button"
              class="relative h-5 w-9 shrink-0 cursor-not-allowed rounded-full transition-colors"
              [class]="channel.enabled ? 'bg-primary' : 'bg-slate-200'"
              [attr.aria-checked]="channel.enabled"
              role="switch"
              [attr.aria-label]="labelOf(channel.channel) + '开关（当前版本不可修改）'"
              [attr.title]="channel.channel === 'INAPP' ? '当前版本必须开启' : '该渠道暂未接入'"
              (click)="onChannelClick(channel.channel)"
            >
              <span
                class="absolute top-0.5 h-4 w-4 rounded-full bg-white shadow-sm transition-[left]"
                [style.left.px]="channel.enabled ? 18 : 2"
              ></span>
            </button>
          </div>
        }

        <div class="flex items-center gap-2 bg-slate-50/60 px-5 py-2.5">
          <app-icon name="lock" [size]="13" class="text-slate-400" />
          <p class="text-[11px] text-slate-400">
            站内信为当前唯一可用渠道，不可关闭；飞书与邮件渠道暂未接入。
          </p>
          <button type="button" class="ml-auto text-[11px] font-medium text-primary hover:underline" (click)="onFeishu()">
            绑定飞书
          </button>
        </div>
      </section>

      <section class="card overflow-hidden">
        <div class="border-b border-slate-100 px-5 py-3">
          <h2 class="text-sm font-semibold text-slate-900">事件级订阅</h2>
          <p class="mt-1 text-[11px] text-slate-400">V1.0 只读展示：四类关键事件均通过站内信送达。</p>
        </div>

        @for (row of eventRows; track row.key) {
          <div class="flex items-center gap-3 border-b border-slate-50 px-5 py-3 last:border-b-0">
            <span class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary-soft text-primary">
              <app-icon name="check" [size]="14" />
            </span>
            <p class="min-w-0 flex-1 text-xs text-slate-700">{{ row.label }}</p>
            <span class="text-[11px] text-slate-400">站内信</span>
            <span class="rounded bg-success-soft px-1.5 py-0.5 text-[10px] font-medium text-success">已订阅</span>
          </div>
        }
      </section>

      <p class="text-[11px] text-slate-400">
        更多设置见
        <a routerLink="/notifications" class="font-medium text-primary hover:underline">消息中心</a>
        ，或直接查看
        <a routerLink="/dashboard" class="font-medium text-primary hover:underline">工作台</a>。
      </p>
    </div>
  `,
})
export class NotificationPreferencesComponent implements OnInit {
  private readonly api = inject(NotificationService);
  private readonly toast = inject(ToastService);

  protected readonly eventRows = NOTIFICATION_EVENT_ROWS;
  protected readonly channels = signal<readonly ChannelPreference[]>(FALLBACK_CHANNELS);

  ngOnInit(): void {
    this.api.preferences().subscribe({
      next: (response: PreferencesOut) => {
        const byChannel = new Map((response?.channels ?? []).map((item) => [item.channel, item]));
        this.channels.set(
          CHANNEL_ORDER.map(
            (channel) =>
              byChannel.get(channel) ?? FALLBACK_CHANNELS.find((item) => item.channel === channel)!,
          ),
        );
      },
      // 偏好是只读展示：接口不可用时用既定状态兜底，不阻断页面
      error: () => undefined,
    });
  }

  protected labelOf(channel: NotificationChannel): string {
    return NOTIFICATION_CHANNEL_META[channel].label;
  }

  protected hintOf(channel: NotificationChannel): string {
    return NOTIFICATION_CHANNEL_META[channel].hint;
  }

  protected badgeOf(channel: NotificationChannel): string {
    return NOTIFICATION_CHANNEL_META[channel].badge;
  }

  protected badgeToneOf(channel: NotificationChannel): string {
    return NOTIFICATION_CHANNEL_META[channel].badgeTone;
  }

  protected iconOf(channel: NotificationChannel): 'bell' | 'mail' | 'send' {
    switch (channel) {
      case 'FEISHU':
        return 'send';
      case 'EMAIL':
        return 'mail';
      default:
        return 'bell';
    }
  }

  protected toneOf(channel: NotificationChannel): string {
    switch (channel) {
      case 'FEISHU':
        return 'bg-primary-soft text-primary';
      case 'EMAIL':
        return 'bg-slate-100 text-slate-500';
      default:
        return 'bg-success-soft text-success';
    }
  }

  /** 禁用开关也要有点击反馈：点了必须说清为什么不能改（prd-04 §8 末段） */
  protected onChannelClick(channel: NotificationChannel): void {
    if (channel === 'INAPP') {
      this.toast.show('当前版本必须开启站内信');
      return;
    }
    this.toast.show('邮件渠道尚未接入，当前仅支持站内信');
  }

  protected onFeishu(): void {
    this.toast.show('飞书渠道尚未接入，当前仅支持站内信');
  }
}
