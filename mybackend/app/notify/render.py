"""事件 → 消息文案（prd-04 §4 通知类型矩阵的唯一实现）。

两条原则：
1. **渲染只读 payload**：`domain_events.payload` 是"渲染所需全部快照"，
   消费者不许回头查批注/图纸/项目 —— 否则事件重试时数据已经变了，
   用户看到的消息内容与事件语义不符（"补发昨天的通知，文案却是今天的标题"）。
2. **文案只在这一个文件里**：标题模板、摘要模板、deeplink 拼接三件事集中，
   前端联调时只需要对照本文件，不用满仓库找 f-string。

例外只有一处：`HITL_PENDING` / `REPORT_READY` 的接收人是"项目内审图角色"这种
**目录型**信息（不是业务状态），允许由 dispatcher 反查一次，且只查成员表。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.models.enums import BizType, NotificationType
from app.models.system import DomainEvent

# 不通知自己的例外（prd-04 §4）：这两类消息的作用是"流程卡着等你"，跳过了流程就挂死
SELF_NOTIFY_ALLOWED: frozenset[NotificationType] = frozenset(
    {NotificationType.HITL_PENDING, NotificationType.REPORT_READY}
)

# 批量合并规则（prd-04 §4）：HITL_PENDING 不参与合并 —— 合并会丢掉 interrupt_id
MERGEABLE: frozenset[NotificationType] = frozenset(
    {
        NotificationType.ANNOTATION_ASSIGNED,
        NotificationType.ANNOTATION_SUBMITTED,
        NotificationType.ANNOTATION_CLOSED,
    }
)

MERGE_TEMPLATE: dict[NotificationType, tuple[str, str]] = {
    # (合并后标题模板, 合并后 deeplink)
    NotificationType.ANNOTATION_ASSIGNED: (
        "{n} 条批注待处理",
        "/annotations?assignee=me&status=ISSUED",
    ),
    NotificationType.ANNOTATION_SUBMITTED: (
        "{n} 条批注待复核",
        "/annotations?status=MODIFIED",
    ),
    NotificationType.ANNOTATION_CLOSED: (
        "{n} 条批注已关闭",
        "/annotations?status=CLOSED",
    ),
}

# 从"单条标题"里剥出批注标题（合并摘要要列举前 2 条标题）
SINGLE_TITLE_PREFIX: dict[NotificationType, str] = {
    NotificationType.ANNOTATION_ASSIGNED: "新批注待处理：",
    NotificationType.ANNOTATION_SUBMITTED: "批注待复核：",
    NotificationType.ANNOTATION_CLOSED: "批注已关闭：",
}

DISPLAY_TYPE = "批注"


@dataclass
class Rendered:
    """一条事件渲染出的"消息内容 + 收件人清单"。"""

    title: str
    content: str
    biz_type: BizType
    biz_id: uuid.UUID | None
    deeplink: str | None
    recipients: list[uuid.UUID] = field(default_factory=list)
    type: NotificationType | None = None
    project_id: uuid.UUID | None = None
    mergeable: bool = False


def safe_deeplink(path: str | None) -> str | None:
    """只放行站内绝对路径（prd-04 §7：防开放重定向）。

    契约要求"必须以 / 开头且不含 ://"，这里在**下发前**拦一次，
    前端再拦一次 —— 两侧都做，因为 deeplink 最终来自可配置的 payload。
    """
    if not path:
        return None
    if not path.startswith("/") or "://" in path:
        return None
    return path


def _u(value) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _s(value, default: str = "") -> str:
    return str(value) if value not in (None, "") else default


def annotation_deeplink(payload: dict) -> str | None:
    pid, did, aid = payload.get("project_id"), payload.get("drawing_id"), payload.get("annotation_id")
    if not (pid and did and aid):
        return None
    return safe_deeplink(f"/projects/{pid}/drawings/{did}?annotation={aid}&focus=1")


def review_deeplink(payload: dict, *, suffix: str = "") -> str | None:
    pid = payload.get("project_id")
    did = payload.get("drawing_id")
    tid = payload.get("review_task_id")
    if not (pid and did and tid):
        return None
    return safe_deeplink(f"/projects/{pid}/drawings/{did}/review/{tid}{suffix}")


def _recipients(etype: NotificationType, payload: dict) -> list[uuid.UUID]:
    """按 prd-04 §4 的"接收人"列解析，去重保序。"""
    if etype is NotificationType.REVIEW_COMPLETED or etype is NotificationType.REVIEW_FAILED:
        raw = [payload.get("creator_id")]
    elif etype is NotificationType.ANNOTATION_ASSIGNED:
        raw = [payload.get("assignee_id")]
    elif etype is NotificationType.ANNOTATION_SUBMITTED:
        raw = [payload.get("creator_id")]
    elif etype is NotificationType.ANNOTATION_CLOSED:
        raw = [payload.get("creator_id"), payload.get("assignee_id")]
    elif etype is NotificationType.MEMBER_ADDED:
        raw = [payload.get("user_id")]
    else:  # HITL_PENDING / REPORT_READY / SYSTEM 走显式名单
        raw = list(payload.get("recipient_ids") or [])

    out: list[uuid.UUID] = []
    for item in raw:
        uid = _u(item)
        if uid is not None and uid not in out:
            out.append(uid)
    return out


def render_event(
    event: DomainEvent, fallback_recipients: list[uuid.UUID] | None = None
) -> Rendered | None:
    """把一条 domain_event 渲染成消息；无法识别的事件类型返回 None（跳过，不算失败）。"""
    try:
        etype = NotificationType(event.event_type)
    except ValueError:
        return None

    p = event.payload or {}
    title = content = ""
    biz_type = BizType.SYSTEM
    biz_id: uuid.UUID | None = None
    deeplink: str | None = None

    match etype:
        case NotificationType.REVIEW_COMPLETED:
            biz_type, biz_id = BizType.REVIEW_TASK, _u(p.get("review_task_id"))
            title = f"预审完成：{_s(p.get('drawing_name'), '图纸')}"
            content = (
                f"发现 {int(p.get('total') or 0)} 个问题，"
                f"其中严重 {int(p.get('blocker') or 0)} 个"
            )
            deeplink = review_deeplink(p)

        case NotificationType.REVIEW_FAILED:
            biz_type, biz_id = BizType.REVIEW_TASK, _u(p.get("review_task_id"))
            title = f"预审失败：{_s(p.get('drawing_name'), '图纸')}"
            content = f"{_s(p.get('error_reason'), '引擎异常')}，请重试"
            deeplink = review_deeplink(p)

        case NotificationType.ANNOTATION_ASSIGNED:
            biz_type, biz_id = BizType.ANNOTATION, _u(p.get("annotation_id"))
            title = f"新批注待处理：{_s(p.get('title'), '未命名批注')}"
            content = (
                f"{_s(p.get('creator_name'), '同事')} 在 "
                f"{_s(p.get('drawing_name'), '图纸')} 下发："
                f"{_s(p.get('severity_text'), '一般')}"
            )
            deeplink = annotation_deeplink(p)

        case NotificationType.ANNOTATION_SUBMITTED:
            biz_type, biz_id = BizType.ANNOTATION, _u(p.get("annotation_id"))
            title = f"批注待复核：{_s(p.get('title'), '未命名批注')}"
            content = f"{_s(p.get('assignee_name'), '责任人')} 已标记修改，请复核"
            deeplink = annotation_deeplink(p)

        case NotificationType.ANNOTATION_CLOSED:
            biz_type, biz_id = BizType.ANNOTATION, _u(p.get("annotation_id"))
            title = f"批注已关闭：{_s(p.get('title'), '未命名批注')}"
            content = f"{_s(p.get('operator_name'), '同事')} 复核通过"
            deeplink = annotation_deeplink(p)

        case NotificationType.MEMBER_ADDED:
            biz_type, biz_id = BizType.PROJECT, _u(p.get("project_id"))
            title = f"你已加入项目：{_s(p.get('project_name'), '项目')}"
            content = f"{_s(p.get('operator_name'), '管理员')} 邀请你参与该项目"
            deeplink = safe_deeplink(f"/projects/{p.get('project_id')}") if p.get("project_id") else None

        case NotificationType.HITL_PENDING:
            # biz_type 复用 ANNOTATION，biz_id = interrupt_id（prd-04 §4 明确要求）
            biz_type, biz_id = BizType.ANNOTATION, _u(p.get("interrupt_id"))
            title = f"预审已暂停，等你确认：{_s(p.get('drawing_name'), '图纸')}"
            content = f"{int(p.get('risk_count') or 0)} 项风险待确认，24 小时内有效"
            deeplink = review_deeplink(
                p, suffix=f"?interrupt={p.get('interrupt_id')}" if p.get("interrupt_id") else ""
            )

        case NotificationType.REPORT_READY:
            biz_type, biz_id = BizType.ANNOTATION, _u(p.get("interrupt_id"))
            title = f"报告初稿已生成：{_s(p.get('drawing_name'), '图纸')}"
            content = f"共 {int(p.get('issue_total') or 0)} 个问题，请确认是否输出正式文档"
            deeplink = review_deeplink(p, suffix="/report")

        case NotificationType.SYSTEM:
            title = _s(p.get("title"), "系统通知")
            content = _s(p.get("content"), "")
            biz_type = BizType.SYSTEM
            biz_id = _u(p.get("biz_id"))
            deeplink = safe_deeplink(p.get("deeplink"))

    if not title:
        return None

    recipients = _recipients(etype, p)
    if not recipients and etype in SELF_NOTIFY_ALLOWED and fallback_recipients:
        recipients = list(fallback_recipients)

    return Rendered(
        title=title,
        content=content,
        biz_type=biz_type,
        biz_id=biz_id,
        deeplink=deeplink,
        recipients=recipients,
        type=etype,
        project_id=_u(p.get("project_id")),
        mergeable=etype in MERGEABLE,
    )


def merge_title(etype: NotificationType, count: int) -> str:
    template, _ = MERGE_TEMPLATE[etype]
    return template.format(n=count)


def merge_deeplink(etype: NotificationType) -> str:
    _, link = MERGE_TEMPLATE[etype]
    return link


def merge_content(etype: NotificationType, samples: list[str], count: int) -> str:
    """摘要：前 2 条标题 + "等 N 条"（prd-04 §4）。"""
    shown = [s for s in samples if s][:2]
    head = "、".join(shown)
    if not head:
        return f"共 {count} 条{DISPLAY_TYPE}等待处理"
    return f"{head} 等 {count} 条"


def sample_title(etype: NotificationType, title: str) -> str:
    """从单条消息标题里剥出批注标题，供合并摘要使用。"""
    prefix = SINGLE_TITLE_PREFIX.get(etype)
    if prefix and title.startswith(prefix):
        return title[len(prefix) :].strip()
    return title.strip()
