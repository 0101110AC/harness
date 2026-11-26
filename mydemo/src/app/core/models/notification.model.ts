/** 与 prd-04 / prd-07 §6.8 对齐的站内信模型。 */

export type NotificationType =
  | 'REVIEW_COMPLETED'
  | 'REVIEW_FAILED'
  | 'ANNOTATION_ASSIGNED'
  | 'ANNOTATION_SUBMITTED'
  | 'ANNOTATION_CLOSED'
  | 'MEMBER_ADDED'
  | 'HITL_PENDING'
  | 'REPORT_READY'
  | 'SYSTEM';

export type NotificationBizType = 'ANNOTATION' | 'REVIEW_TASK' | 'PROJECT' | 'SYSTEM';

/** 消息中心 Tab（prd-04 §6.1）。 */
export type NotificationTab = 'all' | 'unread' | 'system';

export interface NotificationOut {
  id: string;
  type: NotificationType;
  type_text: string;
  title: string;
  content: string;
  biz_type: NotificationBizType;
  biz_id: string | null;
  project_id: string | null;
  /** 站内相对路径；渲染/跳转前必须过 `safeDeeplink()`（防开放重定向） */
  deeplink: string | null;
  is_read: boolean;
  read_at: string | null;
  created_at: string;
}

export interface UnreadCountResponse {
  count: number;
}

export interface ReadAllResponse {
  updated: number;
}

export type NotificationChannel = 'INAPP' | 'FEISHU' | 'EMAIL';

export interface ChannelPreference {
  channel: NotificationChannel;
  enabled: boolean;
  /** V1.0 仅 INAPP 为 true */
  available: boolean;
  target_masked: string | null;
  event_types: string[];
}

export interface PreferencesOut {
  channels: ChannelPreference[];
}

export interface NotificationQuery {
  tab?: NotificationTab;
  type?: NotificationType;
  page?: number;
  pageSize?: number;
}
