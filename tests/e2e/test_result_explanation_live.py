"""Opt-in real browser/provider/reader checks of entity-bound explanations."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import secrets
import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.mark.skipif(os.getenv("RUN_LIVE_RESULT_ANALYSIS") != "1", reason="opt-in real model/browser")
def test_live_entity_bound_explanation_and_refresh(monkeypatch):
    import pandas as pd
    import psycopg2
    import psycopg2.extras
    from fastapi.testclient import TestClient
    from playwright.sync_api import sync_playwright

    from data_analysis_agent.local_auth import AccountStore
    from data_analysis_agent.llm_observability import ObservedLlmService
    from data_analysis_agent.postgres_runner import PostgresConnectionSettings
    from data_analysis_agent.result_explanation import ExplanationSelection
    from data_analysis_agent.runtime_workspace import load_runtime_workspace
    from data_analysis_agent.server import create_app

    workspace = load_runtime_workspace().profile
    settings = PostgresConnectionSettings.from_environment()
    thelook = workspace.workspace_id == "thelook-demo"
    metric, year = ("completed_sale_amount", 2023) if thelook else ("gmv", 2017)
    source = ("SELECT u.state, SUM(i.sale_price) FROM analytics.order_items i "
              "JOIN analytics.orders o ON o.order_id=i.order_id JOIN analytics.users u ON u.id=o.user_id "
              "WHERE o.status='Complete' AND o.created_at>=%s AND o.created_at<%s GROUP BY u.state") if thelook else (
              "SELECT c.customer_state, SUM(i.price) FROM analytics.fact_order_items i "
              "JOIN analytics.fact_orders o ON o.order_id=i.order_id "
              "JOIN analytics.dim_customers c ON c.customer_id=o.customer_id "
              "WHERE o.order_status NOT IN ('canceled','unavailable') "
              "AND o.order_purchase_timestamp>=%s AND o.order_purchase_timestamp<%s GROUP BY c.customer_state")
    with psycopg2.connect(host=settings.host, port=settings.port, dbname=settings.database, user=workspace.reader_role) as connection:
        with connection.cursor() as cursor:
            cursor.execute(source, (f"{year}-01-01", f"{year+1}-01-01"))
            expected = {state: float(value) for state, value in cursor.fetchall()}
    if thelook:
        expected = dict(sorted(expected.items(), key=lambda item: item[1], reverse=True)[:10])
    dimension = "customer_state"  # The runtime's result alias, independent of source column name.
    root = Path(os.environ["RESULT_ANALYSIS_LIVE_OUTPUT"]) / workspace.workspace_id
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    observations, explanations = [], []
    send = ObservedLlmService._send_request

    async def observe(self, request):
        response = await send(self, request)
        if request.metadata.get("purpose") == "result_explanation":
            explanations.append({"model": self.model, "content": response.content,
                                 "tools_enabled": bool(request.tools)})
            (root / "provider-selections.json").write_text(json.dumps(explanations, ensure_ascii=False, indent=2))
        return response

    monkeypatch.setattr(ObservedLlmService, "_send_request", observe)
    username, password = "explain_" + uuid.uuid4().hex[:12], secrets.token_hex(16)
    from dataclasses import replace
    AccountStore(replace(settings, writer_user=workspace.writer_role)).create_account(username, password, "analyst")
    amount = "已完成销售额" if thelook else "商品成交额"
    scope = "销售额前十名" if thelook else "各州金额"
    questions = [f"按客户州统计{year}年{amount}，列出{scope}，不用画图。",
                 "用一句话概括刚才的各州结果，不要重新查询。",
                 "把这份各州结果画成柱状图，不要重新查询。",
                 "继续解释刚才各州结果，不用重新查询和画图。"]
    with TestClient(create_app()) as client, sync_playwright() as engine:
        assert client.post("/api/auth/login", json={"username": username, "password": password}).is_success
        browser = engine.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width":1440, "height":1100})
            page.set_default_timeout(240000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def forward(route):
                request = route.request
                response = client.request(request.method, request.url.removeprefix("http://testserver"),
                    content=request.post_data_buffer,
                    headers={k:v for k,v in request.headers.items() if k not in {"host", "content-length"}})
                route.fulfill(status=response.status_code, body=response.content,
                    headers={k:v for k,v in response.headers.items() if k not in {"content-length", "content-encoding", "transfer-encoding"}})

            page.route("http://testserver/**", forward)
            page.goto("http://testserver/embedded-copilot", wait_until="networkidle")
            for index, question in enumerate(questions):
                if index == 3:
                    page.reload(wait_until="networkidle")
                chat = page.locator("vanna-chat")
                page.wait_for_function("() => !!document.querySelector('vanna-chat')?.shadowRoot")
                if chat.locator(".minimized-icon").is_visible():
                    chat.locator(".minimized-icon").click()
                chat.locator("textarea.message-input").fill(question)
                with page.expect_response(lambda r, q=question: r.url.endswith("/chat_sse") and r.request.post_data_json.get("message") == q):
                    chat.locator("button.send-button").click()
                page.wait_for_function("() => !document.querySelector('vanna-chat').shadowRoot.querySelector('textarea.message-input').disabled")
                cid = chat.evaluate("el => el.conversationId")
                detail = client.get(f"/api/project/conversations/{cid}").json()
                with psycopg2.connect(host=settings.host, port=settings.port, dbname=settings.database, user=workspace.writer_role) as connection:
                    with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                        cursor.execute("SELECT termination_reason,sql_calls_used,catalog_trace FROM app.agent_runs WHERE conversation_id=%s ORDER BY started_at DESC LIMIT 1", (cid,))
                        run = dict(cursor.fetchone())
                answer = detail["messages"][-1]["content"]
                observations.append({"question": question, "answer":answer, **run})
                (root / "observations.json").write_text(json.dumps(observations, ensure_ascii=False, indent=2))
                assert run["termination_reason"] == "completed", observations[-1]
                assert run["sql_calls_used"] == (1 if index == 0 else 0), observations[-1]
                assert '"facts"' not in answer and '"row_number"' not in answer
                if index == 0:
                    response = client.get(f"/api/project/conversations/{cid}/result.csv")
                    assert response.is_success
                    frame = pd.read_csv(io.StringIO(response.text))
                    assert dict(zip(frame[dimension], frame[metric])) == pytest.approx(expected)
                    assert run["catalog_trace"]["result_explanation"]["server_rendered_references"]
                if index == 2:
                    assert detail["charts"]
                    chat.locator(".js-plotly-plot").last.wait_for(state="visible")
                else:
                    chat.get_by_text("本次回答的数据依据", exact=True).last.wait_for(state="visible")
                page.screenshot(path=str(root / f"turn-{index+1}.png"), full_page=True)
                print(f"{workspace.workspace_id}: explanation turn {index+1} completed", flush=True)
            assert explanations, "no real explanation provider call observed"
            assert not any(entry["tools_enabled"] for entry in explanations)
            for entry in explanations:
                ExplanationSelection.model_validate_json(entry["content"])
            assert not errors
        finally:
            browser.close()
