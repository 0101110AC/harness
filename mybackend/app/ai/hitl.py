"""HITL 引擎侧：24h 超时扫描 + 启动时运行恢复对账（prd-06 §8.3 / §12.5）。

这里只放"和进程生命周期绑定"的逻辑；HTTP 语义（幂等/409/410）在
`app/services/hitl_service.py`，两者的唯一共同点是 interrupt_repo 的条件更新。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.ai import interrupt_repo
from app.core.config import settings
from app.database import AsyncSessionLocal
from app.models.enums import ReviewTaskStatus, RunStatus
from app.models.review import ReviewTask

log = logging.getLogger(__name__)


async def sweep_expired_once(limit: int | None = None) -> int:
    """把过期的中断工单置 EXPIRED，并把对应运行置 FAILED(HITL_TIMEOUT)。

    为什么先"条件更新工单"再"改任务"：
    多个实例同时扫时，只有一个能把某条工单从 PENDING 改成 EXPIRED，
    所以"发 run_failed 事件"也就只会发生一次（事件表有唯一 (thread_id, seq)）。
    """
    # 延迟导入：runtime.startup() 会 import 本模块，模块级 import 会成环
    from app.ai.runtime import run_manager

    expired = await interrupt_repo.claim_expired(limit or settings.HITL_SWEEP_BATCH)
    for item in expired:
        try:
            await run_manager.mark_failed(
                thread_id=str(item["thread_id"]),
                run_id=str(item["run_id"] or ""),
                reason="HITL_TIMEOUT",
                message="人工确认已超时，请重新发起预审",
                retriable=True,
            )
        except Exception:
            log.exception("超时善后失败 interrupt=%s", item["id"])

    if expired:
        log.info("HITL 超时扫描：%d 条中断被置为 EXPIRED", len(expired))
    return len(expired)


async def hitl_sweeper_loop() -> None:
    while True:
        await asyncio.sleep(settings.HITL_SWEEP_SECONDS)
        try:
            await sweep_expired_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("HITL 超时扫描异常（不影响主流程，下轮继续）")


async def resume_pending_runs() -> None:
    """启动对账：不重复中断、不静默重跑（prd-06 §8.3 恢复语义表）。

    - 有 PENDING 工单且未过期 → 保持 WAITING_HUMAN，等人工处理；
    - WAITING_HUMAN 但没有 PENDING 工单 → 说明"人工已处理、但 resume 没跑起来"
      （进程在 resolve 与 resume 之间挂了）。此时 checkpoint 里还停在 interrupt，
      但我们没有 resume 值可用 → 置 FAILED(CHECKPOINT_UNAVAILABLE)，
      提示重新发起预审，而不是猜一个决定继续跑（那会产出没人确认过的报告）。
    """
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        tasks = (
            await db.execute(
                select(ReviewTask).where(ReviewTask.run_status == RunStatus.WAITING_HUMAN)
            )
        ).scalars().all()

        for task in tasks:
            pending = await interrupt_repo.get_pending_for_thread(db, task.thread_id)
            if pending is not None:
                if pending.expires_at < now:
                    continue  # 交给超时扫描
                if task.waiting_interrupt_id != pending.id:
                    task.waiting_interrupt_id = pending.id
                log.info(
                    "恢复对账：thread=%s 仍在等待 %s 处理", task.thread_id, pending.type.value
                )
                continue

            log.warning("恢复对账：thread=%s 处于 WAITING_HUMAN 但无待处理工单，置 FAILED", task.thread_id)
            task.status = ReviewTaskStatus.FAILED
            task.run_status = RunStatus.FAILED
            task.error_reason = "人工确认已处理但运行未能恢复，请重新发起预审"
            task.finished_at = now
            task.waiting_interrupt_id = None

        await db.commit()
