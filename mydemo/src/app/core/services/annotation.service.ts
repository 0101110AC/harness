import { HttpClient, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { map, Observable } from 'rxjs';
import { API_BASE_URL } from '../api';
import {
  AnnotationCreateRequest,
  AnnotationOut,
  AnnotationQuery,
  AnnotationTransitionRequest,
  TimelineItem,
} from '../models/annotation.model';
import { Page } from '../models/project.model';

/** 批注数据入口（prd-03 §9 / prd-07 §6.7）。 */
@Injectable({ providedIn: 'root' })
export class AnnotationService {
  private readonly http = inject(HttpClient);

  list(query: AnnotationQuery = {}): Observable<Page<AnnotationOut>> {
    let params = new HttpParams()
      .set('page', String(query.page ?? 1))
      .set('page_size', String(query.pageSize ?? 200));

    if (query.projectId) {
      params = params.set('project_id', query.projectId);
    }
    if (query.drawingId) {
      params = params.set('drawing_id', query.drawingId);
    }
    if (query.status) {
      params = params.set('status', query.status);
    }
    if (query.severity) {
      params = params.set('severity', query.severity);
    }
    if (query.assigneeId) {
      params = params.set('assignee_id', query.assigneeId);
    }
    if (query.keyword?.trim()) {
      params = params.set('keyword', query.keyword.trim());
    }

    return this.http.get<Page<AnnotationOut>>(`${API_BASE_URL}/annotations`, { params });
  }

  /** 画布主页只需要「这张图纸的全部批注」：一次取回，翻页与筛选都在前端做。 */
  listByDrawing(drawingId: string, pageSize = 200): Observable<readonly AnnotationOut[]> {
    return this.list({ drawingId, pageSize }).pipe(map((page) => page.items ?? []));
  }

  create(payload: AnnotationCreateRequest): Observable<AnnotationOut> {
    return this.http.post<AnnotationOut>(`${API_BASE_URL}/annotations`, payload);
  }

  /** 状态流转：必须带 `row_version`，不一致时后端返回 409（乐观锁）。 */
  transition(id: string, payload: AnnotationTransitionRequest): Observable<AnnotationOut> {
    return this.http.post<AnnotationOut>(
      `${API_BASE_URL}/annotations/${encodeURIComponent(id)}/transition`,
      payload,
    );
  }

  /** 作废 = 软删除（`is_deleted=true`），不是第五个状态。 */
  remove(id: string, remark: string | null = null): Observable<void> {
    const params = remark ? new HttpParams().set('remark', remark) : undefined;
    return this.http.delete<void>(`${API_BASE_URL}/annotations/${encodeURIComponent(id)}`, {
      params,
    });
  }

  timeline(id: string, page = 1, pageSize = 100): Observable<readonly TimelineItem[]> {
    return this.http
      .get<unknown>(`${API_BASE_URL}/annotations/${encodeURIComponent(id)}/timeline`, {
        params: new HttpParams().set('page', String(page)).set('page_size', String(pageSize)),
      })
      .pipe(map((body) => toTimelineItems(body)));
  }

  comment(id: string, content: string): Observable<TimelineItem> {
    return this.http.post<TimelineItem>(
      `${API_BASE_URL}/annotations/${encodeURIComponent(id)}/comments`,
      { content },
    );
  }

  deleteComment(commentId: string): Observable<void> {
    return this.http.delete<void>(
      `${API_BASE_URL}/annotations/comments/${encodeURIComponent(commentId)}`,
    );
  }
}

/** 时间线契约允许 `[]` / `{items:[]}` 两种信封，这里统一收敛。 */
function toTimelineItems(body: unknown): readonly TimelineItem[] {
  if (Array.isArray(body)) {
    return body as TimelineItem[];
  }
  if (typeof body === 'object' && body !== null) {
    const items = (body as { items?: unknown }).items;
    if (Array.isArray(items)) {
      return items as TimelineItem[];
    }
  }
  return [];
}
