"""统一导出所有 ORM 模型：Alembic autogenerate 依赖这里 import 全部表。"""
from app.models.agent import AgentEvent, AgentStateCheckpoint, InterruptTask
from app.models.annotation import Annotation, AnnotationComment, AnnotationStatusLog
from app.models.base import Base, JSONType, enum_col
from app.models.drawing import Drawing, UploadSession
from app.models.notification import Notification, NotificationPreference
from app.models.project import Project, ProjectMember
from app.models.review import IssueFeedback, ReportDraft, ReviewIssue, ReviewTask
from app.models.system import AuditLog, DomainEvent
from app.models.user import Permission, Role, RolePermission, User, UserRole

__all__ = [
    "Base",
    "JSONType",
    "enum_col",
    # user
    "User",
    "Role",
    "Permission",
    "UserRole",
    "RolePermission",
    # project
    "Project",
    "ProjectMember",
    # drawing
    "Drawing",
    "UploadSession",
    # review
    "ReviewTask",
    "ReviewIssue",
    "IssueFeedback",
    "ReportDraft",
    # annotation
    "Annotation",
    "AnnotationComment",
    "AnnotationStatusLog",
    # notification
    "Notification",
    "NotificationPreference",
    # agent
    "AgentStateCheckpoint",
    "InterruptTask",
    "AgentEvent",
    # system
    "DomainEvent",
    "AuditLog",
]
