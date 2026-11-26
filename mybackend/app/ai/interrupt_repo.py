"""中断工单的数据库操作（`interrupt_tasks`，prd-07 §3.21）。

单独成模块是为了打断循环依赖：
`runtime → graph → nodes → interrupt_repo`，而 `hitl → runtime`；
如果把仓库层和编排层写在同一个文件里，`nodes ↔ hitl` 就会成环。

三条硬约束（prd-06 §8.2 / §12.5）：
1. 工单必须在 `interrupt()` **之前**落库并推事件 —— 否则挂起后没人知道要处理什么；
2. 恢复用**条件更新**（`WHERE status IN ('PENDING','VIEWED')`）保证并发只有一个赢家；
3. 同一 thread 同时只允许 1 条 PENDING（DB 部分唯一索引兜底）。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.agent import InterruptTask
from app.models.enums import InterruptStatus, InterruptType, ReviewStage, ReviewTaskStatus, RunStatus
from app.models.review import ReviewTask
from app.notify.outbox import add_domain_event

log = logging.getLogger(__name__)

OPEN_STATUSES = (InterruptStatus.PENDING, InterruptStatus.VIEWED)


def interrupt_uuid(thread_id: str, itype: str, plan_revision: int) -> uuid.UUID:
    """确定性 interrupt_id：同一个 (thread, 卡点, 重规划轮次) 永远同一个 id。

    这是"节点重放"的安全阀：`interrupt()` 恢复后整个节点会**从头再执行一遍**，
    如果每次生成随机 id，就会插入一条重复工单 + 重复推送 risk_interrupt 事件。
    """
    return uuid.uuid5(uuid.NAMESPACE_URL, f"arch-harness:{thread_id}:{itype}:{plan_revision}")


async def open_interrupt(
    *,
    interrupt_id: uuid.UUID,
    thread_id: str,
    run_id: str,
    project_id: uuid.UUID,
    drawing_id: uuid.UUID,
    review_task_id: uuid.UUID,
    itype: InterruptType,
    title: str,
    payload: dict[str, Any],
    expires_at: datetime,
    stage: ReviewStage | None = None,
    notify: dict[str, Any] | None = None,
) -> tuple[bool, datetime]:
    """落一条 PENDING 工单；返回 (是否本次新建, 过期时间)。

    幂等：同 id 已存在直接返回 created=False —— 节点重放不会重复建单。

    `notify` 是"同一事务里要排的站内信事件"（prd-04 §9.1）。为什么不放到节点里另开
    事务：工单落库与事件落库必须同生共死 —— 工单在、事件丢，用户永远等不到提醒，
    流程就静默挂死了。
    """
    async with AsyncSessionLocal() as db:
        existing = await db.get(InterruptTask, interrupt_id)
        if existing is not None:
            return False, existing.expires_at

        db.add(
            InterruptTask(
                id=interrupt_id,
                thread_id=thread_id,
                run_id=run_id,
                project_id=project_id,
                drawing_id=drawing_id,
                review_task_id=review_task_id,
                type=itype,
                status=InterruptStatus.PENDING,
                title=title,
                payload=payload,
                expires_at=expires_at,
            )
        )
        values: dict[str, Any] = {
            "run_status": RunStatus.WAITING_HUMAN,
            "status": ReviewTaskStatus.REVIEWING,
            "waiting_interrupt_id": interrupt_id,
        }
        if stage is not None:
            values["stage"] = stage
        await db.execute(update(ReviewTask).where(ReviewTask.id == review_task_id).values(**values))

        if notify is not None:
            await add_domain_event(
                db,
                event_type=notify["event_type"],
                aggregate_type="REVIEW_TASK",
                aggregate_id=review_task_id,
                # 幂等键的 version 用确定性 interrupt_id：同一 (thread, 卡点, 重规划轮次)
                # 只会产生一条提醒，节点重放不会给处理人发第二遍
                version=str(interrupt_id),
                payload=notify["payload"],
                project_id=project_id,
                actor_id=None,
            )

        try:
            await db.commit()
        except IntegrityError:
            # 并发下同一 thread 的第二条 PENDING 会撞部分唯一索引：让先到的那条生效
            await db.rollback()
            log.warning("open_interrupt: 并发建单冲突，保留已有 PENDING thread=%s", thread_id)
            return False, expires_at

        return True, expires_at


async def get_by_id(db: AsyncSession, interrupt_id: uuid.UUID) -> InterruptTask | None:
    return await db.get(InterruptTask, interrupt_id)


async def get_pending_for_drawing(
    db: AsyncSession, drawing_id: uuid.UUID
) -> InterruptTask | None:
    return (
        await db.execute(
            select(InterruptTask)
            .where(
                InterruptTask.drawing_id == drawing_id,
                InterruptTask.status.in_(OPEN_STATUSES),
            )
            .order_by(InterruptTask.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_pending_for_thread(db: AsyncSession, thread_id: str) -> InterruptTask | None:
    return (
        await db.execute(
            select(InterruptTask)
            .where(
                InterruptTask.thread_id == thread_id,
                InterruptTask.status.in_(OPEN_STATUSES),
            )
            .order_by(InterruptTask.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def resolve_if_pending(
    db: AsyncSession,
    interrupt_id: uuid.UUID,
    *,
    resolved_by: uuid.UUID,
    resolution: str,
    item_decisions: Sequence[dict[str, Any]] | None = None,
    remark: str | None = None,
) -> bool:
    """条件更新：状态从 PENDING/VIEWED → RESOLVED，返回是否抢到。

    `RETURNING id` + `rowcount` 是唯一的并发正确性来源；
    先 SELECT 再 UPDATE 的写法在两人同时点"提交"时会双写两次 resume。
    """
    result = await db.execute(
        update(InterruptTask)
        .where(
            InterruptTask.id == interrupt_id,
            InterruptTask.status.in_(OPEN_STATUSES),
        )
        .values(
            status=InterruptStatus.RESOLVED,
            resolved_by=resolved_by,
            resolution=resolution,
            item_decisions=list(item_decisions) if item_decisions is not None else None,
            remark=remark,
            resolved_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return bool(result.rowcount)


async def claim_expired(limit: int = 50) -> list[dict[str, Any]]:
    """把过期的 PENDING/VIEWED 批量置 EXPIRED，返回明细（供超时扫描善后）。

    为什么要"先选 id 再按 id 更新"：SQLAlchemy 的 UPDATE 不支持 `.limit()`（PG 可以，
    SQLite 不行），而 RETURNING + LIMIT 需要方言特定的子查询。分两步写，
    第二步仍然保留 `status IN OPEN` 条件 —— 并发时被人工抢先 resolve 的工单不会被改。
    """
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        ids = (
            await db.execute(
                select(InterruptTask.id)
                .where(
                    InterruptTask.status.in_(OPEN_STATUSES),
                    InterruptTask.expires_at < now,
                )
                .order_by(InterruptTask.expires_at)
                .limit(limit)
            )
        ).scalars().all()

        if not ids:
            return []

        rows = (
            await db.execute(
                update(InterruptTask)
                .where(
                    InterruptTask.id.in_(ids),
                    InterruptTask.status.in_(OPEN_STATUSES),
                )
                .values(status=InterruptStatus.EXPIRED, resolved_at=now)
                .returning(
                    InterruptTask.id,
                    InterruptTask.thread_id,
                    InterruptTask.run_id,
                    InterruptTask.review_task_id,
                    InterruptTask.drawing_id,
                    InterruptTask.type,
                )
            )
        ).all()
        await db.commit()

    return [
        {
            "id": row[0],
            "thread_id": row[1],
            "run_id": row[2],
            "review_task_id": row[3],
            "drawing_id": row[4],
            "type": row[5].value if hasattr(row[5], "value") else str(row[5]),
        }
        for row in rows
    ]


async def default_expiry(hours: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)
