"""Real browser -> authenticated settings -> runtime -> reader -> presenter/CSV.

Model responses are controlled; product SSE, PostgreSQL and artifacts are real.
No business data is inserted or changed.
"""
from __future__ import annotations

import csv
import io
import json
import os
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.mark.skipif(os.getenv("RUN_VANNA_E2E") != "1" or os.getenv("RUN_PROJECT_DB") != "1",
                    reason="enable real PostgreSQL/browser checks")
def test_browser_result_settings_change_actual_budget_preview_and_survive_refresh(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from playwright.sync_api import sync_playwright
    from data_analysis_agent import server
    from data_analysis_agent.budget import CURRENT_BUDGET
    from data_analysis_agent.result_evidence import RESULT_EVIDENCE_PREFIX
    from vanna.core.llm import LlmResponse, LlmStreamChunk
    from vanna.core.tool import ToolCall

    plans = []

    class Provider:
        model = "controlled-result-settings"

        async def send_request(self, request):
            if request.metadata.get("purpose") == "semantic_intent":
                return LlmResponse(content=json.dumps({"request_kind": "data_query", "action": "new_task",
                    "metric_ids": ["paid_order_count"], "dimensions": ["customer_city"], "confidence": 0.99}))
            if request.metadata.get("purpose") == "result_answer_review":
                return LlmResponse(content='{"verdict":"supported","issues":[]}')
            evidence, _ = json.JSONDecoder().raw_decode(
                request.messages[-1].content.split(RESULT_EVIDENCE_PREFIX, 1)[1])
            return LlmResponse(content=json.dumps({"paragraphs": [{
                "text": f"本次查询完整返回{evidence['source_row_count']}个城市分组，表格展示部分结果。",
                "kind": "fact", "evidence_refs": ["/source_row_count"],
            }]}, ensure_ascii=False))

        async def stream_request(self, request):
            if request.tools:
                plans.append(CURRENT_BUDGET.get().query_plan)
                yield LlmStreamChunk(tool_calls=[ToolCall(id="settings-sql", name="run_sql", arguments={
                    "sql": "SELECT c.customer_city, COUNT(*) AS paid_order_count "
                           "FROM analytics.fact_orders o JOIN analytics.dim_customers c ON c.customer_id=o.customer_id "
                           "WHERE o.order_status NOT IN ('canceled','unavailable') "
                           "GROUP BY c.customer_city ORDER BY c.customer_city",
                })], finish_reason="tool_calls")
            else:
                response = await self.send_request(request)
                yield LlmStreamChunk(content=response.content, finish_reason="stop")

        async def validate_tools(self, tools):
            return []

        async def aclose(self):
            pass

    username = "browser_settings_" + uuid4().hex[:10]
    monkeypatch.setenv("DATA_ANALYSIS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_ANALYSIS_BOOTSTRAP_USERNAME", username)
    monkeypatch.setenv("DATA_ANALYSIS_BOOTSTRAP_PASSWORD", "browser-settings-password")
    monkeypatch.setattr(server, "RESULT_ARTIFACT_DIRECTORY", tmp_path / "artifacts")
    with TestClient(server.create_app(llm_provider=Provider())) as client, sync_playwright() as engine:
        assert client.post("/api/auth/login", json={"username": username, "password": "browser-settings-password"}).is_success
        browser = engine.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def forward(route):
                request = route.request
                response = client.request(request.method, request.url.removeprefix("http://testserver"),
                    content=request.post_data_buffer,
                    headers={key: value for key, value in request.headers.items() if key not in {"host", "content-length"}})
                route.fulfill(status=response.status_code, body=response.content,
                    headers={key: value for key, value in response.headers.items()
                             if key not in {"content-length", "content-encoding", "transfer-encoding"}})

            page.route("http://testserver/**", forward)
            page.goto("http://testserver/embedded-copilot", wait_until="networkidle")
            page.get_by_text("查询与展示设置", exact=True).click()
            form = page.locator("#result-settings-form")
            button = form.get_by_role("button")
            button.wait_for(state="visible")
            page.wait_for_function("() => !document.querySelector('#result-settings-form button').disabled")
            assert form.locator('[name="execution_max_rows"]').input_value() == "5000"
            assert form.locator('[name="preview_rows"]').input_value() == "200"

            def save(execution, preview):
                form.locator('[name="execution_max_rows"]').fill(str(execution))
                form.locator('[name="preview_rows"]').fill(str(preview))
                with page.expect_response(lambda response: response.url.endswith("/result-settings")
                                          and response.request.method == "PUT") as result:
                    button.click()
                assert result.value.status == 200
                page.wait_for_function("() => !document.querySelector('#result-settings-form button').disabled")

            def ask():
                chat = page.locator("vanna-chat")
                if chat.locator(".minimized-icon").is_visible():
                    chat.locator(".minimized-icon").click()
                chat.locator("textarea.message-input").fill("按客户城市统计有效订单数")
                with page.expect_response(lambda response: response.url.endswith("/chat_sse")) as result:
                    chat.locator("button.send-button").click()
                assert result.value.status == 200
                page.wait_for_function("() => !document.querySelector('vanna-chat').shadowRoot.querySelector('textarea.message-input').disabled")
                return chat, chat.evaluate("element => element.conversationId"), result.value.text()

            save(3000, 20)
            _, _, denied = ask()
            assert not plans, "preflight must use the saved user ceiling before SQL generation"
            assert "缩小" in denied or "预算" in denied
            save(6000, 300)
            page.reload(wait_until="networkidle")
            page.get_by_text("查询与展示设置", exact=True).click()
            page.wait_for_function("() => !document.querySelector('#result-settings-form button').disabled")
            assert form.locator('[name="execution_max_rows"]').input_value() == "6000"
            assert form.locator('[name="preview_rows"]').input_value() == "300"
            chat, cid, body = ask()
            assert len(plans) == 1
            assert plans[0]["result_budget"]["execution_max_rows"] == 6000
            assert plans[0]["result_budget"]["preview_rows"] == 300
            events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: {")]
            frames = [event["rich"]["data"] for event in events
                      if event.get("rich", {}).get("type") == "dataframe"]
            assert len(frames) == 1
            frame = frames[0]
            assert frame["preview_row_count"] == len(frame["data"]) == 300
            assert frame["total_row_count"] > 300 and frame["display_truncated"]
            assert chat.locator(".dataframe-table tbody tr").count() == 300
            response = client.get(f"/api/project/conversations/{cid}/result.csv")
            assert response.is_success
            rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8"))))
            assert len(rows) == frame["total_row_count"]
            detail = client.get(f"/api/project/conversations/{cid}").json()
            assert "本次查询完整返回" in detail["messages"][-1]["content"]
            # Settings apply to future SQL only, never truncate an existing artifact.
            save(10, 10)
            assert client.get(f"/api/project/conversations/{cid}/result.csv").content == response.content
            assert len(plans) == 1
            page.screenshot(path=str(tmp_path / "result-settings.png"), full_page=True)
            assert not errors
        finally:
            browser.close()
