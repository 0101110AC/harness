/**
 * 与 prd-03 / prd-07 §6.7 对齐的批注模型。
 *
 * 注意 `allowed_actions` 是后端算好的「当前用户在这条批注上能做什么」，
 * 前端只负责按它渲染按钮，不允许再写一套身份判断（prd-03 §5.5.5）。
 */

import { UserBrief } from './project.model';
import { Discipline, Location, Severity } from './review.model';

export type ShapeType = 'RECT' | 'POINT' | 'TEXT' | 'FREEHAND';
export type AnnotationStatus = 'PENDING_CONFIRM' | 'ISSUED' | 'MODIFIED' | 'CLOSED';
export type AnnotationSource = 'HUMAN' | 'AI';

/** `allowed_actions` 取值：`TransitionAction` 的超集，含不改状态的编辑类动作。 */
export type AnnotationAction =
  | 'ISSUE'
  | 'MARK_MODIFIED'
  | 'CLOSE'
  | 'REJECT_AND_REISSUE'
  | 'DELETE'
  | 'EDIT'
  | 'COMMENT';

/** 会改变状态的五个动作（`POST /annotations/{id}/transition`）。 */
export type TransitionAction = 'ISSUE' | 'MARK_MODIFIED' | 'CLOSE' | 'REJECT_AND_REISSUE' | 'DELETE';

export type LogType =
  | 'CREATED'
  | 'STATUS_CHANGED'
  | 'ASSIGNED'
  | 'COMMENT'
  | 'VERSION_LINKED'
  | 'CLOSED'
  | 'DELETED';

export interface AnnotationOut {
  id: string;
  project_id: string;
  drawing_id: string;
  /** 后端批量填充：全局列表/待办中心要在一行里显示归属 */
  project_name: string | null;
  drawing_name: string | null;
  drawing_version: number;
  issue_id: string | null;
  source: AnnotationSource;
  shape_type: ShapeType;
  location: Location;
  location_valid: boolean;
  title: string;
  content: string | null;
  severity: Severity;
  severity_text: string;
  discipline: Discipline;
  discipline_text: string;
  status: AnnotationStatus;
  status_text: string;
  assignee: UserBrief | null;
  assignee_missing: boolean;
  creator: UserBrief;
  due_date: string | null;
  version_drawing_id: string | null;
  close_remark: string | null;
  closed_at: string | null;
  comment_count: number;
  is_stale_version: boolean;
  allowed_actions: AnnotationAction[];
  row_version: number;
  created_at: string;
  updated_at: string;
}

export interface AnnotationCreateRequest {
  project_id: string;
  drawing_id: string;
  shape_type: ShapeType;
  location: Location;
  title: string;
  content: string | null;
  severity: Severity;
  discipline: Discipline;
  assignee_id: string | null;
  due_date: string | null;
}

export interface AnnotationTransitionRequest {
  action: TransitionAction;
  /** 乐观锁：与卡片上的 row_version 不一致时后端返回 409 */
  row_version: number;
  assignee_id?: string | null;
  version_drawing_id?: string | null;
  close_remark?: string | null;
  reject_reason?: string | null;
}

export interface AnnotationQuery {
  projectId?: string;
  drawingId?: string;
  status?: AnnotationStatus;
  severity?: Severity;
  assigneeId?: string;
  keyword?: string;
  page?: number;
  pageSize?: number;
}

export interface TimelineItem {
  id: string;
  log_type: LogType;
  log_type_text: string;
  operator: UserBrief;
  from_status: AnnotationStatus | null;
  to_status: AnnotationStatus | null;
  remark: string | null;
  content: string | null;
  can_delete: boolean;
  created_at: string;
}

export interface BatchTransitionRequest {
  annotation_ids: string[];
  action: TransitionAction;
  skip_invalid: boolean;
  assignee_id?: string | null;
  close_remark?: string | null;
}

export interface InvalidItem {
  annotation_id: string;
  reason: string;
}

export interface BatchTransitionResponse {
  succeeded: number;
  failed: InvalidItem[];
}
