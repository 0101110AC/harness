import { HttpClient, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { Observable } from 'rxjs';
import { API_BASE_URL } from '../api';
import { DrawingOut, ReviewTaskOut, ViewUrlResponse } from '../models/drawing.model';
import { Page } from '../models/project.model';
import { DrawingStatus } from '../models/drawing.model';

export interface DrawingQuery {
  page?: number;
  pageSize?: number;
  status?: DrawingStatus;
}

@Injectable({ providedIn: 'root' })
export class DrawingService {
  private readonly http = inject(HttpClient);

  list(projectId: string, query: DrawingQuery = {}): Observable<Page<DrawingOut>> {
    let params = new HttpParams().set('project_id', projectId);
    if (query.page) {
      params = params.set('page', String(query.page));
    }
    if (query.pageSize) {
      params = params.set('page_size', String(query.pageSize));
    }
    if (query.status) {
      params = params.set('status', query.status);
    }
    return this.http.get<Page<DrawingOut>>(`${API_BASE_URL}/drawings`, { params });
  }

  /** 短时签名 URL（≤10 分钟），前端只负责 window.open，不缓存。 */
  getViewUrl(drawingId: string): Observable<ViewUrlResponse> {
    return this.http.get<ViewUrlResponse>(`${API_BASE_URL}/drawings/${drawingId}/view-url`);
  }

  /** 图纸详情：预审中心用它取版本号、页数与状态（prd-02 §8）。 */
  getById(drawingId: string): Observable<DrawingOut> {
    return this.http.get<DrawingOut>(`${API_BASE_URL}/drawings/${drawingId}`);
  }

  remove(drawingId: string): Observable<void> {
    return this.http.delete<void>(`${API_BASE_URL}/drawings/${drawingId}`);
  }

  /** 重新预审：异步任务，立即返回 202（进度走 SSE，D4 接入）。 */
  createReviewTask(drawingId: string): Observable<ReviewTaskOut> {
    return this.http.post<ReviewTaskOut>(`${API_BASE_URL}/drawings/${drawingId}/review-tasks`, {
      disciplines: ['ARCH', 'STRUCT', 'MEP'],
      rule_sets: ['GB50016', 'GB50352'],
    });
  }
}
