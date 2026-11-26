"""批注状态机 + 流转权限矩阵（prd-03 §5.1 / §5.2 / §5.5.3）。

为什么要单独一个纯函数模块：
1. 状态机与权限矩阵是**规则**，不是流程；放在 Service 里会和 SQL 混在一起，
   既不好读也不好测（纯函数模块可以不起库直接断言）。
2. `allowed_actions`（前端按钮）与 `transition`（后端执行）必须共用同一套规则，
   否则会出现"按钮能点但点了 403"这种最招人烦的错位。
3. 文案（拒绝原因）集中一处，前端 tooltip 直接展示后端给的话，不各写一套。
"""
from __future__ import annotations

import uuid

from app.core.errors import Forbidden, InvalidStateTransition, ValidationFailed
from app.models.annotation import Annotation
from app.models.enums import ANNOTATION_STATUS_TEXT, AnnotationStatus, NotificationType
from app.models.user import User
from app.schemas.annotation import TransitionAction

# 动作 → 中文（用在"当前状态（待确认）不支持标记已修改"这类文案里）
ACTION_TEXT: dict[TransitionAction, str] = {
    TransitionAction.ISSUE: "下发",
    TransitionAction.MARK_MODIFIED: "标记已修改",
    TransitionAction.CLOSE: "复核通过",
    TransitionAction.REJECT_AND_REISSUE: "复核不通过并重新下发",
    TransitionAction.DELETE: "作废",
}

# 合法流转：action → {from_status: to_status}；to_status 为 None 表示软删除（不在状态枚举内）
LEGAL_TRANSITIONS: dict[TransitionAction, dict[AnnotationStatus, AnnotationStatus | None]] = {
    TransitionAction.ISSUE: {AnnotationStatus.PENDING_CONFIRM: AnnotationStatus.ISSUED},
    TransitionAction.MARK_MODIFIED: {AnnotationStatus.ISSUED: AnnotationStatus.MODIFIED},
    TransitionAction.CLOSE: {AnnotationStatus.MODIFIED: AnnotationStatus.CLOSED},
    TransitionAction.REJECT_AND_REISSUE: {AnnotationStatus.MODIFIED: AnnotationStatus.ISSUED},
    TransitionAction.DELETE: {
        AnnotationStatus.PENDING_CONFIRM: None,
        AnnotationStatus.ISSUED: None,
        AnnotationStatus.MODIFIED: None,
        AnnotationStatus.CLOSED: None,
    },
}

# 流转成功要发的通知类型（prd-04 §4）。DELETE 不发：作废不该打扰任何人。
ACTION_EVENT_TYPE: dict[TransitionAction, NotificationType | None] = {
    TransitionAction.ISSUE: NotificationType.ANNOTATION_ASSIGNED,
    # 复核不通过 → 重新变回"已下发"，责任人要重新处理，语义等同一次下发
    TransitionAction.REJECT_AND_REISSUE: NotificationType.ANNOTATION_ASSIGNED,
    TransitionAction.MARK_MODIFIED: NotificationType.ANNOTATION_SUBMITTED,
    TransitionAction.CLOSE: NotificationType.ANNOTATION_CLOSED,
    TransitionAction.DELETE: None,
}

REJECT_REASON_MIN = 10
REJECT_REASON_MAX = 500


# ---------------------------------------------------------------------------
# 功能权限之外的数据权限（prd-03 §5.5.3 范式）
# ---------------------------------------------------------------------------
def ensure_action_allowed(
    ann: Annotation, user: User, action: TransitionAction, admin: bool
) -> None:
    """动作与身份的关系。返回 None = 允许；否则抛 403 并给出**能看懂的原因**。"""
    is_creator = ann.creator_id == user.id
    is_assignee = ann.assignee_id == user.id

    match action:
        case TransitionAction.ISSUE:
            ok = is_creator or admin
            reason = "只有创建者可以下发该批注"
        case TransitionAction.MARK_MODIFIED:
            # 本节最关键的一条：责任人专属，不能由创建者代劳（否则复核闭环失效）
            ok = is_assignee
            reason = "仅责任人可标记已修改"
        case TransitionAction.CLOSE | TransitionAction.REJECT_AND_REISSUE:
            ok = is_creator or admin
            reason = "只有创建者可以复核该批注"
        case TransitionAction.DELETE:
            if ann.status is AnnotationStatus.CLOSED:
                ok = admin
                reason = "已关闭的批注仅管理员可删除"
            else:
                ok = is_creator or admin
                reason = "只有创建者可以作废该批注"
        case _:  # pragma: no cover - 枚举已封闭
            ok = False
            reason = "不支持的操作"

    if not ok:
        raise Forbidden(
            reason,
            detail={
                "action": action.value,
                "status": ann.status.value,
                "assignee_id": str(ann.assignee_id) if ann.assignee_id else None,
            },
        )


def ensure_transition_legal(status: AnnotationStatus, action: TransitionAction) -> None:
    """状态机合法性：非法 → 422 INVALID_STATE_TRANSITION（不是 409）。

    409 语义是"你看到的是旧数据，刷新再来"，422 是"这个动作在当前状态根本不成立"。
    混用会让前端无法区分"重试"还是"这个按钮不该出现"。
    """
    if action in LEGAL_TRANSITIONS and status in LEGAL_TRANSITIONS[action]:
        return
    raise InvalidStateTransition(
        f"当前状态（{ANNOTATION_STATUS_TEXT[status]}）不支持{ACTION_TEXT[action]}",
        detail={"from": status.value, "action": action.value},
    )


def next_status(status: AnnotationStatus, action: TransitionAction) -> AnnotationStatus | None:
    return LEGAL_TRANSITIONS[action][status]


def ensure_action_fields(
    action: TransitionAction,
    *,
    assignee_id: uuid.UUID | None,
    reject_reason: str | None,
) -> None:
    """附加字段校验（prd-03 §5.2 的"附加要求"列）。"""
    if action is TransitionAction.ISSUE and assignee_id is None:
        raise ValidationFailed("下发批注必须指定责任人", detail={"field": "assignee_id"})
    if action is TransitionAction.REJECT_AND_REISSUE:
        reason = (reject_reason or "").strip()
        if len(reason) < REJECT_REASON_MIN:
            raise ValidationFailed(
                f"复核不通过必须填写原因（不少于 {REJECT_REASON_MIN} 字）",
                detail={"field": "reject_reason", "min_length": REJECT_REASON_MIN},
            )
        if len(reason) > REJECT_REASON_MAX:
            raise ValidationFailed(
                f"复核原因不能超过 {REJECT_REASON_MAX} 字",
                detail={"field": "reject_reason", "max_length": REJECT_REASON_MAX},
            )


# ---------------------------------------------------------------------------
# 通知事件快照
# ---------------------------------------------------------------------------
def event_payload(
    ann: Annotation,
    *,
    action: TransitionAction,
    operator: User,
    creator_name: str,
    assignee_id: uuid.UUID | None,
    assignee_name: str | None,
    drawing_name: str,
    close_remark: str | None,
) -> dict:
    """渲染消息所需的**全部快照**（prd-04 §9.2：不依赖后续再查库）。

    名字一律在这里定格：事件可能秒级后才被消费，那时批注标题/指派人可能已经变了。
    `assignee_id/assignee_name` 必须由调用方显式传入新值 —— "条件更新"执行后
    ORM 里的 `ann.assignee_id` 仍是旧值，直接读会把消息发给上一个人。
    """
    return {
        "annotation_id": str(ann.id),
        "title": ann.title,
        "project_id": str(ann.project_id),
        "drawing_id": str(ann.drawing_id),
        "drawing_name": drawing_name,
        "creator_id": str(ann.creator_id),
        "creator_name": creator_name,
        "assignee_id": str(assignee_id) if assignee_id else None,
        "assignee_name": assignee_name,
        "operator_id": str(operator.id),
        "operator_name": operator.name,
        "severity_text": _severity_text(ann),
        "action": action.value,
        "close_remark": close_remark,
    }


def _severity_text(ann: Annotation) -> str:
    from app.models.enums import SEVERITY_TEXT

    return SEVERITY_TEXT.get(ann.severity, str(getattr(ann.severity, "value", ann.severity)))
