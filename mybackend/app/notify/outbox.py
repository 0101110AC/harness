"""Outbox 模式：领域事件与业务数据同事务落库，消费者异步拉取（prd-04 §9.1~§9.2、prd-07 §8 第 3 条）。

为什么不用进程内 `asyncio.create_task(dispatch())`：
1. 业务事务提交失败时消息已经发出去了 → 用户看到一条"批注不存在"的通知；
2. 进程重启时队列里的消息蒸发 → "业务成功 ⇒ 消息一定送达"直接破功；
3. 无法多实例消费，也无法重放。
把事件当成**业务数据的一部分**写进同一张表，才是"要么都成功、要么都不发生"。

两条硬约束：
- `event_key = {event_type}:{aggregate_id}:{version}` 唯一：重放/重试不产生重复消息；
- 消费者必须"先认领、再处理、再落终态"，认领是条件更新，多实例只有一个赢家。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.database import AsyncSessionLocal
from app.models.enums import EventStatus
from app.models.system import DomainEvent

log = logging.getLogger(__name__)

PENDING = EventStatus.PENDING
PROCESSING = EventStatus.PROCESSING
PROCESSED = EventStatus.PROCESSED
FAILED = EventStatus.FAILED

# 指数退避（prd-04 §9.2）：+1s / +4s / +16s
BACKOFF_SECONDS = (1, 4, 16)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_event_key(event_type: str, aggregate_id: uuid.UUID | str, version: int | str) -> str:
    """幂等键：同一聚合的同一版本只会有一条事件。

    `version` 刻意用"聚合版本"（批注的 row_version、中断的确定性 id），
    而不是时间戳或 uuid4 —— 后者在"节点重放/请求重试"场景下会让幂等失效。
    """
    return f"{event_type}:{aggregate_id}:{version}"


async def add_domain_event(
    db: AsyncSession,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    version: int | str,
    payload: dict[str, Any],
    project_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
) -> bool:
    """在**调用方的事务里**插一条 PENDING 事件；返回是否真的插入。

    注意这里不 commit：调用方业务写入与事件必须同生共死。
    重复键用 SAVEPOINT 兜住 —— 直接让 IntegrityError 冒到外层会把整个业务事务带崩。
    """
    key = make_event_key(event_type, aggregate_id, version)
    exists = (
        await db.execute(select(DomainEvent.id).where(DomainEvent.event_key == key))
    ).scalar_one_or_none()
    if exists is not None:
        return False

    try:
        async with db.begin_nested():
            db.add(
                DomainEvent(
                    event_key=key,
                    event_type=event_type,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    project_id=project_id,
                    actor_id=actor_id,
                    payload=payload,
                    status=PENDING,
                )
            )
    except IntegrityError:
        # 并发下同一 key 被另一个请求抢先插入：视作已入队，不算失败
        log.debug("domain_event 已存在，跳过 key=%s", key)
        return False
    return True


class OutboxConsumer:
    """后台协程：100ms 一轮，认领 → 分发 → 落终态。

    并发安全的三层：
    1. `SELECT ... FOR UPDATE SKIP LOCKED`（PG）让多实例各拿各的，不会互相等锁；
    2. 认领用条件更新 `WHERE status=PENDING`，赢得更新的一方才处理；
    3. `claimed_at` 租约：进程崩在 PROCESSING 的事件会被回收重试（否则永远卡住）。
    """

    def __init__(self, poll_ms: int | None = None, batch: int | None = None) -> None:
        self.poll_interval = (poll_ms if poll_ms is not None else settings.OUTBOX_POLL_MS) / 1000
        self.batch = batch or settings.OUTBOX_BATCH
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._dispatcher = None

    # ---------------- 生命周期 ----------------
    async def startup(self) -> None:
        if not settings.OUTBOX_ENABLED:
            log.info("OutboxConsumer 已关闭（OUTBOX_ENABLED=false）")
            return
        # 上一轮进程崩溃遗留的 PROCESSING 先回收，再开始消费
        recovered = await self.recover_stale()
        if recovered:
            log.warning("Outbox 启动对账：%d 条 PROCESSING 事件租约过期，已重新入队", recovered)
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="outbox-consumer")
        log.info("OutboxConsumer 已启动（poll=%dms, batch=%d）", self.poll_interval * 1000, self.batch)

    async def shutdown(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - 关闭阶段不关心原因
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.run_once()
                if processed == 0:
                    # 空转时退到轮询间隔；有活干就立刻继续，避免"每轮多睡 100ms"
                    await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 兜底 catch 绝不能吞掉信号：D4 复盘 2.4 就是这么静默失败了一整轮
                log.exception("Outbox 消费异常（不影响主流程，下轮继续）")
                await asyncio.sleep(self.poll_interval * 5)

    # ---------------- 单个事件 ----------------
    async def run_once(self) -> int:
        claimed = await self._claim(self.batch)
        for event_id in claimed:
            await self._process(event_id)
        return len(claimed)

    async def _claim(self, limit: int) -> list[uuid.UUID]:
        now = _now()
        async with AsyncSessionLocal() as db:
            stmt = (
                select(DomainEvent.id)
                .where(
                    DomainEvent.status == PENDING,
                    or_(DomainEvent.next_retry_at.is_(None), DomainEvent.next_retry_at <= now),
                )
                .order_by(DomainEvent.created_at.asc())
                .limit(limit)
            )
            if not settings.is_sqlite:
                # SQLite 没有行级锁，也没有 SKIP LOCKED；单机场景靠下面的条件更新兜
                stmt = stmt.with_for_update(skip_locked=True)
            candidates = list((await db.execute(stmt)).scalars().all())
            if not candidates:
                await db.commit()
                return []

            result = await db.execute(
                update(DomainEvent)
                .where(DomainEvent.id.in_(candidates), DomainEvent.status == PENDING)
                .values(status=PROCESSING, claimed_at=now)
            )
            await db.commit()
            if (result.rowcount or 0) == 0:
                return []

            # 只认"我这一轮认领成功"的行：并行实例可能抢先改走了其中几条
            mine = await db.execute(
                select(DomainEvent.id).where(
                    DomainEvent.id.in_(candidates),
                    DomainEvent.status == PROCESSING,
                    DomainEvent.claimed_at == now,
                )
            )
            return list(mine.scalars().all())

    async def _process(self, event_id: uuid.UUID) -> None:
        from app.notify.dispatcher import get_notification_service

        async with AsyncSessionLocal() as db:
            event = await db.get(DomainEvent, event_id)
            if event is None or event.status is not PROCESSING:
                return
            try:
                result = await get_notification_service().dispatch(event)
                event.status = PROCESSED
                event.processed_at = _now()
                event.claimed_at = None
                event.error_message = None
                await db.commit()
                log.debug(
                    "outbox processed key=%s delivered=%d skipped=%d",
                    event.event_key, result.delivered, result.skipped,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 任何失败都要落库，靠重试兜
                await db.rollback()
                await self._mark_failed_or_retry(event_id, exc)

    async def _mark_failed_or_retry(self, event_id: uuid.UUID, exc: Exception) -> None:
        async with AsyncSessionLocal() as db:
            event = await db.get(DomainEvent, event_id)
            if event is None:
                return
            event.retry_count = (event.retry_count or 0) + 1
            event.error_message = f"{type(exc).__name__}: {exc}"[:2000]
            event.claimed_at = None
            if event.retry_count >= settings.OUTBOX_MAX_RETRY:
                event.status = FAILED
                event.processed_at = _now()
                log.error(
                    "outbox 事件置 FAILED key=%s retry=%d err=%s",
                    event.event_key, event.retry_count, event.error_message,
                )
            else:
                backoff = BACKOFF_SECONDS[min(event.retry_count - 1, len(BACKOFF_SECONDS) - 1)]
                event.status = PENDING
                event.next_retry_at = _now() + timedelta(seconds=backoff)
                log.warning(
                    "outbox 事件重试 key=%s retry=%d backoff=%ds err=%s",
                    event.event_key, event.retry_count, backoff, event.error_message,
                )
            await db.commit()

    async def recover_stale(self) -> int:
        """把租约过期的 PROCESSING 事件放回 PENDING。"""
        now = _now()
        cutoff = now - timedelta(seconds=settings.OUTBOX_LEASE_SECONDS)
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                update(DomainEvent)
                .where(
                    DomainEvent.status == PROCESSING,
                    or_(DomainEvent.claimed_at.is_(None), DomainEvent.claimed_at < cutoff),
                )
                .values(status=PENDING, claimed_at=None, next_retry_at=now)
            )
            await db.commit()
            return result.rowcount or 0


consumer = OutboxConsumer()


async def enqueue(
    db: AsyncSession,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    version: int | str,
    payload: dict[str, Any],
    project_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
) -> bool:
    """业务侧唯一入口：在事务里排一条通知事件。"""
    return await add_domain_event(
        db,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        version=version,
        payload=payload,
        project_id=project_id,
        actor_id=actor_id,
    )
