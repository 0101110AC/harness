"""prd-07 §2 全部枚举（唯一权威，代码与文档逐字对齐）。"""
from __future__ import annotations

from enum import Enum


class RoleCode(str, Enum):
    ADMIN = "ADMIN"
    REVIEWER = "REVIEWER"
    DESIGNER = "DESIGNER"
    VIEWER = "VIEWER"


class ProjectRole(str, Enum):
    OWNER = "OWNER"
    REVIEWER = "REVIEWER"
    DESIGNER = "DESIGNER"
    VIEWER = "VIEWER"


class ProjectStatus(str, Enum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class UserStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class UploadSessionStatus(str, Enum):
    UPLOADING = "UPLOADING"
    MERGING = "MERGING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class FileType(str, Enum):
    PDF = "pdf"
    DXF = "dxf"


class DrawingStatus(str, Enum):
    UPLOADING = "UPLOADING"
    UPLOADED = "UPLOADED"
    PARSING = "PARSING"
    PARSED = "PARSED"
    REVIEWING = "REVIEWING"
    REVIEW_DONE = "REVIEW_DONE"
    PARSE_FAILED = "PARSE_FAILED"
    REVIEW_FAILED = "REVIEW_FAILED"
    ARCHIVED = "ARCHIVED"


class ReviewTaskStatus(str, Enum):
    PENDING = "PENDING"
    PARSING = "PARSING"
    REVIEWING = "REVIEWING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class ReviewStage(str, Enum):
    LOAD_CONTEXT = "LOAD_CONTEXT"
    PLANNING = "PLANNING"
    PARSING = "PARSING"
    RETRIEVING = "RETRIEVING"
    VALIDATING = "VALIDATING"
    DETECTING = "DETECTING"
    WAITING_RISK = "WAITING_RISK"
    REPORTING = "REPORTING"
    WAITING_REPORT = "WAITING_REPORT"
    PACKAGING = "PACKAGING"
    DONE = "DONE"


class ReviewMode(str, Enum):
    PROFESSIONAL = "PROFESSIONAL"
    SCHOOL = "SCHOOL"


class RunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RETURNED = "RETURNED"
    CANCELED = "CANCELED"


class Severity(str, Enum):
    BLOCKER = "BLOCKER"
    MAJOR = "MAJOR"
    MINOR = "MINOR"
    INFO = "INFO"


class Discipline(str, Enum):
    ARCH = "ARCH"
    STRUCT = "STRUCT"
    MEP = "MEP"
    GENERAL = "GENERAL"


class IssueCategory(str, Enum):
    CODE = "CODE"
    CONSISTENCY = "CONSISTENCY"
    CLASH = "CLASH"
    COMPLETENESS = "COMPLETENESS"


class IssueStatus(str, Enum):
    PENDING = "PENDING"
    ADOPTED = "ADOPTED"
    REJECTED = "REJECTED"
    IGNORED = "IGNORED"


class RejectReason(str, Enum):
    FALSE_POSITIVE = "FALSE_POSITIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    DUPLICATE = "DUPLICATE"
    OTHER = "OTHER"


class AnnotationSource(str, Enum):
    HUMAN = "HUMAN"
    AI = "AI"


class ShapeType(str, Enum):
    RECT = "RECT"
    POINT = "POINT"
    TEXT = "TEXT"
    FREEHAND = "FREEHAND"


class AnnotationStatus(str, Enum):
    PENDING_CONFIRM = "PENDING_CONFIRM"
    ISSUED = "ISSUED"
    MODIFIED = "MODIFIED"
    CLOSED = "CLOSED"


class LogType(str, Enum):
    CREATED = "CREATED"
    STATUS_CHANGED = "STATUS_CHANGED"
    ASSIGNED = "ASSIGNED"
    COMMENT = "COMMENT"
    VERSION_LINKED = "VERSION_LINKED"
    CLOSED = "CLOSED"
    DELETED = "DELETED"


class NotificationType(str, Enum):
    REVIEW_COMPLETED = "REVIEW_COMPLETED"
    REVIEW_FAILED = "REVIEW_FAILED"
    ANNOTATION_ASSIGNED = "ANNOTATION_ASSIGNED"
    ANNOTATION_SUBMITTED = "ANNOTATION_SUBMITTED"
    ANNOTATION_CLOSED = "ANNOTATION_CLOSED"
    MEMBER_ADDED = "MEMBER_ADDED"
    HITL_PENDING = "HITL_PENDING"
    REPORT_READY = "REPORT_READY"
    SYSTEM = "SYSTEM"


class NotificationChannel(str, Enum):
    INAPP = "INAPP"
    FEISHU = "FEISHU"
    EMAIL = "EMAIL"


class BizType(str, Enum):
    ANNOTATION = "ANNOTATION"
    REVIEW_TASK = "REVIEW_TASK"
    PROJECT = "PROJECT"
    SYSTEM = "SYSTEM"


class EventStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


class AuditAction(str, Enum):
    """prd-05 §8 动作清单 + 启用/编辑两个必要补充。"""

    USER_CREATE = "USER_CREATE"
    USER_UPDATE = "USER_UPDATE"
    USER_ROLE_UPDATE = "USER_ROLE_UPDATE"
    USER_DISABLE = "USER_DISABLE"
    USER_ENABLE = "USER_ENABLE"
    PASSWORD_RESET = "PASSWORD_RESET"
    MEMBER_ADD = "MEMBER_ADD"
    MEMBER_REMOVE = "MEMBER_REMOVE"
    PROJECT_DELETE = "PROJECT_DELETE"
    DRAWING_DELETE = "DRAWING_DELETE"
    ANNOTATION_DELETE = "ANNOTATION_DELETE"


class InterruptType(str, Enum):
    RISK_CONFIRM = "RISK_CONFIRM"
    REPORT_CONFIRM = "REPORT_CONFIRM"
    SCHOOL_REVIEW = "SCHOOL_REVIEW"


class InterruptStatus(str, Enum):
    PENDING = "PENDING"
    VIEWED = "VIEWED"
    RESOLVED = "RESOLVED"
    EXPIRED = "EXPIRED"
    CANCELED = "CANCELED"


class HitlResolution(str, Enum):
    CONFIRM_AS_ISSUE = "CONFIRM_AS_ISSUE"
    MARK_EXEMPT = "MARK_EXEMPT"


class ReportDecision(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ReportStatus(str, Enum):
    DRAFT = "DRAFT"
    WAITING_CONFIRM = "WAITING_CONFIRM"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    RETURNED = "RETURNED"


class AgentEventType(str, Enum):
    RUN_STARTED = "run_started"
    PLAN_CREATED = "plan_created"
    NODE_STARTED = "node_started"
    NODE_FINISHED = "node_finished"
    PROGRESS = "progress"
    AGENT_THOUGHT = "agent_thought"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ISSUE_FOUND = "issue_found"
    RISK_FOUND = "risk_found"
    RISK_INTERRUPT = "risk_interrupt"
    HITL_RESOLVED = "hitl_resolved"
    REPORT_READY = "report_ready"
    REPORT_INTERRUPT = "report_interrupt"
    RUN_RETURNED = "run_returned"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    STREAM_RESET = "stream_reset"
    STREAM_IDLE = "stream_idle"
    SANDBOX_STATUS = "sandbox_status"
    HEARTBEAT = "heartbeat"


# 中文展示映射（后端下发 *_text，前端不自行映射）
SEVERITY_TEXT = {
    Severity.BLOCKER: "严重",
    Severity.MAJOR: "主要",
    Severity.MINOR: "一般",
    Severity.INFO: "提示",
}
DISCIPLINE_TEXT = {
    Discipline.ARCH: "建筑",
    Discipline.STRUCT: "结构",
    Discipline.MEP: "机电",
    Discipline.GENERAL: "通用",
}
ANNOTATION_STATUS_TEXT = {
    AnnotationStatus.PENDING_CONFIRM: "待确认",
    AnnotationStatus.ISSUED: "已下发",
    AnnotationStatus.MODIFIED: "已修改待复核",
    AnnotationStatus.CLOSED: "已关闭",
}
ISSUE_STATUS_TEXT = {
    IssueStatus.PENDING: "待判定",
    IssueStatus.ADOPTED: "已采纳",
    IssueStatus.REJECTED: "已驳回",
    IssueStatus.IGNORED: "已忽略",
}
DRAWING_STATUS_TEXT = {
    DrawingStatus.UPLOADING: "上传中",
    DrawingStatus.UPLOADED: "已上传",
    DrawingStatus.PARSING: "解析中",
    DrawingStatus.PARSED: "解析完成",
    DrawingStatus.REVIEWING: "预审中",
    DrawingStatus.REVIEW_DONE: "预审完成",
    DrawingStatus.PARSE_FAILED: "解析失败",
    DrawingStatus.REVIEW_FAILED: "预审失败",
    DrawingStatus.ARCHIVED: "已归档",
}
RUN_STATUS_TEXT = {
    RunStatus.PENDING: "排队中",
    RunStatus.RUNNING: "运行中",
    RunStatus.WAITING_HUMAN: "等待人工",
    RunStatus.SUCCEEDED: "已成功",
    RunStatus.FAILED: "失败",
    RunStatus.RETURNED: "已退回",
    RunStatus.CANCELED: "已取消",
}
ISSUE_CATEGORY_TEXT = {
    IssueCategory.CODE: "规范符合性",
    IssueCategory.CONSISTENCY: "图纸一致性",
    IssueCategory.CLASH: "专业碰撞",
    IssueCategory.COMPLETENESS: "图纸完整性",
}
REVIEW_STAGE_TEXT = {
    ReviewStage.LOAD_CONTEXT: "正在读取项目资料",
    ReviewStage.PLANNING: "正在制定审查计划",
    ReviewStage.PARSING: "正在解析图纸",
    ReviewStage.RETRIEVING: "正在核对规范",
    ReviewStage.VALIDATING: "正在校验指标",
    ReviewStage.DETECTING: "正在归纳问题",
    ReviewStage.WAITING_RISK: "等待设计师确认风险",
    ReviewStage.REPORTING: "正在生成报告初稿",
    ReviewStage.WAITING_REPORT: "等待确认报告",
    ReviewStage.PACKAGING: "正在打包资料",
    ReviewStage.DONE: "预审完成",
}
INTERRUPT_TYPE_TEXT = {
    InterruptType.RISK_CONFIRM: "风险确认",
    InterruptType.REPORT_CONFIRM: "报告确认",
    InterruptType.SCHOOL_REVIEW: "校园评审",
}
LOG_TYPE_TEXT = {
    LogType.CREATED: "创建",
    LogType.STATUS_CHANGED: "状态变更",
    LogType.ASSIGNED: "指派",
    LogType.COMMENT: "评论",
    LogType.VERSION_LINKED: "关联版本",
    LogType.CLOSED: "关闭",
    LogType.DELETED: "作废",
}
# 消息中心列表的"类型"列（prd-04 §6.2）
NOTIFICATION_TYPE_TEXT = {
    NotificationType.REVIEW_COMPLETED: "AI 预审",
    NotificationType.REVIEW_FAILED: "AI 预审",
    NotificationType.ANNOTATION_ASSIGNED: "批注",
    NotificationType.ANNOTATION_SUBMITTED: "批注",
    NotificationType.ANNOTATION_CLOSED: "批注",
    NotificationType.MEMBER_ADDED: "项目",
    NotificationType.HITL_PENDING: "AI 预审",
    NotificationType.REPORT_READY: "AI 预审",
    NotificationType.SYSTEM: "系统",
}
REPORT_STATUS_TEXT = {
    ReportStatus.DRAFT: "草稿",
    ReportStatus.WAITING_CONFIRM: "待确认",
    ReportStatus.CONFIRMED: "已确认",
    ReportStatus.REJECTED: "已退回",
    ReportStatus.RETURNED: "已退回学生",
}
AUDIT_ACTION_TEXT = {
    AuditAction.USER_CREATE: "新建用户",
    AuditAction.USER_UPDATE: "编辑用户",
    AuditAction.USER_ROLE_UPDATE: "变更角色",
    AuditAction.USER_DISABLE: "禁用用户",
    AuditAction.USER_ENABLE: "启用用户",
    AuditAction.PASSWORD_RESET: "重置密码",
    AuditAction.MEMBER_ADD: "添加成员",
    AuditAction.MEMBER_REMOVE: "移除成员",
    AuditAction.PROJECT_DELETE: "删除项目",
    AuditAction.DRAWING_DELETE: "删除图纸",
    AuditAction.ANNOTATION_DELETE: "删除批注",
}
