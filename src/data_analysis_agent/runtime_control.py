"""Single-process capacity and cancellation for the product SSE endpoint."""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
import os
from time import monotonic
import time
from pathlib import Path
import uuid

import anyio

from vanna.servers.base import ChatHandler

logger = logging.getLogger(__name__)


def cleanup_query_files(root: Path, retention_seconds: int) -> int:
    """Expire Vanna's duplicate CSV cache, only its known per-user file names."""
    removed = 0
    cutoff = time.time() - retention_seconds
    for path in root.glob("*/query_results_*.csv"):
        if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
            continue
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return removed


@dataclass(frozen=True)
class RuntimeLimits:
    active_requests: int = 4
    queued_requests: int = 8
    queue_wait_seconds: float = 15.0
    model_calls: int = 4
    request_timeout_seconds: float = 180.0
    login_per_minute: int = 10
    questions_per_minute: int = 30
    artifact_retention_seconds: int = 7 * 86400
    artifact_cleanup_seconds: int = 3600

    @classmethod
    def from_environment(cls):
        values = {
            name: type(default)(os.getenv("DATA_ANALYSIS_" + name.upper(), default))
            for name, default in cls().__dict__.items()
        }
        if any(value <= 0 for value in values.values()):
            raise ValueError("runtime limits must be positive")
        return cls(**values)


class CapacityExceeded(Exception):
    pass


class CapacityLimiter:
    def __init__(self, active: int, queued: int, wait_seconds: float):
        self.limit, self.max_queued, self.wait_seconds = active, queued, wait_seconds
        self.semaphore = asyncio.Semaphore(active)
        self.active = self.waiting = self.peak_active = self.peak_waiting = 0

    @asynccontextmanager
    async def slot(self):
        started = monotonic()
        if self.semaphore.locked():
            if self.waiting >= self.max_queued:
                raise CapacityExceeded("queue_full")
            self.waiting += 1
            self.peak_waiting = max(self.peak_waiting, self.waiting)
            try:
                await asyncio.wait_for(self.semaphore.acquire(), self.wait_seconds)
            except TimeoutError as exc:
                raise CapacityExceeded("queue_timeout") from exc
            finally:
                self.waiting -= 1
        else:
            await self.semaphore.acquire()
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            yield round((monotonic() - started) * 1000, 2)
        finally:
            self.active -= 1
            self.semaphore.release()


class WindowRateLimiter:
    """A bounded in-process minute window; no forwarded-IP header is trusted."""

    def __init__(self):
        self.windows = {}

    def retry_after(self, key, limit, *, now=None):
        now = monotonic() if now is None else now
        for old_key in [
            k for k, v in self.windows.items() if not v or v[-1] <= now - 60
        ]:
            del self.windows[old_key]
        if key not in self.windows and len(self.windows) >= 4096:
            return 60
        window = self.windows.setdefault(key, deque())
        while window and window[0] <= now - 60:
            window.popleft()
        if len(window) >= limit:
            return max(1, int(60 - (now - window[0])) + 1)
        window.append(now)
        return 0


@dataclass
class ActiveRequest:
    key: tuple[str, str, str]
    request_id: str
    task: asyncio.Task | None = None
    cancel_reason: str | None = None
    detached: bool = False
    started: float = field(default_factory=monotonic)
    first_effective_ms: float | None = None
    outcome: str = "running"
    queue: asyncio.Queue | None = None
    producer_started: bool = False

    def cancel(self, reason):
        if (
            self.task is not None
            and not self.task.done()
            and not self.task.cancelling()
        ):
            self.cancel_reason = reason
            self.task.cancel()


CURRENT_REQUEST: ContextVar[ActiveRequest | None] = ContextVar(
    "active_product_request", default=None
)


class RuntimeControl:
    def __init__(self, limits: RuntimeLimits):
        self.limits = limits
        self.requests = CapacityLimiter(
            limits.active_requests, limits.queued_requests, limits.queue_wait_seconds
        )
        self.models = CapacityLimiter(
            limits.model_calls, limits.queued_requests, limits.queue_wait_seconds
        )
        self.rates = WindowRateLimiter()
        self.inflight: dict[tuple[str, str, str], ActiveRequest] = {}
        self.outcomes = Counter()
        self.closed = False

    async def cancel(self, *, user_id, workspace_id, conversation_id, request_id):
        active = self.inflight.get((user_id, workspace_id, conversation_id))
        if active is None or active.request_id != request_id:
            return False
        active.cancel("user_cancelled")
        await asyncio.gather(active.task, return_exceptions=True)
        return True

    async def close(self):
        self.closed = True
        tasks = [active.task for active in list(self.inflight.values())]
        for active in list(self.inflight.values()):
            active.detached = True
            while not active.queue.empty():
                active.queue.get_nowait()
            active.queue.put_nowait(None)
            active.cancel("server_shutdown")
        await asyncio.gather(*tasks, return_exceptions=True)

    def snapshot(self):
        return {
            name: {
                "active": limiter.active,
                "waiting": limiter.waiting,
                "peak_active": limiter.peak_active,
                "peak_waiting": limiter.peak_waiting,
                "limit": limiter.limit,
            }
            for name, limiter in (("requests", self.requests), ("models", self.models))
        } | {"inflight": len(self.inflight), "outcomes": dict(self.outcomes)}


class ManagedChatHandler(ChatHandler):
    """A cancellable producer owns the whole turn, including DB/run cleanup."""

    def __init__(self, delegate, control: RuntimeControl, workspace_id: str):
        super().__init__(delegate.agent)
        self.delegate, self.control, self.workspace_id = delegate, control, workspace_id

    async def handle_stream(self, request):
        user = await self.agent.user_resolver.resolve_user(request.request_context)
        request.conversation_id = request.conversation_id or f"conv_{uuid.uuid4().hex}"
        request.request_id = request.request_id or str(uuid.uuid4())
        card = lambda title, detail: self.delegate._budget_chunk(
            request.conversation_id, request.request_id, title, detail
        )
        if user.metadata.get("auth_mode") == "anonymous":
            yield card("请先登录", "登录状态已失效，请重新登录。")
            return
        key = (user.id, self.workspace_id, request.conversation_id)
        if self.control.closed:
            yield card("服务正在停止", "请稍后重新连接，已保存的会话仍可恢复。")
            return
        if key in self.control.inflight:
            self.control.outcomes["conversation_busy"] += 1
            yield card(
                "本会话正在处理",
                "请等待当前回答完成，或先停止当前请求；其他会话可以继续使用。",
            )
            return
        if request.message.strip():
            retry = self.control.rates.retry_after(
                ("question", user.id), self.control.limits.questions_per_minute
            )
            if retry:
                self.control.outcomes["rate_limited"] += 1
                yield card(
                    "提问过于频繁",
                    f"请在{retry}秒后重试；本次没有调用模型或查询数据库。",
                )
                return
        active = ActiveRequest(key, request.request_id)
        queue = asyncio.Queue(maxsize=1)
        active.queue = queue
        self.control.inflight[key] = active

        async def emit(chunk):
            if not active.detached:
                await queue.put(chunk)

        async def produce():
            active.producer_started = True
            token = CURRENT_REQUEST.set(active)
            timer = None
            try:
                if self.control.requests.semaphore.locked():
                    yield_status = card(
                        "正在排队", "当前分析请求较多，正在等待处理；可以停止本次请求。"
                    )
                    await emit(yield_status)
                async with self.control.requests.slot() as queue_ms:
                    request.request_context.metadata["request_queue_ms"] = queue_ms
                    timer = asyncio.get_running_loop().call_later(
                        self.control.limits.request_timeout_seconds,
                        active.cancel,
                        "request_timeout",
                    )
                    iterator = self.delegate.handle_stream(request)
                    try:
                        async for chunk in iterator:
                            rich = chunk.rich
                            if (
                                active.first_effective_ms is None
                                and rich
                                and rich.get("type")
                                in {"text", "dataframe", "plotly", "chart"}
                            ):
                                active.first_effective_ms = round(
                                    (monotonic() - active.started) * 1000, 2
                                )
                            await emit(chunk)
                    finally:
                        await iterator.aclose()
                    usage = request.request_context.metadata.get("budget_usage")
                    active.outcome = (
                        usage.termination_reason
                        if usage is not None and usage.termination_reason != "running"
                        else "completed"
                    )
            except CapacityExceeded as exc:
                active.outcome = str(exc)
                await emit(
                    card(
                        "服务繁忙",
                        "等待队列已满或超过等待时间，本次未继续处理，请稍后重试。",
                    )
                )
            except asyncio.CancelledError:
                active.outcome = active.cancel_reason or "client_disconnected"
                if not active.detached:
                    await emit(
                        card(
                            "请求已停止"
                            if active.outcome == "user_cancelled"
                            else "处理超时",
                            "本次处理已停止，已保存的结果仍可查看。请稍后重试或缩小分析范围。",
                        )
                    )
            except Exception:
                active.outcome = "execution_error"
                logger.exception(
                    "product_request_failed request_id=%s", request.request_id
                )
                await emit(
                    card(
                        "本次分析未完成",
                        "服务调用失败，未将未经验证的数据作为结论返回。请稍后重试。",
                    )
                )
            finally:
                if timer is not None:
                    timer.cancel()
                CURRENT_REQUEST.reset(token)
                await emit(None)

        active.task = asyncio.create_task(produce())

        def finished(task):
            if self.control.inflight.get(key) is not active:
                return
            if not active.producer_started:
                active.outcome = active.cancel_reason or "client_disconnected"
                if not active.detached:
                    queue.put_nowait(None)
            self.control.inflight.pop(key, None)
            self.control.outcomes[active.outcome] += 1
            logger.info(
                "product_request_finished request_id=%s outcome=%s elapsed_ms=%.2f first_effective_ms=%s",
                active.request_id,
                active.outcome,
                (monotonic() - active.started) * 1000,
                active.first_effective_ms,
            )

        active.task.add_done_callback(finished)
        try:
            while (chunk := await queue.get()) is not None:
                yield chunk
        finally:
            active.detached = True
            while not queue.empty():
                queue.get_nowait()
            active.cancel("client_disconnected")
            # ASGI disconnect cancels its AnyIO scope repeatedly. Shield only
            # cleanup, not analysis, so the child can cancel PostgreSQL and
            # persist its terminal run before the request slot is reused.
            with anyio.CancelScope(shield=True):
                await asyncio.gather(active.task, return_exceptions=True)
            finished(active.task)
