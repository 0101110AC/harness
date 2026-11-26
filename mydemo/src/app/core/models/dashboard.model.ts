/** 与 prd-07 §6.3 对齐的工作台总览。 */
export interface DashboardOverview {
  pending_drawings: number;
  pending_drawings_in_review: number;
  pending_annotations: number;
  pending_annotations_to_review: number;
  confirmed_issues_this_week: number;
  confirmed_issues_last_week: number;
  /** 上周为 0 时后端返回 null（避免除零），前端显示 "—" */
  wow_change: number | null;
  ai_block_rate_this_week: number;
  pending_hitl: number;
}
