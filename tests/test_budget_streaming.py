"""Check every visible event, including those emitted before final middleware."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from data_analysis_agent.budget import (
    BudgetSafetyMiddleware,
    BudgetUsage,
    CURRENT_BUDGET,
    RequestBudget,
)
from vanna import Agent, AgentConfig
from vanna.core.llm import LlmRequest, LlmResponse, LlmService, LlmStreamChunk
from vanna.core.registry import ToolRegistry
from vanna.core.tool import ToolCall
from vanna.core.user import RequestContext, User, UserResolver
from vanna.integrations.local.agent_memory import DemoAgentMemory
from vanna.servers.base.chat_handler import ChatHandler
from vanna.servers.base.models import ChatRequest


CANDIDATE_NUMBER = "987654321"


class _UserResolver(UserResolver):
    async def resolve_user(self, request_context):
        return User(id="stream-test", group_memberships=["analyst"])


class _StreamingLlm(LlmService):
    def __init__(self, *, fail=False, tool_calls=None):
        self.finished = False
        self.fail = fail
        self.tool_calls = tool_calls

    async def send_request(self, request):
        return LlmResponse(content=f"销售额是 {CANDIDATE_NUMBER} 元")

    async def stream_request(self, request):
        yield LlmStreamChunk(content="销售额是 ")
        yield LlmStreamChunk(content=CANDIDATE_NUMBER)
        if self.fail:
            raise TimeoutError("simulated provider interruption")
        self.finished = True
        yield LlmStreamChunk(
            content=" 元",
            tool_calls=self.tool_calls,
            finish_reason="tool_calls" if self.tool_calls else "stop",
            metadata={"usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18}},
        )

    async def validate_tools(self, tools):
        return []


def _agent(llm, *, stream=True):
    return Agent(
        llm_service=llm,
        tool_registry=ToolRegistry(),
        user_resolver=_UserResolver(),
        agent_memory=DemoAgentMemory(),
        llm_middlewares=[BudgetSafetyMiddleware()],
        config=AgentConfig(stream_responses=stream),
    )


def _usage(reason):
    usage = BudgetUsage(RequestBudget())
    if reason == "degraded":
        usage.mark_result_answer_blocked()
    elif reason == "tool_budget":
        usage.terminate("tool_budget_exhausted")
    elif reason in {"sql_repair_failed", "result_validation_failed"}:
        usage.terminate("execution_error", reason)
    elif reason == "trusted":
        usage.mark_result_contract_satisfied()
    return usage


async def _chat_events(reason, *, stream=True, llm=None):
    """Use the actual Agent, conversation save and SSE serializer."""
    agent = _agent(llm or _StreamingLlm(), stream=stream)
    token = CURRENT_BUDGET.set(_usage(reason))
    try:
        return [
            event.model_dump(mode="json")
            async for event in ChatHandler(agent).handle_stream(
                ChatRequest(message="请说明查询结果", conversation_id="stream-test-conversation")
            )
        ]
    finally:
        CURRENT_BUDGET.reset(token)


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize(
    ("reason", "safe_text"),
    [
        ("degraded", "不能据此给出数字结论"),
        ("tool_budget", "未输出未经完整验证的数值结论"),
        ("sql_repair_failed", "未通过可信执行或结果合同校验"),
        ("result_validation_failed", "未通过可信执行或结果合同校验"),
    ],
)
async def test_blocked_answer_never_enters_rich_simple_or_sse(reason, safe_text, stream):
    events = await _chat_events(reason, stream=stream)
    sse = "".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events)
    assert CANDIDATE_NUMBER not in sse
    assert safe_text in sse
    assert any(event["rich"]["type"] == "text" for event in events)


@pytest.mark.parametrize("reason", ["trusted", "ordinary"])
async def test_normal_answer_still_streams_before_provider_finishes(reason):
    llm = _StreamingLlm()
    agent = _agent(llm)
    usage = _usage(reason)
    token = CURRENT_BUDGET.set(usage)
    try:
        stream = agent._handle_streaming_response(
            LlmRequest(messages=[], user=User(id="stream-test"), stream=True)
        )
        first = await anext(stream)
        assert first.simple_component.text == "销售额是 "
        assert llm.finished is False
        remaining = [event async for event in stream]
    finally:
        CURRENT_BUDGET.reset(token)

    assert remaining[-1].content == f"销售额是 {CANDIDATE_NUMBER} 元"
    assert remaining[-1].usage["total_tokens"] == 18
    assert usage.total_tokens == 18
    assert len({event.rich_component.id for event in [first, *remaining[:-1]]}) == 1


async def test_buffered_provider_interruption_emits_no_candidate_text():
    events = await _chat_events("degraded", llm=_StreamingLlm(fail=True))
    assert CANDIDATE_NUMBER not in json.dumps(events, ensure_ascii=False)
    assert not any(event["rich"]["type"] == "text" for event in events)


async def test_streaming_preserves_tool_calls_and_usage():
    call = ToolCall(id="query-one", name="run_sql", arguments={"sql": "SELECT 1"})
    llm = _StreamingLlm(tool_calls=[call])
    agent = _agent(llm)
    token = CURRENT_BUDGET.set(_usage("ordinary"))
    try:
        events = [
            event async for event in agent._handle_streaming_response(
                LlmRequest(messages=[], user=User(id="stream-test"), stream=True)
            )
        ]
    finally:
        CURRENT_BUDGET.reset(token)
    assert events[-1].tool_calls == [call]
    assert events[-1].usage["total_tokens"] == 18


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_VANNA_E2E") != "1", reason="explicit browser opt-in")
@pytest.mark.parametrize(
    "reason", ["degraded", "tool_budget", "sql_repair_failed", "result_validation_failed", "trusted"]
)
def test_browser_renders_actual_agent_sse_without_blocked_text(reason):
    """Replay actual Agent SSE in a real component, using a controlled provider."""
    from playwright.sync_api import sync_playwright

    events = asyncio.run(_chat_events(reason))
    base_url = os.getenv("VANNA_E2E_BASE_URL", "http://127.0.0.1:32013")
    with sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        try:
            page = browser.new_page()

            def replay_agent_sse(route):
                request = json.loads(route.request.post_data)
                # Keep the real component payloads; use the browser request's
                # envelope ids so stale-conversation filtering is exercised.
                browser_events = [
                    {
                        **event,
                        "conversation_id": request["conversation_id"],
                        "request_id": request["request_id"],
                    }
                    for event in events
                ] if request.get("message") else []
                sse = "".join(
                    f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    for event in browser_events
                )
                route.fulfill(
                    status=200, content_type="text/event-stream", body=sse + "data: [DONE]\n\n"
                )

            page.route("**/api/vanna/v2/chat_sse", replay_agent_sse)
            page.goto(base_url, wait_until="networkidle")
            page.set_content(
                '<script type="module" src="/static/vanna-components.js"></script>'
                '<vanna-chat data-defer-starter sse-endpoint="/api/vanna/v2/chat_sse"></vanna-chat>'
            )
            page.wait_for_function("() => !!document.querySelector('vanna-chat').shadowRoot")
            chat = page.locator("vanna-chat")
            chat.evaluate("""element => {
                window.seenCandidate = false;
                new MutationObserver(records => {
                    for (const record of records) {
                        const texts = [record.target.textContent, ...Array.from(record.addedNodes, n => n.textContent)];
                        if (texts.some(text => text?.includes('987654321'))) window.seenCandidate = true;
                    }
                }).observe(element.shadowRoot, {subtree: true, childList: true, characterData: true});
            }""")
            if chat.locator(".minimized-icon").is_visible():
                chat.locator(".minimized-icon").click()
            chat.locator("textarea.message-input").fill("请说明查询结果")
            chat.locator("button.send-button").click()
            expected = {
                "trusted": CANDIDATE_NUMBER,
                "degraded": "不能据此给出数字结论",
                "tool_budget": "未输出未经完整验证的数值结论",
                "sql_repair_failed": "未通过可信执行或结果合同校验",
                "result_validation_failed": "未通过可信执行或结果合同校验",
            }[reason]
            page.wait_for_function(
                "text => document.querySelector('vanna-chat').shadowRoot.textContent.includes(text)",
                arg=expected,
            )
            assert page.evaluate("window.seenCandidate") is (reason == "trusted")
        finally:
            browser.close()
