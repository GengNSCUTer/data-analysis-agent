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
def test_browser_result_evidence_continuity_and_refresh(result_evidence_data, monkeypatch):
    import pandas as pd
    from fastapi.testclient import TestClient
    from playwright.sync_api import sync_playwright
    import psycopg2
    import psycopg2.extras

    from data_analysis_agent.local_auth import AccountStore
    from data_analysis_agent.postgres_runner import PostgresConnectionSettings
    from data_analysis_agent.result_evidence import RESULT_EVIDENCE_PREFIX
    from result_answer_provider import model_answer
    from data_analysis_agent.server import create_app
    from vanna.core.llm import LlmResponse, LlmStreamChunk
    from vanna.core.tool import ToolCall
    from data_analysis_agent.budget import CURRENT_BUDGET

    captured = []
    from data_analysis_agent.result_evidence import build_result_evidence
    def record_evidence(*args, **kwargs):
        result = build_result_evidence(*args, **kwargs)
        captured.append(json.loads(result.removeprefix(RESULT_EVIDENCE_PREFIX)))
        return result
    monkeypatch.setattr("data_analysis_agent.postgres_runner.build_result_evidence", record_evidence)
    monkeypatch.setattr("data_analysis_agent.chat_runtime.build_result_evidence", record_evidence)

    categories_question = "按商品品类看销售额前十名"
    monthly_question = "2017年按月看销售额趋势"
    intents = {
        categories_question: {"request_kind": "data_query", "action": "new_task", "metric_ids": ["gmv"],
                              "dimensions": ["product_category_name"], "top_n": 10},
        "概述刚才的品类结果": {"request_kind":"result_followup", "action":"keep"},
        "重新概述刚才的品类结果": {"request_kind":"result_followup", "action":"keep"},
        "第九名和第十名的金额分别是多少": {"request_kind": "result_followup", "action": "keep",
                                        "result_inspections": [{"operation": "rows", "row_numbers": [9, 10]}]},
        "这十个品类合计销售额是多少": {"request_kind": "result_followup", "action": "keep",
                                       "result_inspections": [{"operation": "sum", "metric_id": "gmv"}]},
        "比较前两名，差多少": {"request_kind": "result_followup", "action": "keep",
                                "result_inspections": [{"operation": "compare", "metric_id": "gmv", "row_numbers": [1, 2]}]},
        "刚才前两名占全部品类销售额多少": {"request_kind": "result_followup", "action": "keep",
                                            "result_inspections": [{"operation": "share", "metric_id": "gmv", "row_numbers":[1,2], "denominator": "business_total"}]},
        monthly_question: {"request_kind": "data_query", "action": "new_task", "metric_ids": ["gmv"],
                           "dimensions": [], "time_grain": "month", "time_range": {"start": "2017-01-01", "end": "2018-01-01"}},
        "最后一个月表现怎么样": {"request_kind": "result_followup", "action": "keep",
                                    "result_inspections": [{"operation": "rows", "row_numbers": [12]}]},
        "全年每个月都在增长吗": {"request_kind": "result_followup", "action": "keep",
                                "result_inspections": [{"operation": "trend", "metric_id": "gmv", "trend_mode": "strict_increase"}]},
        "有没有从未下降": {"request_kind": "result_followup", "action": "keep",
                          "result_inspections": [{"operation": "trend", "metric_id": "gmv", "trend_mode": "nondecreasing"}]},
        "最后一个月环比": {"request_kind": "result_followup", "action": "keep",
                          "result_inspections": [{"operation": "period_compare", "metric_id": "gmv"}]},
        "按月累计金额": {"request_kind": "result_followup", "action": "keep",
                        "result_inspections": [{"operation": "trend", "metric_id": "gmv", "trend_mode": "cumulative"}]},
        "再给三月滑动平均": {"request_kind": "result_followup", "action": "keep",
                            "result_inspections": [{"operation": "trend", "metric_id": "gmv", "trend_mode": "rolling", "window": 3}]},
        "金额前三的月份": {"request_kind": "result_followup", "action": "keep",
                          "result_inspections": [{"operation": "rank", "metric_id": "gmv", "limit": 3}]},
        "月销售额中位数": {"request_kind": "result_followup", "action": "keep",
                          "result_inspections": [{"operation": "distribution", "metric_id": "gmv"}]},
        "低于一万的月份有几个": {"request_kind": "result_followup", "action": "keep",
                              "result_inspections": [{"operation": "threshold", "metric_id": "gmv", "threshold_value": 10000}]},
        "全年合计和十二月占比一起给我，并画折线图": {"request_kind": "result_followup", "action": "keep", "chart_request":"line",
                                      "result_inspections": [{"operation": "sum", "metric_id": "gmv"},
                                                             {"operation": "share", "metric_id": "gmv", "row_numbers": [12]}]},
        "继续解释全年结果": {"request_kind": "result_followup", "action": "keep"},
        "另查2017年十二月商品成交额，按月": {"request_kind":"data_query", "action":"new_task", "metric_ids":["gmv"],
            "dimensions":[], "time_grain":"month", "time_range":{"start":"2017-12-01", "end":"2018-01-01"}},
        "补上一个月对比环比": {"request_kind":"result_followup", "action":"keep",
            "result_inspections":[{"operation":"period_compare", "metric_id":"gmv"}]},
        "再看十二月同比，没有基期就说明": {"request_kind":"result_followup", "action":"keep",
            "result_inspections":[{"operation":"period_compare", "metric_id":"gmv", "baseline":"previous_year"}]},
        "另按客户州统计客单价": {"request_kind":"data_query", "action":"new_task", "metric_ids":["average_order_value"], "dimensions":["customer_state"]},
        "这些州合在一起算总体客单价": {"request_kind":"result_followup", "action":"keep",
            "result_inspections":[{"operation":"sum", "metric_id":"average_order_value"}]},
        "重新查销售额检查重复关联": {"request_kind":"data_query", "action":"new_task", "metric_ids":["gmv"]},
        "重新正确查询销售额": {"request_kind":"data_query", "action":"update_task", "metric_ids":["gmv"]},
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
    invalid_explanations = []

    class Provider:
        model = "controlled-result-evidence"

        async def send_request(self, request):
            if request.metadata.get("purpose") == "semantic_intent":
                prompt = request.messages[0].content
                question = prompt.rsplit("用户问题: ", 1)[1]
                return LlmResponse(content=json.dumps({**intents[question], "confidence": 0.99}, ensure_ascii=False))
            if request.metadata.get("purpose") == "result_answer_review":
                return LlmResponse(content='{"verdict":"supported","issues":[]}')
            text = request.messages[-1].content.split(RESULT_EVIDENCE_PREFIX, 1)[1]
            # Follow-up prompts append scope/history after the evidence JSON.
            data, _ = json.JSONDecoder().raw_decode(text)
            explanations.append(data)
            question = request.messages[0].content.split("\n", 1)[0]
            if question in {"概述刚才的品类结果", "重新概述刚才的品类结果"}:
                if request.metadata["purpose"] == "result_answer_draft":
                    invalid_explanations.append(question)
                if request.metadata["purpose"] == "result_answer_draft" or question == "重新概述刚才的品类结果":
                    return LlmResponse(content='{"paragraphs":[{"text":"wrong-category销售额987654321。","kind":"fact","evidence_refs":["/sample_rows/0"]}]}')
            return LlmResponse(content=model_answer(data), finish_reason="stop")

        async def stream_request(self, request):
            # No text SSE is fabricated: the Agent builds it from this actual response.
            if request.tools:
                usage = CURRENT_BUDGET.get()
                plan = usage.query_plan
                monthly = bool(plan.get("time_grain"))
                statement = statements["monthly" if monthly else "category"]
                if monthly:
                    bounds = plan["time_range"]
                    statement = statement.replace("2017-01-01", bounds["start"]).replace("2018-01-01", bounds["end"])
                if not monthly and not plan.get("top_n"):
                    statement = statement.removesuffix(" LIMIT 10").replace("ORDER BY gmv DESC", "ORDER BY gmv ASC")
                if plan["metric_ids"] == ["average_order_value"]:
                    grouped = bool(plan["dimensions"])
                    statement = ("WITH order_totals AS (SELECT o.order_id, c.customer_state, SUM(i.price) AS amount "
                        "FROM analytics.fact_orders o JOIN analytics.fact_order_items i ON i.order_id=o.order_id "
                        "JOIN analytics.dim_customers c ON c.customer_id=o.customer_id "
                        "WHERE o.order_status NOT IN ('canceled','unavailable') GROUP BY o.order_id,c.customer_state) "
                        + ("SELECT customer_state," if grouped else "SELECT ")
                        + "AVG(amount) AS average_order_value FROM order_totals"
                        + (" GROUP BY customer_state" if grouped else ""))
                if usage.catalog_question in {"重新查销售额检查重复关联", "重新正确查询销售额"}:
                    statement = ("SELECT SUM(i.price) AS gmv FROM analytics.fact_order_items i "
                        "JOIN analytics.fact_orders o ON o.order_id=i.order_id "
                        + ("CROSS JOIN (SELECT 1 AS n UNION ALL SELECT 2 AS n) duplicate_rows "
                           if usage.catalog_question == "重新查销售额检查重复关联" else "")
                        + "WHERE o.order_status NOT IN ('canceled','unavailable')")
                yield LlmStreamChunk(tool_calls=[ToolCall(id="sql", name="run_sql",
                                                          arguments={"sql": statement})],
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
    from data_analysis_agent.semantic_catalog import CatalogLoader
    from data_analysis_agent.runtime_workspace import load_runtime_workspace
    assert CatalogLoader(load_runtime_workspace().profile).load().row_keys
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
                                          and response.request.post_data_json.get("message") == question) as pending:
                    chat.locator("button.send-button").click()
                page.wait_for_function("() => !document.querySelector('vanna-chat').shadowRoot.querySelector('textarea.message-input').disabled")
                cid = chat.evaluate("element => element.conversationId")
                detail = client.get(f"/api/project/conversations/{cid}").json()
                with psycopg2.connect(host=settings.host, port=settings.port, dbname=settings.database,
                                      user=settings.writer_user) as connection:
                    with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                        cursor.execute("SELECT termination_reason,sql_calls_used,catalog_trace FROM app.agent_runs WHERE conversation_id=%s ORDER BY started_at DESC LIMIT 1", (cid,))
                        run = dict(cursor.fetchone())
                if question == "重新查销售额检查重复关联":
                    assert run["termination_reason"] != "completed"
                    assert run["sql_calls_used"] == 1
                    assert run["catalog_trace"]["join_evidence"]["status"] == "amplified"
                    assert "未输出未经验证的数字结论" in detail["messages"][-1]["content"]
                    observations.append({"question":question, "join_evidence":run["catalog_trace"]["join_evidence"],
                                         "termination_reason":run["termination_reason"]})
                    continue
                assert run["termination_reason"] == "completed", run
                assert run["sql_calls_used"] == int(question in {categories_question, monthly_question, "刚才前两名占全部品类销售额多少", "另查2017年十二月商品成交额，按月", "补上一个月对比环比", "再看十二月同比，没有基期就说明", "另按客户州统计客单价", "这些州合在一起算总体客单价", "重新正确查询销售额"})
                if run["sql_calls_used"]:
                    assert run["catalog_trace"]["join_evidence"]["status"] in {"checked", "partial"}
                assert detail["trusted_result_artifact"]
                current = captured[-1]
                answer = detail["messages"][-1]["content"]
                if question in {"概述刚才的品类结果", "重新概述刚才的品类结果"}:
                    assert "987654321" not in answer and "wrong-category" not in answer
                    assert '"paragraphs"' not in answer
                    assert "987654321" not in pending.value.text()
                    assert run["catalog_trace"]["result_explanation"]["revision_count"] == 1
                    if question == "重新概述刚才的品类结果":
                        assert "文字解释未通过核验" in answer
                    chat.get_by_text("本次回答的数据依据", exact=True).last.wait_for(state="visible")
                    page.screenshot(path=str(output / "reference-fallback.png"), full_page=True)
                if question not in {"概述刚才的品类结果", "重新概述刚才的品类结果"}:
                    assert "模型说明" in answer
                if question == categories_question:
                    assert len(current["sample_rows"]) == 10
                    assert current["coverage"] == "complete"
                elif question == "第九名和第十名的金额分别是多少":
                    assert current["row_numbers"] == [9, 10]
                    assert run["catalog_trace"]["result_inspection"]["source_row_count"] == 10
                elif question == "这十个品类合计销售额是多少":
                    assert current["analyses"][0]["value"] == sum(row["gmv"] for row in current["sample_rows"])
                    assert current["scope"] == "returned_top_n"
                elif question == "刚才前两名占全部品类销售额多少":
                    calculation = current["analyses"][0]
                    assert current["source_row_count"] > 10
                    ordered = sorted((r["gmv"] for r in current["sample_rows"]), reverse=True)
                    assert calculation["share_percent"] == pytest.approx(sum(ordered[:2]) / sum(ordered) * 100)
                    assert calculation["numerator_rows"] == 2
                    assert "analysis_acquisition" in run["catalog_trace"]
                elif question == monthly_question:
                    assert current["row_count"] == 12
                    assert current["sample_rows"][-1]["time"].startswith("2017-12")
                elif question == "最后一个月表现怎么样":
                    assert current["row_numbers"] == [12]
                elif question == "全年每个月都在增长吗":
                    assert current["analyses"][0]["groups"][0]["judgment"] == "false"
                    assert "不成立" in answer and "反例" in answer
                elif question == "月销售额中位数":
                    assert current["analyses"][0]["median"] == pd.Series([r["gmv"] for r in current["sample_rows"]]).median()
                elif question == "全年合计和十二月占比一起给我，并画折线图":
                    assert len(current["analyses"]) == 2
                    assert detail["charts"]
                    chat.locator(".js-plotly-plot").last.wait_for(state="visible")
                elif question == "补上一个月对比环比":
                    assert current["source_row_count"] == 2
                    assert current["analyses"][0]["comparisons"][0]["baseline_period"] == "2017-11"
                    assert "analysis_acquisition" in run["catalog_trace"]
                elif question == "再看十二月同比，没有基期就说明":
                    assert current["analyses"][0]["status"] == "query_required"
                    assert "基期" in answer and "部分分组" in answer
                elif question == "这些州合在一起算总体客单价":
                    assert current["source_row_count"] == 1 and "analysis_acquisition" in run["catalog_trace"]
                    with psycopg2.connect(os.environ["RESULT_EVIDENCE_TEST_ADMIN_DSN"]) as connection:
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT i.price FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id WHERE o.order_status NOT IN ('canceled','unavailable')")
                            prices = [r[0] for r in cursor.fetchall()]
                    assert current["sample_rows"][0]["average_order_value"] == pytest.approx(float(sum(prices)/len(prices)))
                elif question == "继续解释全年结果":
                    assert current["coverage"] == "complete" and len(current["sample_rows"]) == 12
                elif question == "重新正确查询销售额":
                    assert run["catalog_trace"]["join_evidence"]["status"] == "checked"
                    assert run["catalog_trace"]["join_evidence"]["checks"][0]["multiplicity_lower_bound"] == 1
                observations.append({"question": question, "sql_calls": run["sql_calls_used"],
                                     "source_rows": current["source_row_count"], "coverage": current["coverage"],
                                     "inspection": run["catalog_trace"].get("result_inspection"),
                                     "operations": [a["operation"] for a in current["analyses"]],
                                     "answer": answer})
            csv_response = client.get(f"/api/project/conversations/{cid}/result.csv")
            assert csv_response.is_success
            import io
            assert len(pd.read_csv(io.StringIO(csv_response.text))) == 1
            page.screenshot(path=str(output / "result-evidence-continuity.png"), full_page=True)
            assert not errors
            assert len(invalid_explanations) == 2
        finally:
            (output / "observations.json").write_text(json.dumps(observations, ensure_ascii=False, indent=2))
            browser.close()
