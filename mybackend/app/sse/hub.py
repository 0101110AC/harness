"""SSE Hub：进程内订阅 / 发布 / 心跳 / 断线重放（prd-06 §12.6）。

设计约束：
- **先落库再推送**：`hub.publish` 只负责"把已经写进 agent_events 的事件推给在线连接"，
  所以推送失败（慢消费者、连接已断）不会造成**不可恢复**的数据缺口 —— 重连时从库里补。
- **回放窗口**：30min / 5000 条，超出先发 `stream_reset` 让前端全量刷新，
  而不是悄悄截断（截断 = 前端状态永久错位）。
- **多实例**：MVP 单实例进程内 pub/sub；多实例需换 Redis Pub/Sub，本模块接口不变。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from app.ai.events import TERMINAL_EVENTS, count_events, list_events
from app.core.config import settings
from app.core.errors import AppError

log = logging.getLogger(__name__)

MAX_QUEUE = 500


class Subscription:
    __slots__ = ("thread_id", "user_id", "queue", "created_at", "last_business_event")

    def __init__(self, thread_id: str, user_id: str) -> None:
        self.thread_id = thread_id
        self.user_id = user_id
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=MAX_QUEUE)
        now = time.monotonic()
        self.created_at = now
        self.last_business_event = now


def sse_frame(event: str, data: dict[str, Any], seq: int | None = None) -> str:
    head = f"id: {seq}\n" if seq is not None else ""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"{head}event: {event}\ndata: {payload}\n\n"


class SSEHub:
    def __init__(self) -> None:
        self._subs: dict[str, set[Subscription]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._total = 0

    # ---------------- 订阅管理 ----------------
    async def subscribe(self, thread_id: str, user_id: str) -> Subscription:
        async with self._lock:
            if self._total >= settings.SSE_MAX_CONNECTIONS:
                raise AppError(
                    "STREAM_LIMIT_EXCEEDED", "实时连接已满，请稍后重试", 429
                )
            if len(self._subs[thread_id]) >= settings.SSE_MAX_PER_THREAD:
                raise AppError(
                    "STREAM_LIMIT_EXCEEDED",
                    f"该任务已有 {settings.SSE_MAX_PER_THREAD} 个实时连接",
                    429,
                )
            sub = Subscription(thread_id, user_id)
            self._subs[thread_id].add(sub)
            self._total += 1
            return sub

    async def unsubscribe(self, sub: Subscription) -> None:
        async with self._lock:
            if sub in self._subs.get(sub.thread_id, set()):
                self._subs[sub.thread_id].discard(sub)
                self._total -= 1
            if not self._subs.get(sub.thread_id):
                self._subs.pop(sub.thread_id, None)

    @property
    def connection_count(self) -> int:
        return self._total

    # ---------------- 发布 ----------------
    async def publish(self, thread_id: str, item: dict[str, Any]) -> None:
        for sub in list(self._subs.get(thread_id, ())):
            try:
                sub.queue.put_nowait(item)
            except asyncio.QueueFull:
                # 慢消费者：丢最旧的非终态事件，终态必须保留（否则前端永远停在"运行中"）
                with suppress(asyncio.QueueEmpty):
                    dropped = sub.queue.get_nowait()
                    if dropped and dropped.get("event") in TERMINAL_EVENTS:
                        with suppress(asyncio.QueueFull):
                            sub.queue.put_nowait(dropped)
                with suppress(asyncio.QueueFull):
                    sub.queue.put_nowait(item)
                log.warning("sse slow consumer: thread=%s user=%s", thread_id, sub.user_id)

    # ---------------- 生成器 ----------------
    async def stream(
        self, sub: Subscription, *, from_seq: int = 0, snapshot: dict[str, Any] | None = None
    ) -> AsyncIterator[str]:
        """SSE 生成器：stream_open → 回放 → 实时 + 心跳 → 终态收尾。

        任何退出路径都必须走到 finally 摘除订阅，否则连接池会被慢慢抽干。
        """
        last_seq = from_seq
        try:
            # ① 连接级首帧（不落库、不可回放）：让前端 ≤500ms 内拿到当前快照
            yield sse_frame(
                "stream_open",
                {
                    "thread_id": sub.thread_id,
                    "from_seq": from_seq,
                    "snapshot": snapshot or {},
                    "ts": datetime.now(timezone.utc).isoformat(),
                },
            )

            # ② 回放：断线期间的事件从 agent_events 补齐
            total = await count_events(sub.thread_id, from_seq)
            if total > settings.SSE_REPLAY_LIMIT:
                yield sse_frame(
                    "stream_reset",
                    {
                        "reason": "replay_overflow",
                        "from_seq": from_seq,
                        "pending": total,
                        "limit": settings.SSE_REPLAY_LIMIT,
                    },
                )
                # 溢出后仍从"最近窗口"继续推，前端已全量刷新，不会错位
                from_seq = max(from_seq, 0)

            history = await list_events(sub.thread_id, from_seq, settings.SSE_REPLAY_LIMIT)
            for item in history:
                last_seq = max(last_seq, int(item["seq"]))
                yield sse_frame(item["event"], item, item["seq"])

            if history and history[-1]["event"] in TERMINAL_EVENTS:
                await asyncio.sleep(2)  # 留 2s 给前端收尾（prd-06 §9.1）
                return

            # ③ 实时 + 心跳
            while True:
                try:
                    item = await asyncio.wait_for(
                        sub.queue.get(), timeout=settings.SSE_HEARTBEAT_SEC
                    )
                except (TimeoutError, asyncio.TimeoutError):
                    idle = time.monotonic() - sub.last_business_event
                    if idle > settings.SSE_IDLE_CLOSE_SEC:
                        yield sse_frame("stream_idle", {"idle_seconds": int(idle)})
                        return
                    yield sse_frame(
                        "heartbeat",
                        {"ts": datetime.now(timezone.utc).isoformat()},
                    )
                    continue

                if item is None:  # 停机信号
                    yield sse_frame("stream_reset", {"reason": "server_shutdown"})
                    return

                seq = int(item.get("seq") or 0)
                if seq and seq <= last_seq:
                    continue  # 回放与实时队列的重叠部分，按 seq 去重

                last_seq = max(last_seq, seq)
                sub.last_business_event = time.monotonic()
                yield sse_frame(item["event"], item, seq or None)

                if item["event"] in TERMINAL_EVENTS:
                    await asyncio.sleep(2)
                    return
        except asyncio.CancelledError:  # 客户端断开
            raise
        except Exception:  # 兜底：绝不让异常泄漏到 ASGI 层变成 500
            log.exception("sse stream error thread=%s", sub.thread_id)
        finally:
            await self.unsubscribe(sub)


hub = SSEHub()
