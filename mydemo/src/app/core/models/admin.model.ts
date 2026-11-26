/**
 * 系统管理模型（prd-05 §5~§8 / prd-07 §6.11）。
 * 状态、角色、审计动作的中文一律由后端下发，前端不再自建一套映射。
 */

import { UserBrief } from './project.model';

export type UserStatus = 'ACTIVE' | 'DISABLED';

/** 全局角色码（与数据库 `roles.code` 一致）。 */
export type GlobalRole = 'ADMIN' | 'REVIEWER' | 'DESIGNER' | 'VIEWER';

export interface AdminUserOut {
  id: string;
  name: string;
  email: string;
  avatar_url: string | null;
  roles: GlobalRole[];
  role_names: string[];
  status: UserStatus;
  status_text: string;
  last_login_at: string | null;
  created_at: string;
  project_count: number;
}

/** 新建用户响应：初始密码只在创建时返回一次。 */
export interface AdminUserCreated extends AdminUserOut {
  initial_password: string;
}

export interface UserCreateRequest {
  name: string;
  email: string;
  /** 留空则由后端生成强随机密码（secrets），避免前端自己造弱密码 */
  password?: string;
  role_codes: GlobalRole[];
  status: UserStatus;
}

export interface UserUpdateRequest {
  name?: string;
  email?: string;
  role_codes?: GlobalRole[];
  status?: UserStatus;
}

/** 重置密码响应：新密码只在响应里出现一次，前端也不落 localStorage。 */
export interface ResetPasswordOut {
  user_id: string;
  new_password: string;
}

export interface RoleOut {
  id: string;
  code: GlobalRole;
  name: string;
  description: string | null;
  is_system: boolean;
  user_count: number;
  permissions: string[];
}

export interface PermissionOut {
  id: string;
  code: string;
  name: string;
  module: string;
  description: string | null;
}

/** 权限码按模块分组下发：分组标题也用后端的 module_text，前端不另建字典。 */
export interface PermissionGroupOut {
  module: string;
  module_text: string;
  permissions: PermissionOut[];
}

export interface AuditLogOut {
  id: string;
  created_at: string;
  user: UserBrief | null;
  action: string;
  action_text: string;
  target_type: string;
  target_id: string | null;
  target_name: string | null;
  detail: Record<string, unknown> | null;
  ip: string | null;
}

export interface AuditActionOption {
  action: string;
  text: string;
}

export interface UserQuery {
  keyword?: string;
  role?: GlobalRole | null;
  status?: UserStatus | null;
  page?: number;
  pageSize?: number;
}

export interface AuditQuery {
  action?: string | null;
  userId?: string | null;
  /** 本地日期算出的绝对时刻（含时区偏移），后端换 UTC 后比较 */
  start?: string | null;
  end?: string | null;
  page?: number;
  pageSize?: number;
}
