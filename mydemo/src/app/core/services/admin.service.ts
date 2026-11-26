import { HttpClient, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { Observable } from 'rxjs';
import { API_BASE_URL } from '../api';
import {
  AdminUserCreated,
  AdminUserOut,
  AuditActionOption,
  AuditLogOut,
  AuditQuery,
  PermissionGroupOut,
  ResetPasswordOut,
  RoleOut,
  UserCreateRequest,
  UserQuery,
  UserStatus,
  UserUpdateRequest,
} from '../models/admin.model';
import { Page } from '../models/project.model';

/** 系统管理数据入口（prd-05 §9）。所有写操作都由后端落审计日志。 */
@Injectable({ providedIn: 'root' })
export class AdminService {
  private readonly http = inject(HttpClient);

  listUsers(query: UserQuery = {}): Observable<Page<AdminUserOut>> {
    let params = new HttpParams()
      .set('page', String(query.page ?? 1))
      .set('page_size', String(query.pageSize ?? 20));
    if (query.keyword?.trim()) {
      params = params.set('keyword', query.keyword.trim());
    }
    if (query.role) {
      params = params.set('role', query.role);
    }
    if (query.status) {
      params = params.set('status', query.status);
    }
    return this.http.get<Page<AdminUserOut>>(`${API_BASE_URL}/users`, { params });
  }

  createUser(payload: UserCreateRequest): Observable<AdminUserCreated> {
    return this.http.post<AdminUserCreated>(`${API_BASE_URL}/users`, payload);
  }

  updateUser(id: string, payload: UserUpdateRequest): Observable<AdminUserOut> {
    return this.http.patch<AdminUserOut>(`${API_BASE_URL}/users/${encodeURIComponent(id)}`, payload);
  }

  resetPassword(id: string): Observable<ResetPasswordOut> {
    return this.http.post<ResetPasswordOut>(
      `${API_BASE_URL}/users/${encodeURIComponent(id)}/reset-password`,
      {},
    );
  }

  setStatus(id: string, status: UserStatus): Observable<AdminUserOut> {
    return this.http.post<AdminUserOut>(`${API_BASE_URL}/users/${encodeURIComponent(id)}/status`, {
      status,
    });
  }

  listRoles(): Observable<RoleOut[]> {
    return this.http.get<RoleOut[]>(`${API_BASE_URL}/roles`);
  }

  listPermissions(): Observable<PermissionGroupOut[]> {
    return this.http.get<PermissionGroupOut[]>(`${API_BASE_URL}/permissions`);
  }

  listAuditActions(): Observable<AuditActionOption[]> {
    return this.http.get<AuditActionOption[]>(`${API_BASE_URL}/audit-logs/actions`);
  }

  listAuditLogs(query: AuditQuery = {}): Observable<Page<AuditLogOut>> {
    let params = new HttpParams()
      .set('page', String(query.page ?? 1))
      .set('page_size', String(query.pageSize ?? 20));
    if (query.action) {
      params = params.set('action', query.action);
    }
    if (query.userId) {
      params = params.set('user_id', query.userId);
    }
    if (query.start) {
      params = params.set('start', query.start);
    }
    if (query.end) {
      params = params.set('end', query.end);
    }
    return this.http.get<Page<AuditLogOut>>(`${API_BASE_URL}/audit-logs`, { params });
  }
}
