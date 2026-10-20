"""Capacity/cancellation tests use events, not external model timing."""

import asyncio
from types import SimpleNamespace
import os
import time
from unittest.mock import AsyncMock

import pytest

from data_analysis_agent.chat_runtime import BudgetedChatHandler
from data_analysis_agent.budget import RequestBudget
from data_analysis_agent.run_recorder import PostgresRunRecorder
from data_analysis_agent.llm_observability import ObservedLlmService
from data_analysis_agent.runtime_control import (
    CapacityExceeded,
    CapacityLimiter,
    ManagedChatHandler,
    RuntimeControl,
    RuntimeLimits,
    WindowRateLimiter,
    cleanup_query_files,
)
from vanna.core.llm import LlmRequest, LlmResponse, LlmStreamChunk
from vanna.core.user import User
from vanna.servers.base import ChatRequest


class HeldDelegate:
    _budget_chunk = staticmethod(BudgetedChatHandler._budget_chunk)

    def __init__(self):
        self.agent = SimpleNamespace(
            user_resolver=SimpleNamespace(
                resolve_user=AsyncMock(
                    return_value=User(
                        id="local:test", metadata={"auth_mode": "local_session"}
                    )
                )
            )
        )
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = 0
        self.calls = 0

    async def handle_stream(self, request):
        self.calls += 1
        self.started.set()
        try:
            yield self._budget_chunk(
                request.conversation_id, request.request_id, "运行中", "正在处理"
            )
            await self.release.wait()
            yield BudgetedChatHandler._text_chunk(
                request.conversation_id, request.request_id, "完成"
            )
        finally:
            self.closed += 1


async def collect(handler, conv="a", rid="r"):
    return [
        chunk
        async for chunk in handler.handle_stream(
            ChatRequest(message="查询", conversation_id=conv, request_id=rid)
        )
    ]


@pytest.mark.asyncio
async def test_queue_bounds_timeout_and_cancelled_waiter_release():
    limiter = CapacityLimiter(1, 1, 0.02)
    async with limiter.slot():

        async def waiting():
            async with limiter.slot():
                pytest.fail("occupied slot acquired")

        waiter = asyncio.create_task(waiting())
        await asyncio.sleep(0)
        with pytest.raises(CapacityExceeded, match="queue_full"):
            async with limiter.slot():
                pass
        with pytest.raises(CapacityExceeded, match="queue_timeout"):
            await waiter
        waiter = asyncio.create_task(waiting())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    assert limiter.active == limiter.waiting == 0
    async with limiter.slot():
        assert limiter.active == 1


@pytest.mark.asyncio
async def test_same_conversation_busy_and_cancel_scoped_to_owner_and_request():
    delegate = HeldDelegate()
    control = RuntimeControl(RuntimeLimits(active_requests=1))
    handler = ManagedChatHandler(delegate, control, "olist")
    running = asyncio.create_task(collect(handler))
    await delegate.started.wait()
    busy = await collect(handler, rid="other")
    assert "本会话正在处理" in busy[0].model_dump_json()
    assert not await control.cancel(
        user_id="intruder", workspace_id="olist", conversation_id="a", request_id="r"
    )
    assert not await control.cancel(
        user_id="local:test",
        workspace_id="olist",
        conversation_id="a",
        request_id="wrong",
    )
    assert await control.cancel(
        user_id="local:test", workspace_id="olist", conversation_id="a", request_id="r"
    )
    assert "请求已停止" in "".join(c.model_dump_json() for c in await running)
    assert delegate.calls == delegate.closed == 1
    assert control.snapshot()["inflight"] == 0
    assert control.requests.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disconnect", "timeout", "shutdown"])
async def test_terminal_paths_close_iterator_and_release_slots(mode):
    delegate = HeldDelegate()
    control = RuntimeControl(
        RuntimeLimits(request_timeout_seconds=0.03 if mode == "timeout" else 10)
    )
    handler = ManagedChatHandler(delegate, control, "olist")
    stream = handler.handle_stream(
        ChatRequest(message="查询", conversation_id="a", request_id="r")
    )
    await anext(stream)
    if mode == "disconnect":
        await stream.aclose()
    elif mode == "shutdown":
        # No consumer draining the queue during shutdown: must not deadlock.
        await asyncio.wait_for(control.close(), 1)
        assert [item async for item in stream] == []
    else:
        chunks = [item async for item in stream]
        assert "处理超时" in "".join(c.model_dump_json() for c in chunks)
    assert delegate.closed == 1
    assert not control.inflight
    assert control.requests.active == 0
    assert (
        control.outcomes[
            {
                "disconnect": "client_disconnected",
                "timeout": "request_timeout",
                "shutdown": "server_shutdown",
            }[mode]
        ]
        == 1
    )


@pytest.mark.asyncio
async def test_queued_requests_can_cancel_and_overload_does_not_call_delegate():
    delegate = HeldDelegate()
    control = RuntimeControl(RuntimeLimits(active_requests=1, queued_requests=1))
    handler = ManagedChatHandler(delegate, control, "olist")
    first = asyncio.create_task(collect(handler))
    await delegate.started.wait()
    stream = handler.handle_stream(
        ChatRequest(message="查询", conversation_id="queued", request_id="q")
    )
    assert "正在排队" in (await anext(stream)).model_dump_json()
    await asyncio.sleep(0)
    rejected = await collect(handler, conv="overload")
    assert "服务繁忙" in "".join(c.model_dump_json() for c in rejected)
    await stream.aclose()
    assert control.requests.waiting == 0
    delegate.release.set()
    await first
    assert delegate.calls == 1
    assert not control.inflight


def test_rate_window_expires_without_trusting_forwarded_addresses():
    limiter = WindowRateLimiter()
    assert limiter.retry_after("user", 2, now=1) == 0
    assert limiter.retry_after("user", 2, now=2) == 0
    assert limiter.retry_after("user", 2, now=3) > 0
    assert limiter.retry_after("user", 2, now=62) == 0


@pytest.mark.asyncio
async def test_question_rate_limit_prevents_execution():
    delegate = HeldDelegate()
    delegate.release.set()
    control = RuntimeControl(RuntimeLimits(questions_per_minute=1))
    handler = ManagedChatHandler(delegate, control, "olist")
    await collect(handler)
    rejected = await collect(handler, conv="next")
    assert "提问过于频繁" in rejected[0].model_dump_json()
    assert delegate.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_model_capacity_covers_stream_and_nonstream_and_releases_on_cancel(
    stream,
):
    active = peak = 0
    release = asyncio.Event()

    class Provider:
        async def send_request(self, request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await release.wait()
                return LlmResponse(content="ok")
            finally:
                active -= 1

        async def stream_request(self, request):
            await self.send_request(request)
            yield LlmStreamChunk(content="ok")

    limiter = CapacityLimiter(1, 2, 1)
    service = ObservedLlmService(Provider(), capacity=limiter)
    request = LlmRequest(messages=[], user=User(id="test"))

    async def call():
        if stream:
            return [chunk async for chunk in service.stream_request(request)]
        return await service.send_request(request)

    tasks = [asyncio.create_task(call()) for _ in range(3)]
    await asyncio.sleep(0.02)
    assert active == peak == 1
    tasks[0].cancel()
    await asyncio.sleep(0.02)
    assert active == peak == 1
    release.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert limiter.active == limiter.waiting == active == 0


@pytest.mark.asyncio
async def test_context_compaction_does_not_hold_model_permit():
    provider = SimpleNamespace(
        send_request=AsyncMock(return_value=LlmResponse(content="ok"))
    )
    limiter = CapacityLimiter(1, 1, 0.05)
    service = ObservedLlmService(provider, capacity=limiter)

    async def prepare(request):
        if request.metadata.get("purpose") == "main":
            await service.send_request(
                LlmRequest(
                    messages=[], user=request.user, metadata={"purpose": "summary"}
                )
            )
        return request

    service._ensure_context = prepare
    result = await service.send_request(
        LlmRequest(messages=[], user=User(id="test"), metadata={"purpose": "main"})
    )
    assert result.content == "ok" and provider.send_request.await_count == 2


def test_query_cache_cleanup_leaves_unrelated_files(tmp_path):
    root = tmp_path / "cache"
    directory = root / "user"
    directory.mkdir(parents=True)
    old = directory / "query_results_old.csv"
    old.write_text("n\n1")
    recent = directory / "query_results_new.csv"
    recent.write_text("n\n2")
    unrelated = directory / "experiment.csv"
    unrelated.write_text("keep")
    os.utime(old, (0, 0))
    assert cleanup_query_files(root, 60) == 1
    assert not old.exists() and recent.exists() and unrelated.read_text() == "keep"


@pytest.mark.asyncio
async def test_cancel_waits_for_metadata_transaction_and_finalizes_started_run():
    import threading

    started, release = threading.Event(), threading.Event()
    events = []
    recorder = PostgresRunRecorder()

    def insert(*args):
        started.set()
        release.wait(1)
        events.append("inserted")

    def finish(run, usage):
        events.append(usage.termination_reason)

    recorder._start_sync, recorder._finish_sync = insert, finish
    task = asyncio.create_task(
        recorder.start(
            request_id="cancel-start",
            conversation_id="metadata",
            user=User(id="test"),
            question="test",
            budget=RequestBudget(),
            dataset_version_id="v1",
            metric_version="1",
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()  # Metadata thread still owns its transaction.
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["inserted", "client_disconnected"]
