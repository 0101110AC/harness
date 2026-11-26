"""审计日志读写（prd-05 §8）。

**写入时机是这一层最关键的决策**：`record()` 只 `db.add()`，不 commit。
审计行与业务变更在同一个事务里提交，语义是"要么都成功，要么都不留痕"。

为什么不采用"事务提交后另开 session 补写"：
- 业务 commit 成功后进程被杀 / 连接断开，补写就丢了 —— 最常见的是审计里缺一条
  "项目已删"，而项目真的没了；
- 反过来，如果补写失败还重试，会出现业务回滚了但审计写着"删除成功"的幽灵记录。

代价是审计表写入失败会连带业务失败（例如磁盘满）。对"用户管理 / 删除"这类
低频高危操作，这个代价是对的：操作没有留痕比操作失败严重得多。

脱敏：detail 里任何键名包含 password/token/hash/secret 的字段一律丢弃，
在入口处白名单化比在每个调用点自觉更可靠。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.request_context import get_client_ip, get_user_agent
from app.models.enums import AUDIT_ACTION_TEXT, AuditAction
from app.models.system import AuditLog
from app.models.user import User
from app.schemas.common import Page
from app.schemas.system import AuditLogOut
from app.schemas.auth import UserBrief

_SENSITIVE_KEY_PARTS = ("password", "token", "hash", "secret")


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_value(item)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in _SENSITIVE_KEY_PARTS)
        }
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _sanitize(detail: dict[str, Any] | None) -> dict[str, Any] | None:
    if not detail:
        return None
    safe = _sanitize_value(detail)
    return safe or None


def record(
    db: AsyncSession,
    *,
    action: AuditAction | str,
    target_type: str,
    user: User | None,
    target_id: uuid.UUID | None = None,
    target_name: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditLog:
    """在当前事务里追加一条审计记录（不 commit，由调用方统一提交）。"""
    log = AuditLog(
        user_id=user.id if user else None,
        action=action.value if isinstance(action, AuditAction) else action,
        target_type=target_type,
        target_id=target_id,
        target_name=(target_name or "")[:200] or None,
        detail=_sanitize(detail),
        ip=get_client_ip(),
        user_agent=get_user_agent(),
        # SQLite 的 CURRENT_TIMESTAMP 只有秒精度，同一秒内的多条管理操作会失去先后。
        # 显式写入 UTC（含微秒），查询排序才有稳定语义。
        created_at=datetime.now(timezone.utc),
    )
    db.add(log)
    return log


def _to_out(log: AuditLog) -> AuditLogOut:
    return AuditLogOut(
        id=log.id,
        user=UserBrief.model_validate(log.user) if log.user else None,
        action=log.action,
        action_text=AUDIT_ACTION_TEXT.get(log.action, log.action),
        target_type=log.target_type,
        target_id=log.target_id,
        target_name=log.target_name,
        detail=log.detail,
        ip=log.ip,
        created_at=log.created_at,
    )


async def list_audit_logs(
    db: AsyncSession,
    *,
    page: int,
    page_size: int,
    action: str | None = None,
    user_id: uuid.UUID | None = None,
    target_type: str | None = None,
    start=None,
    end=None,
) -> Page[AuditLogOut]:
    conditions = []
    if action:
        conditions.append(AuditLog.action == action)
    if user_id:
        conditions.append(AuditLog.user_id == user_id)
    if target_type:
        conditions.append(AuditLog.target_type == target_type)
    if start is not None:
        conditions.append(AuditLog.created_at >= start)
    if end is not None:
        conditions.append(AuditLog.created_at <= end)

    total = (
        await db.execute(select(func.count()).select_from(AuditLog).where(*conditions))
    ).scalar_one()

    rows = (
        await db.execute(
            select(AuditLog)
            .options(joinedload(AuditLog.user))
            .where(*conditions)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    return Page[AuditLogOut](
        items=[_to_out(log) for log in rows],
        total=total,
        page=page,
        page_size=page_size,
        pages=(total + page_size - 1) // page_size,
    )
