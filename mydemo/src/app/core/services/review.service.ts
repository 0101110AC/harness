import { HttpClient, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { map, Observable } from 'rxjs';
import { API_BASE_URL } from '../api';
import { Page } from '../models/project.model';
import {
  BatchAdoptResponse,
  ConfirmReportRequest,
  ConfirmReportResponse,
  HitlResumeRequest,
  HitlResumeResponse,
  InterruptTaskOut,
  IssueOut,
  IssueQuery,
  ReportDraftOut,
  ReviewTaskBrief,
  ReviewTaskSnapshot,
} from '../models/review.model';

/** 预审中心的数据入口：问题清单、任务快照、HITL 与报告初稿（prd-07 §5.4~§5.5）。 */
@Injectable({ providedIn: 'root' })
export class ReviewService {
  private readonly http = inject(HttpClient);

  /* ---------- 任务 ---------- */

  /** 首屏 / 断线恢复用的终态快照；接口注释明确禁止用它轮询进度。 */
  getTask(taskId: string): Observable<ReviewTaskSnapshot> {
    return this.http.get<ReviewTaskSnapshot>(`${API_BASE_URL}/review-tasks/${taskId}`);
  }

  /**
   * 图纸下最近一次预审任务。
   * PRD 的列表接口清单里没有单列它，但图纸列表的「查看预审」必须能找到 taskId；
   * 契约未覆盖时按 `[]` 收敛，页面退化成"发起预审"空态，而不是白屏。
   */
  listTasks(drawingId: string): Observable<readonly ReviewTaskBrief[]> {
    return this.http
      .get<unknown>(`${API_BASE_URL}/drawings/${drawingId}/review-tasks`, {
        params: new HttpParams().set('page_size', '5'),
      })
      .pipe(map((body) => toList<ReviewTaskBrief>(body)));
  }

  /* ---------- 问题清单 ---------- */

  listIssues(taskId: string, query: IssueQuery = {}): Observable<Page<IssueOut>> {
    let params = new HttpParams()
      .set('page', String(query.page ?? 1))
      .set('page_size', String(query.pageSize ?? 100));

    for (const severity of query.severity ?? []) {
      params = params.append('severity', severity);
    }
    for (const discipline of query.discipline ?? []) {
      params = params.append('discipline', discipline);
    }
    for (const status of query.status ?? []) {
      params = params.append('status', status);
    }
    if (query.keyword?.trim()) {
      params = params.set('keyword', query.keyword.trim());
    }

    return this.http.get<Page<IssueOut>>(`${API_BASE_URL}/review-tasks/${taskId}/issues`, {
      params,
    });
  }

  adopt(issueId: string): Observable<IssueOut> {
    return this.http.post<IssueOut>(`${API_BASE_URL}/issues/${issueId}/adopt`, {});
  }

  /** 批量采纳：后端事务内整批成功或整批回滚（prd-07 §7.5）。 */
  batchAdopt(issueIds: readonly string[]): Observable<BatchAdoptResponse> {
    return this.http.post<BatchAdoptResponse>(`${API_BASE_URL}/issues/batch-adopt`, {
      issue_ids: issueIds,
    });
  }

  /* ---------- HITL ---------- */

  /**
   * 当前待处理中断。契约只写了"查询当前中断"，
   * 这里兼容 `null` / 单对象 / 数组 / `{items: []}` 四种返回形态。
   */
  getPendingInterrupt(drawingId: string): Observable<InterruptTaskOut | null> {
    return this.http
      .get<unknown>(`${API_BASE_URL}/drawings/${drawingId}/hitl/pending`)
      .pipe(map((body) => toList<InterruptTaskOut>(body)[0] ?? null));
  }

  resumeRisk(drawingId: string, payload: HitlResumeRequest): Observable<HitlResumeResponse> {
    return this.http.post<HitlResumeResponse>(
      `${API_BASE_URL}/drawings/${drawingId}/hitl/resume`,
      payload,
    );
  }

  confirmReport(
    drawingId: string,
    payload: ConfirmReportRequest,
  ): Observable<ConfirmReportResponse> {
    return this.http.post<ConfirmReportResponse>(
      `${API_BASE_URL}/drawings/${drawingId}/hitl/confirm-report`,
      payload,
    );
  }

  /* ---------- 报告 ---------- */

  getReportDraft(taskId: string): Observable<ReportDraftOut> {
    return this.http.get<ReportDraftOut>(`${API_BASE_URL}/review-tasks/${taskId}/report-draft`);
  }
}

/** 把"单对象 / 数组 / 分页信封 / null"收敛成数组，避免为契约歧义写四份分支。 */
function toList<T>(body: unknown): readonly T[] {
  if (Array.isArray(body)) {
    return body as T[];
  }
  if (typeof body === 'object' && body !== null) {
    const items = (body as { items?: unknown }).items;
    if (Array.isArray(items)) {
      return items as T[];
    }
    if ('id' in body) {
      return [body as T];
    }
  }
  return [];
}
