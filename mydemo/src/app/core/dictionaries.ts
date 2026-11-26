import { IconName } from '../shared/icon/icon';
import {
  AnnotationAction,
  AnnotationStatus,
  LogType,
  ShapeType,
} from './models/annotation.model';
import { DrawingStatus, FileType, SeverityKey } from './models/drawing.model';
import { NotificationType } from './models/notification.model';
import { DesignStage, ProjectRole } from './models/project.model';
import {
  Discipline,
  HitlDecision,
  IssueCategory,
  IssueStatus,
  ReviewStage,
  Severity,
} from './models/review.model';

/**
 * 与 prd-07 的枚举对齐的中文映射：全站唯一来源，页面只消费 label / 样式类，
 * 不允许各模块再写一套文案（prd-01 §5 的硬性要求）。
 */

export interface Option<T extends string> {
  readonly value: T;
  readonly label: string;
}

/* ---------- 项目 ---------- */

/** 业态字典：后端尚未提供字典接口，先由前端托管，D3 起统一从这里取。 */
export const BUILDING_TYPES: readonly string[] = [
  '住宅',
  '办公',
  '商业',
  '酒店',
  '教育',
  '医疗',
  '文化体育',
  '工业',
  '其他',
];

export const DESIGN_STAGES: readonly Option<DesignStage>[] = [
  { value: 'SCHEME', label: '方案设计' },
  { value: 'PRELIMINARY', label: '初步设计' },
  { value: 'CONSTRUCTION', label: '施工图设计' },
];

export const PROJECT_ROLE_LABEL: Record<ProjectRole, string> = {
  OWNER: '项目负责人',
  REVIEWER: '审图人',
  DESIGNER: '设计人',
  VIEWER: '只读',
};

export function designStageLabel(stage: string | null): string {
  return DESIGN_STAGES.find((item) => item.value === stage)?.label ?? '未指定阶段';
}

/* ---------- 图纸 ---------- */

export const FILE_TYPE_LABEL: Record<FileType, string> = {
  pdf: 'PDF',
  dxf: 'DXF',
};

/** PRD §4.4：PDF 红标 / DXF 蓝标。 */
export const FILE_TYPE_BADGE: Record<FileType, string> = {
  pdf: 'bg-danger-soft text-danger',
  dxf: 'bg-primary-soft text-primary',
};

export interface StatusMeta {
  readonly label: string;
  readonly dot: string;
  readonly text: string;
}

export const DRAWING_STATUS_META: Record<DrawingStatus, StatusMeta> = {
  UPLOADING: { label: '上传中', dot: 'bg-slate-400', text: 'text-slate-500' },
  UPLOADED: { label: '待预审', dot: 'bg-primary', text: 'text-primary' },
  PARSING: { label: '解析中', dot: 'bg-primary', text: 'text-primary' },
  PARSED: { label: '解析完成', dot: 'bg-primary', text: 'text-primary' },
  REVIEWING: { label: '预审中', dot: 'bg-annotation-modified', text: 'text-annotation-modified' },
  REVIEW_DONE: { label: '预审完成', dot: 'bg-success', text: 'text-success' },
  PARSE_FAILED: { label: '解析失败', dot: 'bg-danger', text: 'text-danger' },
  REVIEW_FAILED: { label: '预审失败', dot: 'bg-danger', text: 'text-danger' },
  ARCHIVED: { label: '已归档', dot: 'bg-slate-300', text: 'text-slate-400' },
};

/** 可「重新预审」的状态（prd-02 §4.4 行操作规则）。 */
export const RE_REVIEWABLE_STATUSES: ReadonlySet<DrawingStatus> = new Set<DrawingStatus>([
  'REVIEW_DONE',
  'REVIEW_FAILED',
  'PARSE_FAILED',
]);

export const SEVERITY_META: Record<SeverityKey, { label: string; text: string; dot: string }> = {
  BLOCKER: { label: '严重', text: 'text-severity-blocker', dot: 'bg-severity-blocker' },
  MAJOR: { label: '主要', text: 'text-severity-major', dot: 'bg-severity-major' },
  MINOR: { label: '一般', text: 'text-severity-minor', dot: 'bg-severity-minor' },
  INFO: { label: '提示', text: 'text-severity-info', dot: 'bg-severity-info' },
};

/* ---------- 预审问题（prd-02 §5.4~§5.7） ---------- */

export const SEVERITY_ORDER: readonly Severity[] = ['BLOCKER', 'MAJOR', 'MINOR', 'INFO'];

export const SEVERITY_OPTIONS: readonly Option<Severity>[] = SEVERITY_ORDER.map((value) => ({
  value,
  label: SEVERITY_META[value].label,
}));

export const DISCIPLINE_LABEL: Record<Discipline, string> = {
  ARCH: '建筑',
  STRUCT: '结构',
  MEP: '机电',
  GENERAL: '通用',
};

export const DISCIPLINES: readonly Discipline[] = ['ARCH', 'STRUCT', 'MEP', 'GENERAL'];

export const DISCIPLINE_OPTIONS: readonly Option<Discipline>[] = DISCIPLINES.map((value) => ({
  value,
  label: DISCIPLINE_LABEL[value],
}));

export const ISSUE_CATEGORY_LABEL: Record<IssueCategory, string> = {
  CODE: '规范校验',
  CONSISTENCY: '一致性',
  CLASH: '碰撞',
  COMPLETENESS: '完整性',
};

export interface IssueStatusMeta extends StatusMeta {
  readonly chip: string;
}

export const ISSUE_STATUS_META: Record<IssueStatus, IssueStatusMeta> = {
  PENDING: {
    label: '待判定',
    dot: 'bg-slate-400',
    text: 'text-slate-500',
    chip: 'bg-slate-100 text-slate-600',
  },
  ADOPTED: {
    label: '已采纳',
    dot: 'bg-success',
    text: 'text-success',
    chip: 'bg-success-soft text-success',
  },
  REJECTED: {
    label: '已驳回',
    dot: 'bg-slate-400',
    text: 'text-slate-500',
    chip: 'bg-slate-100 text-slate-500',
  },
  IGNORED: {
    label: '已忽略',
    dot: 'bg-slate-300',
    text: 'text-slate-400',
    chip: 'bg-slate-50 text-slate-400',
  },
};

export const ISSUE_STATUSES: readonly IssueStatus[] = ['PENDING', 'ADOPTED', 'REJECTED', 'IGNORED'];

export const ISSUE_STATUS_OPTIONS: readonly Option<IssueStatus>[] = ISSUE_STATUSES.map((value) => ({
  value,
  label: ISSUE_STATUS_META[value].label,
}));

/* ---------- HITL ---------- */

export const RISK_LEVEL_META: Record<'HIGH' | 'CRITICAL', StatusMeta> = {
  CRITICAL: { label: '严重', dot: 'bg-severity-blocker', text: 'text-severity-blocker' },
  HIGH: { label: '高', dot: 'bg-severity-major', text: 'text-severity-major' },
};

export const HITL_DECISION_LABEL: Record<HitlDecision, string> = {
  CONFIRM_AS_ISSUE: '确认是图纸问题',
  MARK_EXEMPT: '标记为特殊豁免',
};

/**
 * 阶段文案兜底（prd-06 §9.7）。正常链路一律用后端下发的 `progress.text`，
 * 这里只在事件缺字段时兜底，避免前端自造百分比或自造阶段文案。
 */
export const STAGE_FALLBACK_TEXT: Record<ReviewStage, string> = {
  LOAD_CONTEXT: '正在读取项目资料',
  PLANNING: '正在制定审查计划',
  PARSING: '正在解析图纸',
  RETRIEVING: '正在核对规范',
  VALIDATING: '正在校验指标',
  DETECTING: '正在归纳问题',
  WAITING_RISK: '等待设计师确认风险',
  REPORTING: '正在生成报告初稿',
  WAITING_REPORT: '等待确认报告',
  PACKAGING: '正在打包资料',
  DONE: '预审完成',
};

/* ---------- 批注（prd-03 §6 / §7） ---------- */

export interface AnnotationStatusMeta extends StatusMeta {
  /** 卡片左侧 4px 色条 */
  readonly bar: string;
  readonly chip: string;
  /** 时间线竖线颜色：状态变更形成视觉流向 */
  readonly line: string;
}

export const ANNOTATION_STATUS_META: Record<AnnotationStatus, AnnotationStatusMeta> = {
  PENDING_CONFIRM: {
    label: '待确认',
    dot: 'bg-annotation-pending',
    text: 'text-annotation-pending',
    bar: 'bg-annotation-pending',
    chip: 'bg-warning-soft text-warning',
    line: 'bg-annotation-pending',
  },
  ISSUED: {
    label: '已下发',
    dot: 'bg-annotation-issued',
    text: 'text-annotation-issued',
    bar: 'bg-annotation-issued',
    chip: 'bg-primary-soft text-primary',
    line: 'bg-annotation-issued',
  },
  MODIFIED: {
    label: '已修改待复核',
    dot: 'bg-annotation-modified',
    text: 'text-annotation-modified',
    bar: 'bg-annotation-modified',
    chip: 'bg-violet-50 text-violet-600',
    line: 'bg-annotation-modified',
  },
  CLOSED: {
    label: '已关闭',
    dot: 'bg-annotation-closed',
    text: 'text-annotation-closed',
    bar: 'bg-annotation-closed',
    chip: 'bg-success-soft text-success',
    line: 'bg-annotation-closed',
  },
};

export const ANNOTATION_STATUSES: readonly AnnotationStatus[] = [
  'PENDING_CONFIRM',
  'ISSUED',
  'MODIFIED',
  'CLOSED',
];

export interface ActionMeta {
  readonly label: string;
  readonly kind: 'primary' | 'secondary' | 'danger' | 'ghost';
}

/**
 * `allowed_actions` → 按钮。只做「怎么画」，不做「谁能画」：
 * 后端没下发的动作一律不渲染（prd-03 §5.5.5）。
 */
export const ANNOTATION_ACTION_META: Record<AnnotationAction, ActionMeta> = {
  ISSUE: { label: '下发', kind: 'primary' },
  MARK_MODIFIED: { label: '标记已修改', kind: 'primary' },
  CLOSE: { label: '复核通过', kind: 'primary' },
  REJECT_AND_REISSUE: { label: '复核不通过', kind: 'secondary' },
  DELETE: { label: '作废', kind: 'ghost' },
  EDIT: { label: '编辑', kind: 'ghost' },
  COMMENT: { label: '评论', kind: 'ghost' },
};

export const SHAPE_TYPE_LABEL: Record<ShapeType, string> = {
  RECT: '矩形框',
  POINT: '点标记',
  TEXT: '文字',
  FREEHAND: '自由画笔',
};

export interface TimelineLogMeta {
  readonly icon: IconName;
  /** 图标的底色，按事件类型区分（状态变更再用状态色覆盖） */
  readonly tone: string;
}

export const TIMELINE_LOG_META: Record<LogType, TimelineLogMeta> = {
  CREATED: { icon: 'plus', tone: 'bg-slate-100 text-slate-500' },
  STATUS_CHANGED: { icon: 'refresh', tone: 'bg-primary-soft text-primary' },
  ASSIGNED: { icon: 'user', tone: 'bg-primary-soft text-primary' },
  COMMENT: { icon: 'annotation', tone: 'bg-slate-100 text-slate-500' },
  VERSION_LINKED: { icon: 'paperclip', tone: 'bg-violet-50 text-violet-600' },
  CLOSED: { icon: 'check', tone: 'bg-success-soft text-success' },
  DELETED: { icon: 'trash', tone: 'bg-danger-soft text-danger' },
};

/* ---------- 站内信（prd-04 §4 / §6.2） ---------- */

export interface NotificationTypeMeta {
  /** 列表「类型」列的中文 */
  readonly label: string;
  readonly icon: IconName;
  readonly tone: string;
}

export const NOTIFICATION_TYPE_META: Record<NotificationType, NotificationTypeMeta> = {
  REVIEW_COMPLETED: { label: 'AI 预审', icon: 'file', tone: 'bg-violet-50 text-violet-600' },
  REVIEW_FAILED: { label: 'AI 预审', icon: 'file', tone: 'bg-violet-50 text-violet-600' },
  ANNOTATION_ASSIGNED: { label: '批注', icon: 'annotation', tone: 'bg-primary-soft text-primary' },
  ANNOTATION_SUBMITTED: { label: '批注', icon: 'annotation', tone: 'bg-primary-soft text-primary' },
  ANNOTATION_CLOSED: { label: '批注', icon: 'annotation', tone: 'bg-primary-soft text-primary' },
  HITL_PENDING: { label: '复核', icon: 'clock', tone: 'bg-warning-soft text-warning' },
  REPORT_READY: { label: '复核', icon: 'clock', tone: 'bg-warning-soft text-warning' },
  MEMBER_ADDED: { label: '项目', icon: 'user', tone: 'bg-slate-100 text-slate-500' },
  SYSTEM: { label: '系统', icon: 'megaphone', tone: 'bg-slate-100 text-slate-500' },
};

export const NOTIFICATION_CHANNEL_META: Record<
  'INAPP' | 'FEISHU' | 'EMAIL',
  { label: string; hint: string; badge: string; badgeTone: string }
> = {
  INAPP: {
    label: '站内信',
    hint: '顶部未读角标与消息中心（当前版本必须开启）',
    badge: '已启用',
    badgeTone: 'bg-success-soft text-success',
  },
  FEISHU: {
    label: '飞书',
    hint: '绑定后可在飞书接收审批与复核提醒',
    badge: '即将开放',
    badgeTone: 'bg-primary-soft text-primary',
  },
  EMAIL: {
    label: '邮件摘要',
    hint: '每日 18:00 汇总未处理批注',
    badge: '规划中',
    badgeTone: 'bg-slate-100 text-slate-500',
  },
};

/** 事件级订阅：V1.0 只读展示，证明架构预留（prd-04 §8）。 */
export const NOTIFICATION_EVENT_ROWS: readonly { key: NotificationType; label: string }[] = [
  { key: 'REVIEW_COMPLETED', label: '预审完成' },
  { key: 'ANNOTATION_ASSIGNED', label: '批注下发' },
  { key: 'ANNOTATION_SUBMITTED', label: '批注待复核' },
  { key: 'ANNOTATION_CLOSED', label: '批注关闭' },
];
