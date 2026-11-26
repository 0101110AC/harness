/**
 * 与 prd-07 §6.6 / §6.11 对齐的预审模型：问题清单、任务快照、HITL 中断、报告初稿。
 * SSE 信封与 payload 以 prd-07 §2.1 为权威，prd-06 §9.4 给完整示例。
 */

import { UserBrief } from './project.model';

export type Severity = 'BLOCKER' | 'MAJOR' | 'MINOR' | 'INFO';
export type Discipline = 'ARCH' | 'STRUCT' | 'MEP' | 'GENERAL';
export type IssueCategory = 'CODE' | 'CONSISTENCY' | 'CLASH' | 'COMPLETENESS';
export type IssueStatus = 'PENDING' | 'ADOPTED' | 'REJECTED' | 'IGNORED';

export type ReviewMode = 'PROFESSIONAL' | 'SCHOOL';
export type RunStatus =
  | 'PENDING'
  | 'RUNNING'
  | 'WAITING_HUMAN'
  | 'SUCCEEDED'
  | 'FAILED'
  | 'RETURNED'
  | 'CANCELED';

export type ReviewStage =
  | 'LOAD_CONTEXT'
  | 'PLANNING'
  | 'PARSING'
  | 'RETRIEVING'
  | 'VALIDATING'
  | 'DETECTING'
  | 'WAITING_RISK'
  | 'REPORTING'
  | 'WAITING_REPORT'
  | 'PACKAGING'
  | 'DONE';

/* ---------- 坐标（prd-07 §4） ---------- */

export interface BBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface PageSize {
  w: number;
  h: number;
}

export interface DxfMeta {
  layout: string;
  entities: string[];
  layers: string[];
}

export interface Location {
  file_type: 'pdf' | 'dxf';
  page_no: number;
  bbox: BBox;
  unit: 'pt' | 'mm';
  coord_system: 'pdf_user_space' | 'dxf_wcs';
  /** DXF 的 y 轴向上，换算屏幕坐标时必须翻转 */
  origin: 'top-left' | 'bottom-left';
  page_size: PageSize;
  dxf: DxfMeta | null;
}

/* ---------- 问题 ---------- */

export interface IssueOut {
  id: string;
  review_task_id: string;
  drawing_id: string;
  category: IssueCategory;
  category_text: string;
  severity: Severity;
  severity_text: string;
  discipline: Discipline;
  discipline_text: string;
  title: string;
  description: string | null;
  suggestion: string | null;
  rule_code: string | null;
  confidence: number;
  location: Location;
  location_valid: boolean;
  status: IssueStatus;
  status_text: string;
  annotation_id: string | null;
  created_at: string;
}

/** 清单渲染用的行模型：`issue_found` 先落占位行（无坐标），`run_completed` 后用全量接口替换。 */
export interface IssueRow {
  id: string;
  severity: Severity;
  discipline: Discipline;
  title: string;
  description: string | null;
  ruleCode: string | null;
  confidence: number;
  status: IssueStatus;
  location: Location | null;
  locationValid: boolean;
  annotationId: string | null;
  /** 由 SSE `issue_found` 实时插入、尚未落库 */
  live: boolean;
}

export interface IssueQuery {
  severity?: readonly Severity[];
  discipline?: readonly Discipline[];
  status?: readonly IssueStatus[];
  keyword?: string;
  page?: number;
  pageSize?: number;
}

/* ---------- 任务快照 ---------- */

export interface IssueSummary {
  by_severity?: Partial<Record<Severity, number>>;
  by_discipline?: Partial<Record<Discipline, number>>;
  by_category?: Partial<Record<IssueCategory, number>>;
}

export interface ReviewTaskSnapshot {
  id: string;
  drawing_id: string;
  thread_id: string;
  status: string;
  status_text: string;
  run_status: RunStatus;
  stage: ReviewStage | null;
  stage_text: string | null;
  progress: number;
  issue_total: number;
  issue_blocker: number;
  summary: IssueSummary | null;
  engine: string;
  cost_ms: number | null;
  last_seq: number;
  waiting_interrupt_id: string | null;
  report_id: string | null;
  error_reason: string | null;
  created_at: string;
  finished_at: string | null;
}

/* ---------- HITL ---------- */

export type HitlDecision = 'CONFIRM_AS_ISSUE' | 'MARK_EXEMPT';
export type InterruptType = 'RISK_CONFIRM' | 'REPORT_CONFIRM' | 'SCHOOL_REVIEW';
export type InterruptStatus = 'PENDING' | 'VIEWED' | 'RESOLVED' | 'EXPIRED' | 'CANCELED';

export interface RiskItem {
  risk_id: string;
  title: string;
  level: 'HIGH' | 'CRITICAL';
  metric_key: string | null;
  detected_value: number | null;
  threshold: number | null;
  clause_ref: string | null;
  why_suspicious: string | null;
  drawing_id: string | null;
  location: Location | null;
}

export interface RiskItemDecision {
  risk_id: string;
  decision: HitlDecision;
  remark: string | null;
}

export interface InterruptTaskOut {
  id: string;
  thread_id: string;
  review_task_id: string;
  drawing_id: string;
  type: InterruptType;
  type_text: string;
  status: InterruptStatus;
  title: string;
  items: RiskItem[];
  report_id: string | null;
  assignee: UserBrief | null;
  resolution: string | null;
  item_decisions: RiskItemDecision[];
  remark: string | null;
  expires_at: string;
  remaining_seconds: number;
  created_at: string;
}

export interface HitlResumeRequest {
  interrupt_id: string;
  item_decisions: RiskItemDecision[];
  remark: string | null;
}

export interface HitlResumeResponse {
  interrupt_id: string;
  status: InterruptStatus;
  run_status: RunStatus;
  resumed: boolean;
}

export type ReportDecision = 'APPROVED' | 'REJECTED';

export interface ConfirmReportRequest {
  interrupt_id: string;
  decision: ReportDecision;
  remark: string | null;
}

export interface ConfirmReportResponse {
  report_id: string;
  status: ReportStatus;
  run_status: RunStatus;
  artifact_url: string | null;
}

/* ---------- 报告初稿 ---------- */

export type ReportStatus = 'DRAFT' | 'WAITING_CONFIRM' | 'CONFIRMED' | 'REJECTED' | 'RETURNED';

export interface ReportMetric {
  key?: string;
  label?: string;
  name?: string;
  value?: string | number;
  limit?: string | number;
  unit?: string;
  conclusion?: string;
}

export interface ReportSection {
  key: string;
  title: string;
  body_md: string;
  metrics: ReportMetric[];
  issue_refs: string[];
}

export interface RiskDecisionRecord {
  risk_id: string;
  title?: string;
  level?: string;
  decision: HitlDecision;
  remark: string | null;
  operator?: string;
}

export interface ReportSummary {
  issue_total?: number;
  by_severity?: Partial<Record<Severity, number>>;
  risk_decided?: number;
  risk_exempt?: number;
  norm_total?: number;
}

export interface ReportDraftOut {
  id: string;
  review_task_id: string;
  drawing_id: string;
  version: number;
  status: ReportStatus;
  status_text: string;
  title: string;
  summary: ReportSummary | null;
  sections: ReportSection[];
  risk_decisions: RiskDecisionRecord[];
  artifact_url: string | null;
  reject_reason: string | null;
  confirmed_at: string | null;
  created_at: string;
}

/* ---------- SSE ---------- */

export type AgentEventType =
  | 'run_started'
  | 'plan_created'
  | 'node_started'
  | 'node_finished'
  | 'progress'
  | 'agent_thought'
  | 'tool_call'
  | 'tool_result'
  | 'issue_found'
  | 'risk_found'
  | 'risk_interrupt'
  | 'hitl_resolved'
  | 'report_ready'
  | 'report_interrupt'
  | 'run_returned'
  | 'run_completed'
  | 'run_failed'
  | 'sandbox_status'
  | 'heartbeat'
  | 'stream_idle'
  | 'stream_reset';

export interface PlanStep {
  step_id: string;
  name: string;
  status: string;
}

export interface RunStartedPayload {
  percent: number;
  engine: string;
  review_mode: ReviewMode;
  drawing_id: string;
}

export interface PlanCreatedPayload {
  percent?: number;
  rationale: string;
  steps: PlanStep[];
}

export interface ProgressPayload {
  percent: number;
  stage: ReviewStage;
  /** 后端下发文案（prd-06 §9.7）；缺失时前端只做兜底，不自造进度 */
  text?: string;
}

export interface AgentThoughtPayload {
  node: string;
  text: string;
  tokens?: number;
}

export interface IssueFoundPayload {
  issue_id: string;
  severity: Severity;
  discipline: Discipline;
  title: string;
  confidence: number;
  location_valid?: boolean;
}

export interface RiskInterruptPayload {
  interrupt_id: string;
  type: InterruptType;
  title: string;
  expires_at?: string;
  remaining_seconds?: number;
  items: RiskItem[];
}

export interface HitlResolvedPayload {
  interrupt_id: string;
  resolution: HitlDecision;
  operator: string;
  item_decisions: RiskItemDecision[];
  resumed: boolean;
}

export interface ReportReadyPayload {
  percent: number;
  report_id: string;
  version: number;
  issue_total: number;
}

export interface ReportInterruptPayload {
  interrupt_id: string;
  type: InterruptType;
  report_id: string;
  title: string;
  expires_at?: string;
}

export interface RunCompletedPayload {
  percent: number;
  status: RunStatus;
  issue_total: number;
  duration_ms: number;
  report_id: string | null;
  thread_id: string;
}

export interface RunFailedPayload {
  status: RunStatus;
  reason: string;
  message: string;
  retryable: boolean;
}

export interface RunReturnedPayload {
  report_id?: string;
  comment?: string;
  reason?: string;
}

export interface StreamResetPayload {
  reason?: string;
}

export interface AgentEvent<T extends AgentEventType, D> {
  seq: number;
  thread_id: string;
  run_id: string;
  event: T;
  ts: string;
  data: D;
}

/**
 * 前端实际消费的事件联合类型。
 * 只声明需要渲染的事件，其余事件后端照发、前端不注册监听（prd-02 §5.3.2）。
 */
export type ReviewStreamEvent =
  | AgentEvent<'run_started', RunStartedPayload>
  | AgentEvent<'plan_created', PlanCreatedPayload>
  | AgentEvent<'progress', ProgressPayload>
  | AgentEvent<'agent_thought', AgentThoughtPayload>
  | AgentEvent<'issue_found', IssueFoundPayload>
  | AgentEvent<'risk_interrupt', RiskInterruptPayload>
  | AgentEvent<'hitl_resolved', HitlResolvedPayload>
  | AgentEvent<'report_ready', ReportReadyPayload>
  | AgentEvent<'report_interrupt', ReportInterruptPayload>
  | AgentEvent<'run_returned', RunReturnedPayload>
  | AgentEvent<'run_completed', RunCompletedPayload>
  | AgentEvent<'run_failed', RunFailedPayload>
  | AgentEvent<'stream_reset', StreamResetPayload>
  | AgentEvent<'heartbeat', Record<string, never>>;

export interface StreamTokenOut {
  token: string;
  expires_at: string;
  /** 已拼好 query 的相对路径，前端直接交给 EventSource */
  stream_url: string;
}

export interface ReviewTaskBrief {
  id: string;
  thread_id: string;
  drawing_id: string;
  status: string;
  status_text: string;
  run_status: RunStatus;
  progress: number;
  issue_total: number;
  created_at: string;
}

export interface BatchAdoptResponse {
  created: number;
  annotations: string[];
}
