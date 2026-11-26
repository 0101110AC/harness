"""Agent 事件发布：先落库、再推送（prd-06 §12.6 的 publish_event）。

顺序不能反：
1. `UPDATE review_tasks SET last_seq = last_seq + 1 ... RETURNING last_seq`
   —— seq 按 thread 维度单调递增，且由数据库原子分配，多实例/多协程都不会撞号；
2. `INSERT agent_events` —— 落库是"可回放"的唯一依据，SSE 只是它的推送副本；
3. 更新 `review_tasks` 的 stage/percent/current_node —— 断线重连时的快照来源；
4. 进程内 hub.publish —— 最后一步，失败也不影响可回放性。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update

from app.database import AsyncSessionLocal
from app.models.agent import AgentEvent
from app.models.review import ReviewTask

log = logging.getLogger(__name__)

TERMINAL_EVENTS = {"run_completed", "run_failed", "run_returned"}

# stage → (percent, 文案)：文案由后端下发，前端不硬编码（prd-06 §9.7）
STAGE_TEXT: dict[str, tuple[int, str]] = {
    "LOAD_CONTEXT": (3, "正在读取项目资料"),
    "PLANNING": (8, "正在制定审查计划"),
    "PARSING": (20, "正在解析图纸"),
    "RETRIEVING": (38, "正在核对规范"),
    "VALIDATING": (55, "正在校验指标"),
    "DETECTING": (70, "正在归纳问题"),
    "WAITING_RISK": (78, "等待设计师确认风险"),
    "REPORTING": (88, "正在生成报告初稿"),
    "WAITING_REPORT": (95, "等待确认报告"),
    "PACKAGING": (98, "正在打包资料"),
    "DONE": (100, "预审完成"),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def envelope(
    *, seq: int, thread_id: str, run_id: str, event: str, data: dict[str, Any], ts: str
) -> dict[str, Any]:
    """所有事件统一信封（prd-06 §9.3）。"""
    return {
        "seq": seq,
        "thread_id": thread_id,
        "run_id": run_id,
        "event": event,
        "ts": ts,
        "data": data,
    }


async def _next_seq(db, thread_id: str) -> tuple[int, Any, str | None]:
    """原子分配 seq：优先走 review_tasks.last_seq 的自增（单条 UPDATE ... RETURNING）。"""
    row = (
        await db.execute(
            update(ReviewTask)
            .where(ReviewTask.thread_id == thread_id)
            .values(last_seq=ReviewTask.last_seq + 1)
            .returning(ReviewTask.id, ReviewTask.last_seq, ReviewTask.run_id)
        )
    ).first()

    if row is not None:
        task_id, seq, run_id = row
        return int(seq), task_id, run_id

    # 兜底：任务行缺失（理论上不该发生）——不让埋点拖垮整轮运行
    current = (
        await db.execute(
            select(func.coalesce(func.max(AgentEvent.seq), 0)).where(
                AgentEvent.thread_id == thread_id
            )
        )
    ).scalar_one()
    log.warning("publish_event: review_task(thread_id=%s) 不存在，seq 退化为 max+1", thread_id)
    return int(current) + 1, None, None


async def publish_event(
    *,
    thread_id: str,
    run_id: str,
    event: str,
    data: dict[str, Any],
    persist: bool = True,
) -> int:
    """发布一条事件，返回 seq。`persist=False` 用于 heartbeat 一类瞬时帧。"""
    from app.sse.hub import hub  # 延迟导入，避免 events ↔ hub 循环引用

    ts = now_iso()
    seq = 0

    if persist:
        async with AsyncSessionLocal() as db:
            seq, task_id, task_run_id = await _next_seq(db, thread_id)
            effective_run_id = task_run_id or run_id

            db.add(
                AgentEvent(
                    thread_id=thread_id,
                    run_id=effective_run_id or "",
                    seq=seq,
                    event=event,
                    data=data,
                )
            )

            if task_id is not None:
                values: dict[str, Any] = {}
                stage = data.get("stage")
                if isinstance(stage, str) and stage in STAGE_TEXT:
                    percent, text = STAGE_TEXT[stage]
                    values.update(
                        stage=stage,
                        stage_text=text,
                        progress=int(data.get("percent", percent)),
                    )
                elif isinstance(data.get("percent"), int):
                    values["progress"] = data["percent"]
                if data.get("node"):
                    values["current_node"] = str(data["node"])
                if values:
                    await db.execute(
                        update(ReviewTask).where(ReviewTask.id == task_id).values(**values)
                    )

            await db.commit()
    else:
        effective_run_id = run_id

    await hub.publish(
        thread_id,
        envelope(
            seq=seq,
            thread_id=thread_id,
            run_id=effective_run_id or "",
            event=event,
            data=data,
            ts=ts,
        ),
    )
    return seq


async def list_events(thread_id: str, from_seq: int, limit: int) -> list[dict[str, Any]]:
    """按 seq 升序取事件（Last-Event-ID 重放用）。"""
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(AgentEvent)
                .where(AgentEvent.thread_id == thread_id, AgentEvent.seq > from_seq)
                .order_by(AgentEvent.seq)
                .limit(limit)
            )
        ).scalars().all()

    return [
        envelope(
            seq=ev.seq,
            thread_id=ev.thread_id,
            run_id=ev.run_id,
            event=ev.event,
            data=ev.data,
            ts=ev.created_at.isoformat(),
        )
        for ev in rows
    ]


async def count_events(thread_id: str, from_seq: int) -> int:
    async with AsyncSessionLocal() as db:
        return int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(AgentEvent)
                    .where(AgentEvent.thread_id == thread_id, AgentEvent.seq > from_seq)
                )
            ).scalar_one()
        )
