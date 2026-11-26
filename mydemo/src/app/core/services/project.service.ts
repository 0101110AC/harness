import { HttpClient, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { Observable } from 'rxjs';
import { API_BASE_URL } from '../api';
import {
  Page,
  ProjectCreateRequest,
  ProjectDetailOut,
  ProjectOut,
  ProjectUpdateRequest,
} from '../models/project.model';

export type ProjectSort = 'recent' | 'updated' | 'name';

export interface ProjectQuery {
  sort?: ProjectSort;
  page?: number;
  pageSize?: number;
  /** 统计卡下钻：pending-review（prd-01 §4.3） */
  filter?: string;
  /** 批注状态筛选：open */
  status?: string;
  /** 本周确认问题下钻 */
  closedThisWeek?: boolean;
}

@Injectable({ providedIn: 'root' })
export class ProjectService {
  private readonly http = inject(HttpClient);

  getAll(query: ProjectQuery = {}): Observable<Page<ProjectOut>> {
    let params = new HttpParams();
    if (query.sort) {
      params = params.set('sort', query.sort);
    }
    if (query.page) {
      params = params.set('page', String(query.page));
    }
    if (query.pageSize) {
      params = params.set('page_size', String(query.pageSize));
    }
    if (query.filter) {
      params = params.set('filter', query.filter);
    }
    if (query.status) {
      params = params.set('status', query.status);
    }
    if (query.closedThisWeek) {
      params = params.set('closed_this_week', '1');
    }
    return this.http.get<Page<ProjectOut>>(`${API_BASE_URL}/projects`, { params });
  }

  getById(id: string): Observable<ProjectDetailOut> {
    return this.http.get<ProjectDetailOut>(`${API_BASE_URL}/projects/${encodeURIComponent(id)}`);
  }

  create(payload: ProjectCreateRequest): Observable<ProjectOut> {
    return this.http.post<ProjectOut>(`${API_BASE_URL}/projects`, payload);
  }

  /** 重命名 / 归档都走同一个 PATCH（prd-05 §5.3 之外的项目编辑契约）。 */
  update(id: string, payload: ProjectUpdateRequest): Observable<ProjectOut> {
    return this.http.patch<ProjectOut>(
      `${API_BASE_URL}/projects/${encodeURIComponent(id)}`,
      payload,
    );
  }

  /** 软删除：后端级联图纸与批注，历史留痕。 */
  remove(id: string): Observable<{ message: string }> {
    return this.http.delete<{ message: string }>(
      `${API_BASE_URL}/projects/${encodeURIComponent(id)}`,
    );
  }
}
