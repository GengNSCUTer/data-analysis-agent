"""Real Web Component -> product ASGI -> reader/artifact -> explanation.

Requires a disposable synthetic PostgreSQL database. The model is controlled;
SSE and query results are produced by the actual product, not canned events.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture
def result_evidence_data():
    """Temporarily enrich only an explicitly supplied synthetic test database."""
    import psycopg2
    import psycopg2.extras

    dsn = os.getenv("RESULT_EVIDENCE_TEST_ADMIN_DSN")
    if not dsn:
        pytest.skip("provide RESULT_EVIDENCE_TEST_ADMIN_DSN for a disposable synthetic DB")
    with psycopg2.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT dataset_version_id FROM analytics.dataset_versions")
            assert {row[0] for row in cursor.fetchall()} == {"synthetic-olist-v1"}
            cursor.execute("SELECT product_id, product_category_name FROM analytics.dim_products")
            categories = cursor.fetchall()
            cursor.execute("SELECT i.order_id,i.order_item_id,i.price FROM analytics.fact_order_items i "
                           "JOIN analytics.fact_orders o ON o.order_id=i.order_id "
                           "WHERE o.order_purchase_timestamp >= DATE '2017-12-01' AND o.order_purchase_timestamp < DATE '2018-01-01'")
            prices = cursor.fetchall()
            cursor.execute("UPDATE analytics.dim_products SET product_category_name='synthetic_category_'||product_id")
            psycopg2.extras.execute_batch(cursor, "UPDATE analytics.fact_order_items SET price=1000 WHERE order_id=%s AND order_item_id=%s",
                                         [(order_id, item_id) for order_id, item_id, _ in prices])
        connection.commit()
        try:
            yield
        finally:
            with connection.cursor() as cursor:
                psycopg2.extras.execute_batch(cursor, "UPDATE analytics.dim_products SET product_category_name=%s WHERE product_id=%s",
                                             [(category, product_id) for product_id, category in categories])
                psycopg2.extras.execute_batch(cursor, "UPDATE analytics.fact_order_items SET price=%s WHERE order_id=%s AND order_item_id=%s",
                                             [(price, order_id, item_id) for order_id, item_id, price in prices])
            connection.commit()


@pytest.mark.skipif(os.getenv("RUN_VANNA_E2E") != "1" or os.getenv("RUN_PROJECT_DB") != "1",
                    reason="requires an explicitly enabled isolated DB/browser run")
def test_browser_result_evidence_continuity_and_refresh(result_evidence_data):
    import pandas as pd
    from fastapi.testclient import TestClient
    from playwright.sync_api import sync_playwright
    import psycopg2
    import psycopg2.extras

    from data_analysis_agent.local_auth import AccountStore
    from data_analysis_agent.postgres_runner import PostgresConnectionSettings
    from data_analysis_agent.result_evidence import RESULT_EVIDENCE_PREFIX, render_result_facts
    from data_analysis_agent.server import create_app
    from vanna.core.llm import LlmResponse, LlmStreamChunk
    from vanna.core.tool import ToolCall

    categories_question = "按商品品类看销售额前十名"
    monthly_question = "2017年按月看销售额趋势"
    intents = {
        categories_question: {"request_kind": "data_query", "action": "new_task", "metric_ids": ["gmv"],
                              "dimensions": ["product_category_name"], "top_n": 10},
        "第九名和第十名的金额分别是多少": {"request_kind": "result_followup", "action": "keep",
                                        "result_inspection": {"operation": "rows", "row_numbers": [9, 10]}},
        "这十个品类合计销售额是多少": {"request_kind": "result_followup", "action": "keep",
                                       "result_inspection": {"operation": "sum", "metric_id": "gmv"}},
        "比较前两名，差多少": {"request_kind": "result_followup", "action": "keep",
                                "result_inspection": {"operation": "compare", "metric_id": "gmv", "row_numbers": [1, 2]}},
        monthly_question: {"request_kind": "data_query", "action": "new_task", "metric_ids": ["gmv"],
                           "dimensions": [], "time_grain": "month", "time_range": {"start": "2017-01-01", "end": "2018-01-01"}},
        "最后一个月表现怎么样": {"request_kind": "result_followup", "action": "keep",
                                    "result_inspection": {"operation": "rows", "row_numbers": [12]}},
        "继续解释全年结果": {"request_kind": "result_followup", "action": "keep"},
    }
    statements = {
        "category": """SELECT p.product_category_name, SUM(i.price) AS gmv
FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id
JOIN analytics.dim_products p ON p.product_id=i.product_id
WHERE o.order_status NOT IN ('canceled','unavailable')
GROUP BY p.product_category_name ORDER BY gmv DESC LIMIT 10""",
        "monthly": """SELECT DATE_TRUNC('month',o.order_purchase_timestamp) AS time, SUM(i.price) AS gmv
FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id
WHERE o.order_status NOT IN ('canceled','unavailable')
AND o.order_purchase_timestamp >= DATE '2017-01-01' AND o.order_purchase_timestamp < DATE '2018-01-01'
GROUP BY DATE_TRUNC('month',o.order_purchase_timestamp) ORDER BY time""",
    }
    explanations = []

    class Provider:
        model = "controlled-result-evidence"

        async def send_request(self, request):
            if request.metadata.get("purpose") == "semantic_intent":
                prompt = request.messages[0].content
                question = prompt.rsplit("用户问题: ", 1)[1]
                return LlmResponse(content=json.dumps({**intents[question], "confidence": 0.99}, ensure_ascii=False))
            text = request.messages[0].content.split(RESULT_EVIDENCE_PREFIX, 1)[1]
            # Follow-up prompts append scope/history after the evidence JSON.
            data, _ = json.JSONDecoder().raw_decode(text)
            explanations.append(data)
            return LlmResponse(content=render_result_facts(data), finish_reason="stop")

        async def stream_request(self, request):
            # No text SSE is fabricated: the Agent builds it from this actual response.
            if request.tools:
                monthly = "2017年按月看销售额趋势" in request.messages[-1].content
                yield LlmStreamChunk(tool_calls=[ToolCall(id="sql", name="run_sql",
                                                          arguments={"sql": statements["monthly" if monthly else "category"]})],
                                     finish_reason="tool_calls")
            else:
                response = await self.send_request(request)
                yield LlmStreamChunk(content=response.content, finish_reason="stop")

        async def validate_tools(self, tools):
            return []

        async def aclose(self):
            pass

    settings = PostgresConnectionSettings.from_environment()
    username = "evidence_" + uuid.uuid4().hex[:12]
    password = secrets.token_hex(16)
    AccountStore().create_account(username, password, "analyst")
    output = Path(os.getenv("RESULT_EVIDENCE_BROWSER_OUTPUT", "/tmp/result-evidence-browser"))
    output.mkdir(parents=True, exist_ok=True)
    observations = []
    with TestClient(create_app(llm_provider=Provider())) as client, sync_playwright() as engine:
        assert client.post("/api/auth/login", json={"username": username, "password": password}).is_success
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
            chat = page.locator("vanna-chat")
            page.wait_for_function("() => !!document.querySelector('vanna-chat')?.shadowRoot")
            if chat.locator(".minimized-icon").is_visible():
                chat.locator(".minimized-icon").click()
            for question in intents:
                if question == "继续解释全年结果":
                    # Reload uses the same real session and persisted conversation.
                    page.reload(wait_until="networkidle")
                    chat = page.locator("vanna-chat")
                    if chat.locator(".minimized-icon").is_visible():
                        chat.locator(".minimized-icon").click()
                chat.locator("textarea.message-input").fill(question)
                with page.expect_response(lambda response, question=question: response.url.endswith("/chat_sse")
                                          and response.request.post_data_json.get("message") == question):
                    chat.locator("button.send-button").click()
                page.wait_for_function("() => !document.querySelector('vanna-chat').shadowRoot.querySelector('textarea.message-input').disabled")
                cid = chat.evaluate("element => element.conversationId")
                detail = client.get(f"/api/project/conversations/{cid}").json()
                with psycopg2.connect(host=settings.host, port=settings.port, dbname=settings.database,
                                      user=settings.writer_user) as connection:
                    with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                        cursor.execute("SELECT termination_reason,sql_calls_used,catalog_trace FROM app.agent_runs WHERE conversation_id=%s ORDER BY started_at DESC LIMIT 1", (cid,))
                        run = dict(cursor.fetchone())
                assert run["termination_reason"] == "completed", run
                assert run["sql_calls_used"] == int(question in {categories_question, monthly_question})
                assert detail["trusted_result_artifact"]
                current = explanations[-1]
                if question == categories_question:
                    assert len(current["sample_rows"]) == 10
                    assert current["coverage"] == "complete"
                elif question == "第九名和第十名的金额分别是多少":
                    assert current["row_numbers"] == [9, 10]
                    assert run["catalog_trace"]["result_inspection"]["source_row_count"] == 10
                elif question == "这十个品类合计销售额是多少":
                    assert current["calculation"]["value"] == sum(row["gmv"] for row in current["sample_rows"])
                    assert current["scope"] == "returned_top_n"
                elif question == monthly_question:
                    assert current["row_count"] == 12
                    assert current["sample_rows"][-1]["time"].startswith("2017-12")
                elif question == "最后一个月表现怎么样":
                    assert current["row_numbers"] == [12]
                elif question == "继续解释全年结果":
                    assert current["coverage"] == "complete" and len(current["sample_rows"]) == 12
                observations.append({"question": question, "sql_calls": run["sql_calls_used"],
                                     "source_rows": current["source_row_count"], "coverage": current["coverage"],
                                     "inspection": run["catalog_trace"].get("result_inspection")})
            csv_response = client.get(f"/api/project/conversations/{cid}/result.csv")
            assert csv_response.is_success
            import io
            assert len(pd.read_csv(io.StringIO(csv_response.text))) == 12
            page.screenshot(path=str(output / "result-evidence-continuity.png"), full_page=True)
            assert not errors
        finally:
            (output / "observations.json").write_text(json.dumps(observations, ensure_ascii=False, indent=2))
            browser.close()
