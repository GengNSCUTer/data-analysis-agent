"""Real browser/SSE/PostgreSQL/CSV; controlled model isolates reuse decisions."""
from __future__ import annotations

import csv
import io
import json
import os
from uuid import uuid4

import psycopg2
import psycopg2.extras
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.mark.skipif(os.getenv("RUN_VANNA_E2E") != "1" or os.getenv("RUN_PROJECT_DB") != "1", reason="enable PostgreSQL/browser")
@pytest.mark.parametrize("dataset", ["olist", "thelook"])
def test_history_coverage_isolation_local_compute_and_safe_sql_fallback(dataset, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from playwright.sync_api import sync_playwright
    from data_analysis_agent import server
    from data_analysis_agent.budget import CURRENT_BUDGET
    from data_analysis_agent.result_evidence import RESULT_EVIDENCE_PREFIX
    from data_analysis_agent.runtime_paths import RESOURCES
    from vanna.core.llm import LlmResponse, LlmStreamChunk
    from vanna.core.tool import ToolCall
    from result_answer_provider import model_answer

    monkeypatch.setenv("DATA_ANALYSIS_WORKSPACE_CONFIG", str(RESOURCES / f"workspaces/{dataset}.yaml"))
    monkeypatch.setenv("DATA_ANALYSIS_POSTGRES_DB", "data_analysis_agent" if dataset=="olist" else "thelook_analytics")
    monkeypatch.delenv("DATA_ANALYSIS_CATALOG_PATH", raising=False)
    monkeypatch.delenv("DATA_ANALYSIS_DATASET_VERSION", raising=False)
    metric, count, year, other_year = ("gmv","paid_order_count",2017,2018) if dataset=="olist" else (
        "completed_sale_amount","completed_order_count",2023,2022)
    bounds = lambda y: {"start":f"{y}-01-01", "end":f"{y+1}-01-01"}
    group_scope = {"metric_ids":[metric], "dimensions":["customer_state"], "time_range":bounds(year), "top_n":10}
    monthly = {"metric_ids":[metric], "dimensions":[], "time_range":bounds(year), "time_grain":"month"}
    q1 = {"metric_ids":[metric], "dimensions":[], "time_range":{"start":f"{year}-01-01","end":f"{year}-04-01"}}
    cases = {
        "先看全年各州销售额前十": {"request_kind":"data_query", "action":"new_task", **group_scope},
        "另看下一期间各州订单量": {"request_kind":"data_query", "action":"new_task", "metric_ids":[count],
            "dimensions":["customer_state"],"time_range":bounds(other_year)},
        "销售额那张表第一名比第二名高多少": {"request_kind":"result_followup","action":"keep",
            "history_reference":{"ordinal":1}, "metric_ids":[], "dimensions":[],
            "filters":[], "time_range":None, "time_grain":None,
            "result_inspections":[{"operation":"compare","metric_id":metric,"row_numbers":[1,2]}]},
        "回到当前订单分析，改成按年统计": {"request_kind":"data_query","action":"update_task",
            "state_patch":{"operations":[{"op":"clear","field":"dimensions"},{"op":"set","field":"time_grain","value":"year"}]}},
        "我想要所有州的全年销售额合计，不是前十": {"request_kind":"data_query","action":"new_task",
            "metric_ids":[metric],"dimensions":[],"time_range":bounds(year),
            "requested_answer":{"metric_ids":[metric],"time_range":bounds(year),"requires_full_population":True}},
        "再看全年每个月销售额": {"request_kind":"data_query","action":"new_task", **monthly},
        "第一季度这些月合计是多少": {"request_kind":"data_query","action":"new_task", **q1,
            "requested_answer":q1,"result_inspections":[{"operation":"sum","metric_id":metric}]},
        "把第一季度各月画成柱状图": {"request_kind":"result_followup","action":"keep","chart_request":"bar",
            "requested_answer":{**q1,"time_grain":"month"}},
        "将销售额旧排行榜改成另一个年份重新查": {"request_kind":"data_query","action":"update_task",
            "history_reference":{"ordinal":1,"mutation_intent":"derive_query"},
            "state_patch":{"operations":[{"op":"set","field":"time_range","value":bounds(other_year)}]}},
        "重新查询最新的全年每月销售额": {"request_kind":"data_query","action":"new_task", **monthly,
            "requested_answer":{**monthly,"refresh":True}},
        "清空后回看之前销售额排行": {"request_kind":"result_followup","action":"keep","history_reference":{"ordinal":1}},
    }
    sql_plans = []

    def statement(plan):
        m = plan["metric_ids"][0]
        if dataset == "olist":
            source = "analytics.fact_orders o JOIN analytics.dim_customers c ON c.customer_id=o.customer_id"
            timestamp, location, status = "o.order_purchase_timestamp", "c.customer_state", "o.order_status NOT IN ('canceled','unavailable')"
            if m == metric:
                source += " JOIN analytics.fact_order_items i ON i.order_id=o.order_id"
                expression = "SUM(i.price)"
            else:
                expression = "COUNT(*)"
        else:
            source = "analytics.orders o JOIN analytics.users c ON c.id=o.user_id"
            timestamp, location, status = "o.created_at", "c.state", "o.status='Complete'"
            if m == metric:
                source += " JOIN analytics.order_items i ON i.order_id=o.order_id"
                expression = "SUM(i.sale_price)"
            else:
                expression = "COUNT(*)"
        fields, groups = [], []
        if plan["dimensions"]:
            fields.append(f"{location} AS customer_state")
            groups.append(location)
        if plan.get("time_grain"):
            time = f"DATE_TRUNC('{plan['time_grain']}',{timestamp})"
            fields.append(time+" AS time")
            groups.append(time)
        fields.append(f"{expression} AS {m}")
        where = [status]
        if plan.get("time_range"):
            interval = plan["time_range"]
            where.extend([f"{timestamp}>=DATE '{interval['start']}'", f"{timestamp}<DATE '{interval['end']}'"])
        sql = "SELECT "+",".join(fields)+" FROM "+source+" WHERE "+" AND ".join(where)
        if groups:
            sql += " GROUP BY "+",".join(groups)
            sql += f" ORDER BY {m} DESC" if plan["dimensions"] else " ORDER BY time"
        if plan.get("top_n"):
            sql += " LIMIT "+str(next(iter(plan["top_n"].values())))
        return sql

    class Provider:
        model = "controlled-history-reuse"
        async def send_request(self, request):
            if request.metadata.get("purpose") == "semantic_intent":
                question = request.messages[0].content.rsplit("用户问题: ",1)[1]
                return LlmResponse(content=json.dumps({**cases[question],"confidence":0.99},ensure_ascii=False))
            if request.metadata.get("purpose") == "result_answer_review":
                return LlmResponse(content='{"verdict":"supported","issues":[]}')
            evidence, _ = json.JSONDecoder().raw_decode(request.messages[-1].content.split(RESULT_EVIDENCE_PREFIX,1)[1])
            return LlmResponse(content=model_answer(evidence))

        async def stream_request(self, request):
            if request.tools:
                plan = CURRENT_BUDGET.get().query_plan
                sql_plans.append(plan)
                yield LlmStreamChunk(tool_calls=[ToolCall(id=uuid4().hex,name="run_sql",arguments={"sql":statement(plan)})],finish_reason="tool_calls")
            else:
                response = await self.send_request(request)
                yield LlmStreamChunk(content=response.content,finish_reason="stop")

        async def validate_tools(self, tools):
            return []
        async def aclose(self):
            pass

    username = "reuse_"+uuid4().hex[:12]
    monkeypatch.setenv("DATA_ANALYSIS_STATE_DIR",str(tmp_path))
    monkeypatch.setenv("DATA_ANALYSIS_BOOTSTRAP_USERNAME",username)
    monkeypatch.setenv("DATA_ANALYSIS_BOOTSTRAP_PASSWORD","history-browser-password")
    monkeypatch.setattr(server,"RESULT_ARTIFACT_DIRECTORY",tmp_path/"artifacts")
    observations = []
    with TestClient(server.create_app(llm_provider=Provider())) as client, sync_playwright() as engine:
        assert client.post("/api/auth/login",json={"username":username,"password":"history-browser-password"}).is_success
        browser = engine.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width":1440,"height":960})
            errors = []
            page.on("pageerror",lambda error:errors.append(str(error)))
            def forward(route):
                request = route.request
                response = client.request(request.method,request.url.removeprefix("http://testserver"),
                    content=request.post_data_buffer,headers={k:v for k,v in request.headers.items() if k not in {"host","content-length"}})
                route.fulfill(status=response.status_code,body=response.content,
                    headers={k:v for k,v in response.headers.items() if k not in {"content-length","content-encoding","transfer-encoding"}})
            page.route("http://testserver/**",forward)
            page.goto("http://testserver/embedded-copilot",wait_until="networkidle")
            page.wait_for_function("() => !!document.querySelector('vanna-chat').shadowRoot")
            chat = page.locator("vanna-chat")

            def send(question, expected_sql, expected_mode=None, terminal=None):
                if chat.locator(".minimized-icon").is_visible():
                    chat.locator(".minimized-icon").click()
                chat.locator("textarea.message-input").fill(question)
                with page.expect_response(lambda response:response.url.endswith("/chat_sse")) as result:
                    chat.locator("button.send-button").click()
                assert result.value.status==200
                page.wait_for_function("() => !document.querySelector('vanna-chat').shadowRoot.querySelector('textarea.message-input').disabled")
                cid = chat.evaluate("el=>el.conversationId")
                detail = client.get(f"/api/project/conversations/{cid}").json()
                role = "daa_app_writer" if dataset=="olist" else "daa_thelook_app_writer"
                with psycopg2.connect(host="/tmp",port=35434,database=os.environ["DATA_ANALYSIS_POSTGRES_DB"],user=role) as conn:
                    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                        cur.execute("SELECT sql_calls_used,termination_reason,catalog_trace FROM app.agent_runs WHERE conversation_id=%s ORDER BY started_at DESC LIMIT 1",(cid,))
                        run = dict(cur.fetchone())
                observations.append({"question":question,**run})
                assert run["sql_calls_used"]==expected_sql, run
                assert run["termination_reason"] == (terminal or ("clarification_required" if expected_mode=="CLARIFY" else "completed")), run
                if expected_mode:
                    assert run["catalog_trace"]["artifact_reuse"]["mode"]==expected_mode,run
                return cid, detail, run, result.value.text()

            questions = list(cases)
            cid, _, _, _ = send(questions[0],1,"SQL_REQUIRED")
            _, original_orders, _, _ = send(questions[1],1,"SQL_REQUIRED")
            _, repeated_orders, _, _ = send(questions[1],0,"DIRECT_REUSE")
            assert repeated_orders["analysis_state"]["metric_ids"] == [count]
            assert repeated_orders["analysis_state"]["time_range"] == {**bounds(other_year),"end_semantics":"exclusive"}
            assert repeated_orders["analysis_state"]["task_id"] != original_orders["analysis_state"]["task_id"]
            assert repeated_orders["trusted_result_artifact"]["artifact_id"] == original_orders["trusted_result_artifact"]["artifact_id"]
            before = client.get(f"/api/project/conversations/{cid}").json()["analysis_state"]
            _, detail, run, _ = send(questions[2],0,"LOCAL_COMPUTE")
            assert detail["analysis_state"]==before
            assert run["catalog_trace"]["artifact_reuse"]["result_ordinal"]==1
            chat.get_by_text("来源：历史结果#1",exact=False).first.wait_for()
            send(questions[3],1,"SQL_REQUIRED")
            assert sql_plans[-1]["metric_ids"]==[count] and sql_plans[-1]["time_range"]=={**bounds(other_year),"end_semantics":"exclusive"}
            send(questions[4],1,"SQL_REQUIRED")
            send(questions[5],1,"SQL_REQUIRED")
            before = client.get(f"/api/project/conversations/{cid}").json()["analysis_state"]
            _, detail, run, _ = send(questions[6],0,"LOCAL_COMPUTE")
            inspection = run["catalog_trace"]["result_inspection"]
            assert (inspection["source_row_count"], inspection["selected_row_count"], inspection["included_row_count"]) == (12, 1, 1)
            assert detail["analysis_state"]["time_range"]=={**q1["time_range"],"end_semantics":"exclusive"}
            assert detail["trusted_result_artifact"]["query_scope"]["time_grain"] is None
            assert detail["trusted_result_artifact"]["artifact_id"]!=before["previous_result_artifact"]["artifact_id"]
            analysis = json.loads(run["catalog_trace"]["result_summary"].split(RESULT_EVIDENCE_PREFIX,1)[1])["analyses"][0]
            reference = {"metric_ids":[metric],"dimensions":[],"time_range":q1["time_range"],"time_grain":None,"top_n":None}
            reader = "daa_analytics_reader" if dataset=="olist" else "daa_thelook_reader"
            with psycopg2.connect(host="/tmp",port=35434,database=os.environ["DATA_ANALYSIS_POSTGRES_DB"],user=reader) as conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL TIME ZONE 'UTC'")
                    cur.execute(statement(reference))
                    expected = float(cur.fetchone()[0])
            assert analysis["value"] == pytest.approx(expected)
            download = client.get(f"/api/project/conversations/{cid}/result.csv")
            rows = list(csv.DictReader(io.StringIO(download.content.decode())))
            assert len(rows)==1 and float(rows[0][metric]) == pytest.approx(expected)
            items = client.get(f"/api/project/conversations/{cid}/artifacts").json()["artifacts"]
            output_id = detail["trusted_result_artifact"]["artifact_id"]
            source_id = run["catalog_trace"]["artifact_reuse"]["source_artifact_id"]
            by_id = {item["artifact_id"]: item for item in items}
            assert by_id[output_id]["expires_at"]==by_id[source_id]["expires_at"]
            assert by_id[output_id]["snapshot_created_at"]==by_id[source_id]["snapshot_created_at"]
            page.reload(wait_until="networkidle")
            send(questions[7],0,"LOCAL_COMPUTE")
            assert chat.locator(".js-plotly-plot").count()>=1
            before_sql = len(sql_plans)
            page.get_by_text("更多",exact=True).click()
            page.locator("#history-results").click()
            page.locator("#result-browser").wait_for(state="visible")
            assert page.locator("#history-result-select option").count()==6
            page.locator("#history-result-select").select_option(index=5)
            page.locator("#result-page-table tbody tr").first.wait_for()
            assert len(sql_plans)==before_sql
            page.locator("#result-page-close").click()
            # Explicit derivation goes through SQL even if another snapshot could be reused.
            send(questions[8],1,"DERIVED_QUERY")
            assert sql_plans[-1]["metric_ids"]==[metric] and sql_plans[-1]["dimensions"]==["customer_state"]
            assert next(iter(sql_plans[-1]["top_n"].values()))==10
            send(questions[9],1,"SQL_REQUIRED")
            listing = client.get(f"/api/project/conversations/{cid}/artifacts").json()["artifacts"]
            download = client.get(f"/api/project/conversations/{cid}/result.csv",params={"artifact_id":listing[-1]["artifact_id"]})
            assert download.is_success and len(list(csv.DictReader(io.StringIO(download.content.decode()))))==10
            assert client.post(f"/api/project/conversations/{cid}/analysis-context").is_success
            send(questions[10],0,"CLARIFY")
            before_count = len(client.get(f"/api/project/conversations/{cid}/artifacts").json()["artifacts"])
            from data_analysis_agent.artifact_history import ArtifactHistory
            with monkeypatch.context() as patch:
                def fail_commit(*args):
                    raise RuntimeError("injected metadata commit failure")
                patch.setattr(ArtifactHistory,"commit",fail_commit)
                _, failed, _, body = send(questions[0],1,"SQL_REQUIRED",terminal="execution_error")
            events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: {")]
            assert not any(e.get("rich",{}).get("type")=="dataframe" for e in events)
            assert "未能与来源一同保存" in failed["messages"][-1]["content"]
            assert len(client.get(f"/api/project/conversations/{cid}/artifacts").json()["artifacts"])==before_count
            page.screenshot(path=str(tmp_path/"history-reuse.png"),full_page=True)
            (tmp_path/"observations.json").write_text(json.dumps(observations,ensure_ascii=False,default=str))
            assert not errors
        finally:
            browser.close()
