"""RunManager：Checkpointer 生命周期 + 图执行 + 运行终态落库（prd-06 §12.3）。

三个必须在这里解决、不能散落在节点里的问题：

1. **Checkpointer 是应用级单例**：`AsyncSqliteSaver.from_conn_string` 是异步上下文
   管理器，必须在 lifespan 里 enter/exit；每个请求新建一个 saver 会丢 checkpoint
   （恢复的唯一权威就是它），并且每次都要重跑 setup()。
2. **同一 thread 的执行必须串行**：start 与 resume 都可能并发进来，
   两个 `ainvoke` 同时跑同一个 thread 会把 checkpoint 写成"分叉历史"。
   这里用 per-thread asyncio.Lock + DB 条件更新双保险。
3. **终态判定以 snapshot 为准**：`ainvoke` 正常返回 ≠ 运行成功 —— 可能是中断挂起。
   必须 `aget_state()` 看 `interrupts` 才能区分 WAITING_HUMAN 与 SUCCEEDED。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import Any

from langgraph.types import Command
from sqlalchemy import func, select, update
from sqlalchemy.engine import make_url

from app.ai.events import publish_event
from app.ai.graph import build_graph
from app.ai.nodes import RunFailed
from app.core.config import settings
from app.database import AsyncSessionLocal
from app.models.drawing import Drawing
from app.models.enums import (
    DrawingStatus,
    NotificationType,
    ReviewStage,
    ReviewTaskStatus,
    RunStatus,
    Severity,
)
from app.notify.outbox import enqueue
from app.models.review import ReviewIssue, ReviewTask

log = logging.getLogger(__name__)


class RunManager:
    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._saver: Any = None
        self._graph: Any = None
        self._tasks: set[asyncio.Task] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._sweeper: asyncio.Task | None = None
        self._started = False

    # ---------------- 生命周期 ----------------
    async def startup(self) -> None:
        if self._started:
            return

        url = make_url(settings.DB_URL)
        if settings.is_sqlite:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            db_path = url.database or "./arch_harness.db"
            self._saver = await self._stack.enter_async_context(
                AsyncSqliteSaver.from_conn_string(db_path)
            )
            # 与业务连接共享同一个 SQLite 文件：WAL 已开，这里补 busy_timeout，
            # 避免"图写 checkpoint"和"节点写业务表"撞成 database is locked。
            await self._saver.conn.execute("PRAGMA busy_timeout=5000")
            await self._saver.conn.commit()
        else:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            conn_string = settings.DB_URL.replace("postgresql+asyncpg://", "postgresql://")
            self._saver = await self._stack.enter_async_context(
                AsyncPostgresSaver.from_conn_string(conn_string)
            )

        await self._saver.setup()  # 建 checkpoints/checkpoint_blobs/checkpoint_writes，幂等
        self._graph = build_graph(self._saver)
        self._started = True

        # 超时扫描与运行恢复放在 startup 之后，避免 import 期就拉起协程
        from app.ai.hitl import hitl_sweeper_loop, resume_pending_runs

        await resume_pending_runs()
        self._sweeper = asyncio.create_task(hitl_sweeper_loop(), name="hitl-sweeper")
        log.info("RunManager started (saver=%s)", type(self._saver).__name__)

    async def shutdown(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except (asyncio.CancelledError, Exception):  # noqa: B014 - 停机不抛
                pass
            self._sweeper = None

        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass
        self._tasks.clear()

        await self._stack.aclose()
        self._stack = AsyncExitStack()
        self._graph = None
        self._saver = None
        self._started = False

    @property
    def graph(self) -> Any:
        if self._graph is None:
            raise RuntimeError("RunManager 未初始化：请先 await run_manager.startup()")
        return self._graph

    @property
    def saver(self) -> Any:
        return self._saver

    # ---------------- 工具 ----------------
    def _lock(self, thread_id: str) -> asyncio.Lock:
        lock = self._locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[thread_id] = lock
        return lock

    @staticmethod
    def _config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def wait_all(self, timeout: float = 30.0) -> None:
        """测试/关机用：等所有在跑的运行收敛。"""
        pending = list(self._tasks)
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    # ---------------- 启动新运行 ----------------
    async def start(
        self,
        *,
        thread_id: str,
        run_id: str,
        project_id: str,
        drawing_id: str,
        review_mode: str,
    ) -> None:
        await self._mark_running(thread_id)
        await publish_event(
            thread_id=thread_id,
            run_id=run_id,
            event="run_started",
            data={
                "percent": 0,
                "engine": settings.REVIEW_ENGINE,
                "review_mode": review_mode,
                "drawing_id": drawing_id,
            },
        )

        from app.ai.state import initial_state

        payload = initial_state(
            thread_id=thread_id,
            run_id=run_id,
            project_id=project_id,
            drawing_id=drawing_id,
            review_mode=review_mode,
        )
        self.spawn(
            self._run(
                thread_id=thread_id,
                run_id=run_id,
                graph_input=payload,
                started=time.monotonic(),
            )
        )

    # ---------------- 恢复 ----------------
    async def resume(self, *, thread_id: str, run_id: str, resume_value: dict[str, Any]) -> None:
        await self._mark_running(thread_id)
        self.spawn(
            self._run(
                thread_id=thread_id,
                run_id=run_id,
                graph_input=Command(resume=resume_value),
                started=time.monotonic(),
            )
        )

    async def resume_and_wait(
        self, *, thread_id: str, run_id: str, resume_value: dict[str, Any], timeout: float = 30.0
    ) -> None:
        """同步等待版本：报告确认（确认后只剩打包，等它跑完才好返回真实 run_status）。"""
        await self._mark_running(thread_id)
        task = self.spawn(
            self._run(
                thread_id=thread_id,
                run_id=run_id,
                graph_input=Command(resume=resume_value),
                started=time.monotonic(),
            )
        )
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("resume_and_wait 超时 thread=%s（运行仍在后台继续）", thread_id)

    # ---------------- 执行体 ----------------
    async def _run(
        self,
        *,
        thread_id: str,
        run_id: str,
        graph_input: Any,
        started: float,
    ) -> None:
        async with self._lock(thread_id):
            try:
                await self.graph.ainvoke(graph_input, self._config(thread_id))
                await self._settle(thread_id=thread_id, run_id=run_id, started=started)
            except RunFailed as exc:
                await self.mark_failed(
                    thread_id=thread_id,
                    run_id=run_id,
                    reason=exc.reason,
                    message=exc.message,
                    retriable=exc.retriable,
                )
            except asyncio.CancelledError:
                log.info("run cancelled thread=%s", thread_id)
                raise
            except Exception as exc:  # 不可预期异常：不静默重跑
                log.exception("graph 执行失败 thread=%s", thread_id)
                await self.mark_failed(
                    thread_id=thread_id,
                    run_id=run_id,
                    reason="ENGINE_ERROR",
                    message=f"引擎执行异常：{exc}",
                    retriable=True,
                )

    async def _settle(self, *, thread_id: str, run_id: str, started: float) -> None:
        """一次 invoke 返回后判定：挂起了，还是跑完了。"""
        snapshot = await self.graph.aget_state(self._config(thread_id))
        interrupts = getattr(snapshot, "interrupts", ()) or ()
        if interrupts:
            payload = getattr(interrupts[0], "value", {}) or {}
            await self._mark_waiting(thread_id=thread_id, interrupt_payload=payload)
            return
        await self._mark_succeeded(thread_id=thread_id, run_id=run_id, started=started)

    # ---------------- 状态落库 ----------------
    async def _load_task(self, thread_id: str) -> ReviewTask | None:
        async with AsyncSessionLocal() as db:
            return (
                await db.execute(select(ReviewTask).where(ReviewTask.thread_id == thread_id))
            ).scalar_one_or_none()

    async def _mark_running(self, thread_id: str) -> None:
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            task = (
                await db.execute(select(ReviewTask).where(ReviewTask.thread_id == thread_id))
            ).scalar_one_or_none()
            if task is None:
                return
            await db.execute(
                update(ReviewTask)
                .where(ReviewTask.id == task.id)
                .values(
                    status=ReviewTaskStatus.REVIEWING,
                    run_status=RunStatus.RUNNING,
                    started_at=task.started_at or now,
                    error_reason=None,
                    current_node=task.current_node or "load_context",
                )
            )
            await db.execute(
                update(Drawing)
                .where(Drawing.id == task.drawing_id)
                .values(status=DrawingStatus.REVIEWING)
            )
            await db.commit()

    async def _mark_waiting(self, *, thread_id: str, interrupt_payload: dict[str, Any]) -> None:
        stage = (
            ReviewStage.WAITING_RISK
            if interrupt_payload.get("type") == "RISK_CONFIRM"
            else ReviewStage.WAITING_REPORT
        )
        async with AsyncSessionLocal() as db:
            task = (
                await db.execute(select(ReviewTask).where(ReviewTask.thread_id == thread_id))
            ).scalar_one_or_none()
            if task is None:
                return
            interrupt_id = None
            raw = interrupt_payload.get("interrupt_id")
            if raw:
                try:
                    interrupt_id = uuid.UUID(str(raw))
                except ValueError:
                    interrupt_id = task.waiting_interrupt_id
            await db.execute(
                update(ReviewTask)
                .where(ReviewTask.id == task.id)
                .values(
                    run_status=RunStatus.WAITING_HUMAN,
                    waiting_interrupt_id=interrupt_id or task.waiting_interrupt_id,
                    stage=stage,
                )
            )
            await db.commit()

    async def _mark_succeeded(self, *, thread_id: str, run_id: str, started: float) -> None:
        finished = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            task = (
                await db.execute(select(ReviewTask).where(ReviewTask.thread_id == thread_id))
            ).scalar_one_or_none()
            if task is None:
                return

            rows = (
                await db.execute(
                    select(ReviewIssue.severity, func.count())
                    .where(ReviewIssue.review_task_id == task.id)
                    .group_by(ReviewIssue.severity)
                )
            ).all()
            by_severity = {s.value: 0 for s in Severity}
            for severity, count in rows:
                by_severity[severity.value if hasattr(severity, "value") else str(severity)] = count

            total = sum(by_severity.values())
            cost_ms = int((time.monotonic() - started) * 1000)
            summary = {**(task.summary or {}), "by_severity": by_severity}
            drawing_name = (
                await db.execute(select(Drawing.name).where(Drawing.id == task.drawing_id))
            ).scalar_one_or_none()

            await db.execute(
                update(ReviewTask)
                .where(ReviewTask.id == task.id)
                .values(
                    status=ReviewTaskStatus.SUCCEEDED,
                    run_status=RunStatus.SUCCEEDED,
                    stage=ReviewStage.DONE,
                    stage_text="预审完成",
                    progress=100,
                    issue_total=total,
                    issue_blocker=by_severity.get("BLOCKER", 0),
                    summary=summary,
                    error_reason=None,
                    waiting_interrupt_id=None,
                    cost_ms=cost_ms,
                    finished_at=finished,
                )
            )
            await db.execute(
                update(Drawing)
                .where(Drawing.id == task.drawing_id)
                .values(status=DrawingStatus.REVIEW_DONE)
            )
            # 预审完成 → 站内信（prd-04 §4）。actor 留空：完成是引擎产生的，
            # 不是"某人操作"，否则"不通知自己"会把发起人唯一的复盘通知吃掉。
            await enqueue(
                db,
                event_type=NotificationType.REVIEW_COMPLETED.value,
                aggregate_type="REVIEW_TASK",
                aggregate_id=task.id,
                version="DONE",
                payload={
                    "review_task_id": str(task.id),
                    "project_id": str(task.project_id),
                    "drawing_id": str(task.drawing_id),
                    "drawing_name": drawing_name or "图纸",
                    "creator_id": str(task.created_by) if task.created_by else None,
                    "total": total,
                    "blocker": by_severity.get("BLOCKER", 0),
                },
                project_id=task.project_id,
                actor_id=None,
            )
            await db.commit()
            report_id = str(task.report_id) if task.report_id else None

        await publish_event(
            thread_id=thread_id,
            run_id=run_id,
            event="run_completed",
            data={
                "percent": 100,
                "status": RunStatus.SUCCEEDED.value,
                "issue_total": total,
                "duration_ms": cost_ms,
                "report_id": report_id,
                "thread_id": thread_id,
            },
        )

    async def mark_failed(
        self, *, thread_id: str, run_id: str, reason: str, message: str, retriable: bool
    ) -> None:
        finished = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            task = (
                await db.execute(select(ReviewTask).where(ReviewTask.thread_id == thread_id))
            ).scalar_one_or_none()
            if task is None:
                return
            await db.execute(
                update(ReviewTask)
                .where(ReviewTask.id == task.id)
                .values(
                    status=ReviewTaskStatus.FAILED,
                    run_status=RunStatus.FAILED,
                    waiting_interrupt_id=None,
                    error_reason=message,
                    finished_at=finished,
                )
            )
            await db.execute(
                update(Drawing)
                .where(Drawing.id == task.drawing_id)
                .values(status=DrawingStatus.REVIEW_FAILED)
            )
            drawing_name = (
                await db.execute(select(Drawing.name).where(Drawing.id == task.drawing_id))
            ).scalar_one_or_none()
            await enqueue(
                db,
                event_type=NotificationType.REVIEW_FAILED.value,
                aggregate_type="REVIEW_TASK",
                aggregate_id=task.id,
                version="FAILED",
                payload={
                    "review_task_id": str(task.id),
                    "project_id": str(task.project_id),
                    "drawing_id": str(task.drawing_id),
                    "drawing_name": drawing_name or "图纸",
                    "creator_id": str(task.created_by) if task.created_by else None,
                    "error_reason": message,
                },
                project_id=task.project_id,
                actor_id=None,
            )
            await db.commit()

        await publish_event(
            thread_id=thread_id,
            run_id=run_id,
            event="run_failed",
            data={
                "status": RunStatus.FAILED.value,
                "reason": reason,
                "message": message,
                "retryable": retriable,
            },
        )


run_manager = RunManager()
