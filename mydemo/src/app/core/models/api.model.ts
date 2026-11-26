/**
 * 与 prd-07 §1.2 / §6.2 对齐的接口模型。
 * 字段名保持 snake_case，前后端契约零转换，避免出现第二套命名。
 */

export interface ApiErrorBody {
  code: string;
  message: string;
  detail?: Record<string, unknown> | null;
  trace_id?: string;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: 'bearer';
  expires_in: number;
}

export interface LoginPayload {
  email: string;
  password: string;
  remember_me: boolean;
}

export interface MenuItem {
  key: string;
  title: string;
  path: string | null;
  icon: string | null;
  permission: string | null;
  children: MenuItem[];
}

export interface CurrentUser {
  id: string;
  name: string;
  email: string;
  avatar_url: string | null;
  roles: string[];
  permissions: string[];
  menus: MenuItem[];
}
