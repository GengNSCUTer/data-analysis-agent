"""Opt-in public-data browser + real provider acceptance, without business fixture writes.

Set RUN_LIVE_RESULT_ANALYSIS=1 and configure a runtime workspace as for the app.
The browser forwards HTTP to the actual ASGI app. No intent, SQL, SSE or model
response is mocked. Reports contain public data and remain outside the repository.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import secrets
import time
import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.mark.skipif(os.getenv("RUN_LIVE_RESULT_ANALYSIS") != "1", reason="opt-in real model/browser")
def test_live_result_analysis_multiturn(monkeypatch):
    import pandas as pd
    import psycopg2
    import psycopg2.extras
    from fastapi.testclient import TestClient
    from playwright.sync_api import sync_playwright
    from data_analysis_agent.local_auth import AccountStore
    from data_analysis_agent.runtime_workspace import load_runtime_workspace
    from data_analysis_agent.postgres_runner import PostgresConnectionSettings
    from data_analysis_agent.server import create_app
    from data_analysis_agent.result_evidence import RESULT_EVIDENCE_PREFIX

    workspace = load_runtime_workspace().profile
    settings = PostgresConnectionSettings.from_environment()
    thelook = workspace.workspace_id == "thelook-demo"
    metric = "completed_sale_amount" if thelook else "gmv"
    year = 2023 if thelook else 2017
    source = ("SELECT DATE_TRUNC('month',o.created_at) AS time, SUM(i.sale_price) AS value "
              "FROM analytics.order_items i JOIN analytics.orders o ON o.order_id=i.order_id "
              "WHERE o.status='Complete' AND o.created_at>=%s AND o.created_at<%s GROUP BY 1 ORDER BY 1") if thelook else (
              "SELECT DATE_TRUNC('month',o.order_purchase_timestamp) AS time, SUM(i.price) AS value "
              "FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id "
              "WHERE o.order_status NOT IN ('canceled','unavailable') "
              "AND o.order_purchase_timestamp>=%s AND o.order_purchase_timestamp<%s GROUP BY 1 ORDER BY 1")
    with psycopg2.connect(host=settings.host, port=settings.port, dbname=settings.database, user=workspace.reader_role) as connection:
        with connection.cursor() as cursor:
            cursor.execute(source, (f"{year}-01-01", f"{year+1}-01-01"))
            expected = cursor.fetchall()
    assert len(expected) == 12
    values = [float(value) for _, value in expected]
    username, password = "analysis_" + uuid.uuid4().hex[:12], secrets.token_hex(16)
    from dataclasses import replace
    AccountStore(replace(settings, writer_user=workspace.writer_role)).create_account(username, password, "analyst")
    root = Path(os.environ["RESULT_ANALYSIS_LIVE_OUTPUT"]) / workspace.workspace_id
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    records = []
    # Observe the real provider output; do not substitute an intent or response.
    from data_analysis_agent import semantic_intent
    original_parse = semantic_intent.parse_semantic_intent
    intent_outputs = []

    def record_parse(text, *args, **kwargs):
        entry = {"output": text}
        intent_outputs.append(entry)
        try:
            return original_parse(text, *args, **kwargs)
        except semantic_intent.SemanticIntentError as error:
            entry["error"] = str(error)
            cause = error.__cause__
            if hasattr(cause, "errors"):
                entry["validation_errors"] = cause.errors(include_input=False, include_context=False)
            raise
        finally:
            (root / "intent-outputs.json").write_text(json.dumps(intent_outputs, ensure_ascii=False, indent=2))

    monkeypatch.setattr(semantic_intent, "parse_semantic_intent", record_parse)
    from data_analysis_agent import chat_runtime, postgres_runner
    from data_analysis_agent.result_evidence import build_result_evidence
    evidence_calls = []
    def record_evidence(*args, **kwargs):
        summary = build_result_evidence(*args, **kwargs)
        evidence_calls.append(json.loads(summary.removeprefix(RESULT_EVIDENCE_PREFIX)))
        return summary
    monkeypatch.setattr(chat_runtime, "build_result_evidence", record_evidence)
    monkeypatch.setattr(postgres_runner, "build_result_evidence", record_evidence)
    # These are development acceptance questions, not protected training holdouts.
    questions = [
        (f"按月统计{year}年{'已完成销售额' if thelook else '商品成交额'}，判断是不是每个月都增长，并给出反例。不用画图。", "trend", 1),
        ("十二月比十一月变化多少？请给差额和环比。", "period_compare", 0),
        ("对这份月度结果，按金额排出最高的三个月。", "rank", 0),
        ("这十二个月金额的中位数和四分位数呢？", "distribution", 0),
        ("对这份按月结果求和，并计算第12行占这些结果合计的比例。", "share", 0),
        ("把这份全年月度结果换成折线图，不用重新查询。", "chart", 0),
        ("回到刚才的全年月度结果，是否每个月都上涨？哪次下跌最多？", "trend", 0),
    ]
    with TestClient(create_app()) as client, sync_playwright() as engine:
        assert client.post("/api/auth/login", json={"username": username, "password": password}).is_success
        browser = engine.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
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
            for index, (question, operation, sql_calls) in enumerate(questions):
                if index == 6:
                    page.reload(wait_until="networkidle")
                chat = page.locator("vanna-chat")
                page.wait_for_function("() => !!document.querySelector('vanna-chat')?.shadowRoot")
                if chat.locator(".minimized-icon").is_visible():
                    chat.locator(".minimized-icon").click()
                started = time.monotonic()
                chat.locator("textarea.message-input").fill(question)
                with page.expect_response(lambda r, question=question: r.url.endswith("/chat_sse") and r.request.post_data_json.get("message") == question) as pending:
                    chat.locator("button.send-button").click()
                page.wait_for_function("() => !document.querySelector('vanna-chat').shadowRoot.querySelector('textarea.message-input').disabled")
                cid = chat.evaluate("el => el.conversationId")
                detail = client.get(f"/api/project/conversations/{cid}").json()
                with psycopg2.connect(host=settings.host, port=settings.port, dbname=settings.database, user=workspace.writer_role) as connection:
                    with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                        cursor.execute("SELECT termination_reason,sql_calls_used,catalog_trace FROM app.agent_runs WHERE conversation_id=%s ORDER BY started_at DESC LIMIT 1", (cid,))
                        run = dict(cursor.fetchone())
                answer = detail["messages"][-1]["content"]
                records.append({"question": question, "seconds": round(time.monotonic()-started, 2), "answer": answer, **run})
                (root / "observations.json").write_text(json.dumps(records, ensure_ascii=False, indent=2))
                (root / f"response-{index}.txt").write_text(pending.value.text())
                assert run["termination_reason"] == "completed", {"question": question, "answer": answer, "terminal":run["termination_reason"]}
                assert run["sql_calls_used"] == sql_calls, question
                if index == 0:
                    assert run["catalog_trace"]["join_evidence"]["status"] in {"checked", "partial"}
                    assert all(r["multiplicity_lower_bound"] == 1 for r in run["catalog_trace"]["join_evidence"]["checks"])
                    csv = pd.read_csv(io.StringIO(client.get(f"/api/project/conversations/{cid}/result.csv").text))
                    assert csv[metric].tolist() == pytest.approx(values)
                    assert pd.to_datetime(csv["time"], utc=True).tolist() == [pd.Timestamp(stamp).tz_convert("UTC") if pd.Timestamp(stamp).tzinfo else pd.Timestamp(stamp).tz_localize("UTC") for stamp, _ in expected]
                    evidence = json.loads(detail["trusted_result_artifact"]["summary"].removeprefix(RESULT_EVIDENCE_PREFIX))
                    group = evidence["analyses"][0]["groups"][0]
                    assert group["complete"]
                    assert group["up_count"] == sum(b>a for a,b in zip(values,values[1:]))
                    assert group["down_count"] == sum(b<a for a,b in zip(values,values[1:]))
                elif operation == "chart":
                    assert detail["charts"]
                    chat.locator(".js-plotly-plot").last.wait_for(state="visible")
                else:
                    inspections = run["catalog_trace"]["semantic_intent"]["result_inspections"]
                    assert operation in [r["operation"] for r in inspections], question
                    if operation == "share":
                        assert "sum" in [r["operation"] for r in inspections]
                    assert not run["catalog_trace"].get("result_explanation", {}).get("model_used", False)
                if operation != "chart":
                    analyses = evidence_calls[-1]["analyses"]
                    calculation = next(r for r in analyses if r["operation"] == operation)
                    if operation == "trend":
                        group = calculation["groups"][0]
                        assert group["down_count"] == sum(b<a for a,b in zip(values, values[1:]))
                        assert group["up_count"] == sum(b>a for a,b in zip(values, values[1:]))
                        assert ("不成立" if any(b<=a for a,b in zip(values, values[1:])) else "成立") in answer
                    elif operation == "period_compare":
                        pair = calculation["comparisons"][0]
                        assert pair["difference"] == pytest.approx(values[-1]-values[-2])
                        assert pair["relative_change_percent"] == pytest.approx((values[-1]/values[-2]-1)*100)
                    elif operation == "rank":
                        wanted = sorted(zip(expected, values), key=lambda r:r[1], reverse=True)[:3]
                        assert [r[metric] for r in calculation["rows"]] == pytest.approx([v for _,v in wanted])
                        assert [pd.Timestamp(r["time"]).month for r in calculation["rows"]] == [pd.Timestamp(pair[0]).month for pair,_ in wanted]
                    elif operation == "distribution":
                        series = pd.Series(values)
                        for key, q in (("median",.5),("p25",.25),("p75",.75)):
                            assert calculation[key] == pytest.approx(series.quantile(q))
                    elif operation == "share":
                        assert calculation["numerator"] == pytest.approx(values[-1])
                        assert calculation["denominator"] == pytest.approx(sum(values))
                        assert calculation["share_percent"] == pytest.approx(values[-1]/sum(values)*100)
                        assert next(r for r in analyses if r["operation"] == "sum")["value"] == pytest.approx(sum(values))
                    records[-1]["independent_calculation_checked"] = True
                    (root / "observations.json").write_text(json.dumps(records, ensure_ascii=False, indent=2))
                print(f"{workspace.workspace_id} turn {index+1}: {operation} completed, SQL={run['sql_calls_used']}", flush=True)
            page.screenshot(path=str(root / "result-analysis.png"), full_page=True)
            assert not errors
        finally:
            browser.close()
