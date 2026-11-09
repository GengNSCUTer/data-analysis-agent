"""Run authored tasks in the real browser; preserve every attempt outside Git.

Numeric expectations never enter model input. Automation and semantic review
are separate: accepted prose is not automatically a relevant/correct answer.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import time

import pandas as pd
import psycopg2
import psycopg2.extras
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "scripts/evals"), str(ROOT / "scripts"), str(ROOT / "src")]
from check_agent_business_values import DIMENSIONS, compare_frames, expected_calculation, numeric_equal, read_csv, reference_frame, source_case
from data_analysis_agent.result_evidence import RESULT_EVIDENCE_PREFIX

CATEGORIES = ("basic", "formula", "comparison", "continuity", "history", "explanation")
SETTINGS = {
    "olist": {"port": 32013, "database": "data_analysis_agent", "reader": "daa_analytics_reader", "writer": "daa_app_writer",
        "password": "/disk2/gengnan/data-analysis-agent-data/local-auth/bootstrap-password.txt"},
    "thelook": {"port": 32104, "database": "thelook_analytics", "reader": "daa_thelook_reader", "writer": "daa_thelook_app_writer",
        "password": "/disk2/gengnan/data-analysis-agent-data/evals/product-readiness-stage4-20261020/state/bootstrap-password.txt"},
}


def load_tasks(path: Path) -> list[dict]:
    suite = yaml.safe_load(path.read_text())
    tasks = suite["tasks"]
    if suite["usage"] != "online_development_only" or len({task["id"] for task in tasks}) != len(tasks):
        raise ValueError("Task usage/IDs invalid")
    quota = suite['tasks_per_category']
    if type(quota) is not int or quota < 1 or suite['tasks_per_workspace'] != quota * len(CATEGORIES):
        raise ValueError('Task quota must be positive and consistent per workspace')
    expected = Counter({(dataset, category): quota for dataset in SETTINGS for category in CATEGORIES})
    if Counter((task["dataset"], task["category"]) for task in tasks) != expected:
        raise ValueError(f'Need {quota} distinct tasks per category and dataset')
    for task in tasks:
        if not task["goal"] or not task["steps"]:
            raise ValueError(task["id"])
        if task["category"] == "continuity" and not 4 <= len(task["steps"]) <= 6:
            raise ValueError("Continuity tasks need 4–6 sequential turns")
        for step in task["steps"]:
            if "source" in step:
                source = step.pop("source")
                inherited = source_case(source)
                # Inherit the independently authored query and behavioral expectation.
                step.update({**inherited, "reference": {"source": source}, **step}
                    if inherited.get("reference_sql") else {**inherited, **step})
            if not step.get("question") or not any(step.get(k) for k in ("reference", "reuse", "no_sql")):
                raise ValueError(f"Missing independent expectation: {task['id']}")
    return tasks


def connect(dataset, *, metadata=False):
    config = SETTINGS[dataset]
    connection = psycopg2.connect(host="/tmp", port=35434, dbname=config["database"],
        user=config["writer" if metadata else "reader"], connect_timeout=5)
    connection.set_session(readonly=True)
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL TIME ZONE 'UTC'")
        cursor.execute("SET LOCAL statement_timeout='30s'")
    return connection


def run_evidence(dataset, cid):
    connection = connect(dataset, metadata=True)
    try:
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute("SELECT * FROM app.agent_runs WHERE conversation_id=%s ORDER BY started_at DESC LIMIT 1", (cid,))
            result = dict(cursor.fetchone())
            cursor.execute("SELECT policy_status,original_sql,final_sql,policy_reason,error_message,elapsed_ms FROM app.query_audits WHERE request_id=%s ORDER BY created_at", (result["request_id"],))
            result["sql_audits"] = [dict(row) for row in cursor.fetchall()]
        return result
    finally:
        connection.close()


def parse_sse(body):
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: {")]


def wire_answer(events):
    text, failures = {}, []
    for event in events:
        rich = event.get("rich") or {}
        data = rich.get("data") or {}
        if rich.get("type") == "text" and data.get("content"):
            # SSE updates replace a component's complete content, just as the
            # web component renders them; they are not incremental text deltas.
            text[rich.get("id") or len(text)] = data["content"]
        if rich.get("type") == "status_card" and data.get("title") == "本次分析未完成":
            failures.append(data.get("description", "本次分析未完成"))
    return "\n\n".join(text.values() if text else failures), failures


def evidence_payload(trace):
    summary = trace.get("result_summary")
    return json.JSONDecoder().raw_decode(summary.split(RESULT_EVIDENCE_PREFIX, 1)[1])[0] if summary else {}


def check_analyses(expectation, payload, expected):
    operation = expectation["operation"]
    analyses = [item for item in payload.get("analyses", []) if item["operation"] == operation and item["status"] == "ready"]
    if not analyses:
        return ["analysis_missing:" + operation]
    if operation=='period_compare':
        current,baseline=expectation['current_window'],expectation['baseline_window']
        comparisons=[comparison for result in analyses if result.get('metric_id')==expectation['metric']
                     for comparison in result.get('comparisons',[])
                     if comparison.get('current_window')==current and comparison.get('baseline_window')==baseline]
        if len(comparisons)!=1:
            return ['analysis_windows']
        values={pd.Timestamp(row['time']).date().isoformat():Decimal(str(row[expectation['metric']]))
                for row in expected.to_dict('records')}
        a,b=values[current[0]],values[baseline[0]]
        wanted={'current':a,'baseline':b,'difference':a-b,
                'relative_change_percent':(a-b)/b*100 if b>0 else None}
        return ['analysis_numeric:'+name for name,value in wanted.items()
                if (comparisons[0].get(name) is not None if value is None else
                    not numeric_equal(comparisons[0].get(name),value,
                        'growth_percent' if name=='relative_change_percent' else expectation['metric']))]
    if operation == "trend":
        values = list(expected["gmv"])
        increasing = all(b > a for a, b in zip(values, values[1:]))
        groups = analyses[0].get("groups", [])
        return [] if len(groups) == 1 and groups[0].get("judgment") == str(increasing).lower() else ["trend_judgment"]
    target = expected_calculation(expected, expectation["metric"], operation, expectation.get("rows"))
    actual = analyses[0].get("value" if operation == "sum" else "difference")
    return [] if numeric_equal(actual, target, expectation["metric"]) else ["analysis_numeric"]


def check_acquisition_frame(observed,expected,acquisition_expected=None,*,ordered=False):
    """Authored alternative raw shape; final calculated operands remain mandatory."""
    errors=compare_frames(observed,expected,ordered=ordered)
    if not errors or acquisition_expected is None:
        return errors
    return compare_frames(observed,acquisition_expected,ordered=ordered)


def task_status(turns, semantic="pending"):
    if any(turn["errors"] for turn in turns):
        return "failed"
    if semantic == "fail":
        return "failed"
    return "passed" if semantic == "pass" else "pending_review"


def check_task_scope(reference, state):
    if not reference or "source" in reference:
        return []
    expected = {"metric_ids":[reference["metric"]], "dimensions":[reference["group"]] if reference.get("group") else [],
        "time_grain":reference.get("grain"), "top_n":reference.get("top_n",reference.get("partition_top_n"))}
    # Half-year is an analysis window, not an existing SQL bucket identifier.
    # Check the independently requested windows numerically and in prose.
    if reference.get("grain") == "half_year":
        expected.pop("time_grain")
    errors = ["scope:"+key for key,value in expected.items() if (state or {}).get(key) != value]
    interval = (state or {}).get("time_range") or {}
    if interval.get("start") != reference.get("start") or interval.get("end") != reference.get("end"):
        errors.append("scope:time_range")
    filters = (state or {}).get("filters") or []
    if reference.get("state") and reference["state"] != "BEST":
        if filters != ["customer_state=" + reference["state"]]:
            errors.append("scope:filters")
    elif reference.get("state") != "BEST" and filters:
        errors.append("scope:filters")
    return errors


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def review_saved(output):
    """Recompute numeric checks without another model call; retain original files."""
    tasks = {task['id']: task for task in json.loads((output / 'tasks.json').read_text())}
    review_path = output / "semantic-reviews.json"
    reviews = json.loads(review_path.read_text()) if review_path.exists() else []
    by_id = {(item["id"],item["attempt"]):item for item in reviews}
    results = []
    for task_path in sorted(output.glob("*/task.json")):
        record = json.loads(task_path.read_text())
        task = tasks[record["id"]]
        previous_expected = None
        for step, turn in zip(task["steps"],record["turns"]):
            prefix = task_path.parent / f"turn-{turn['turn']}"
            expected_path, csv_path = Path(str(prefix)+".expected.csv"),Path(str(prefix)+".csv")
            if Path(str(prefix)+".output.csv").exists():
                csv_path = Path(str(prefix)+".output.csv")
            output_audit = Path(str(prefix)+".output-audit.json")
            if output_audit.exists() and json.loads(output_audit.read_text())["status"] != 200:
                if "derived_artifact_unavailable" not in turn["errors"]:
                    turn["errors"].append("derived_artifact_unavailable")
            expected = read_csv(expected_path.read_text()) if expected_path.exists() else previous_expected
            source_evidence = Path(str(prefix)+".result.json")
            if source_evidence.exists():
                authored = json.loads(source_evidence.read_text())
                if step["question"] != authored["question"]:
                    turn["errors"].append("evaluation_question_changed_requires_rerun")
            sse_path = Path(str(prefix) + ".sse.txt")
            if sse_path.exists():
                turn["persisted_answer"] = turn["answer"]
                turn["answer"], failures = wire_answer(parse_sse(sse_path.read_text()))
                if failures and "sse_runtime_failure" not in turn["errors"]:
                    turn["errors"].append("sse_runtime_failure")
            if not step.get("preserve_state"):
                turn["errors"] = [error for error in turn["errors"] if not error.startswith("scope:")]
                turn["errors"].extend(check_task_scope(step.get("reference"),turn.get("analysis_state")))
            if csv_path.exists() and expected is not None:
                acquisition_path=Path(str(prefix)+'.expected-acquisition.csv')
                acquisition_expected=read_csv(acquisition_path.read_text()) if acquisition_path.exists() else None
                errors = check_acquisition_frame(read_csv(csv_path.read_text()),expected,acquisition_expected,ordered=step.get("ordered",False))
                # Replace only evaluator-owned numeric errors. SQL, narrative,
                # state, browser and latency failures remain untouched.
                turn["errors"] = [error for error in turn["errors"] if not error.startswith(("csv:","DateParseError:"))]
                turn["errors"].extend("csv:"+error for error in errors)
                turn["numeric_errors"] = errors
            if step.get('analysis') and expected is not None and Path(str(prefix)+'.run.json').exists():
                trace=json.loads(Path(str(prefix)+'.run.json').read_text()).get('catalog_trace') or {}
                turn['errors']=[error for error in turn['errors'] if not error.startswith('analysis_')]
                turn['errors'].extend(check_analyses(step['analysis'],evidence_payload(trace),expected))
            chart_path = Path(str(prefix) + ".chart-rechecked.json")
            if chart_path.exists() and expected is not None:
                chart_errors = check_chart(json.loads(chart_path.read_text()),expected)
                turn["errors"] = [error for error in turn["errors"] if error not in {
                    "chart_series_shape", "chart_values", "chart_not_rendered",
                }]
                turn["errors"].extend(chart_errors)
            previous_expected = expected
        semantic = by_id.get((record["id"],record["attempt"]),{})
        record["semantic_review"] = semantic.get("verdict","pending")
        record["semantic_note"] = semantic.get("note")
        record["status"] = task_status(record["turns"],record["semantic_review"])
        results.append(record)
    summary = {"task_attempts":len(results),"turns":sum(len(record["turns"]) for record in results),
        "status_counts":dict(Counter(record["status"] for record in results)),
        "first_failures":{record["id"]:next((turn["errors"] for turn in record["turns"] if turn["errors"]),[])
            for record in results},"records":results,
        "note":"Saved evidence reassessment; original attempts unchanged, pending semantic review is not pass."}
    write_json(output / "reviewed-report.json",summary)
    print(json.dumps({key:value for key,value in summary.items() if key not in {"records","first_failures"}},ensure_ascii=False))
    return summary


# Test-only observer measures first received useful SSE component. It neither
# edits the request nor supplies intent, SQL, answers or reference data.
STREAM_OBSERVER = """(() => {
 const original = window.fetch;
 window.fetch = async function(...args) {
  const isChat = String(args[0] instanceof Request ? args[0].url : args[0]).includes('/chat_sse');
  if (!isChat) return original.apply(this,args);
  const timing = window.__businessTiming = {start:performance.now(),first:null};
  const response = await original.apply(this,args);
  (async () => {
   const reader=response.clone().body.getReader(); const decoder=new TextDecoder(); let rest='';
   while(true) {const {value,done}=await reader.read(); if(done) break;
    rest+=decoder.decode(value,{stream:true}); const lines=rest.split('\\n'); rest=lines.pop();
    for(const line of lines) { if(!line.startsWith('data: {')) continue;
     const event=JSON.parse(line.slice(6)); const rich=event.rich;
     if(timing.first===null && rich && ['text','dataframe','chart'].includes(rich.type))
       timing.first=performance.now()-timing.start;
    }
   }
  })().catch(error=>timing.error=String(error));
  return response;
 };
})()"""


def check_chart(points, expected):
    """Every plotted numeric measure must come from the independently checked CSV."""
    if not points:
        return ["chart_not_rendered"]
    errors = []
    for trace in points:
        pairs = (trace.get("labels"), trace.get("values")) if trace.get("type") == "pie" else (
            (trace.get("y"), trace.get("x")) if trace.get("orientation") == "h" else (trace.get("x"), trace.get("y")))
        labels, values = pairs
        metric = trace.get("name")
        numeric = [name for name in expected if name not in DIMENSIONS]
        if metric not in expected:
            metric = numeric[0] if len(numeric) == 1 else None
        if metric is None or values is None or len(values) != len(expected):
            errors.append("chart_series_shape")
            continue
        # Sort through labels when a category/time dimension is available.
        keys = [name for name in expected if name not in numeric]
        if len(keys) == 1 and labels is not None:
            key = keys[0]
            actual = [{key: label, metric: value} for label, value in zip(labels, values)]
            errors.extend(compare_frames(pd.DataFrame(actual), expected[[key, metric]]))
        elif not all(numeric_equal(a,b,metric) for a,b in zip(values, expected[metric])):
            errors.append("chart_values")
    return errors


PLOT_POINTS = "el=>el._fullData.map(t=>({type:t.type,name:t.name,orientation:t.orientation,x:t.x?Array.from(t.x):null,y:t.y?Array.from(t.y):null,labels:t.labels?Array.from(t.labels):null,values:t.values?Array.from(t.values):null}))"


def audit_saved_charts(output):
    """Replay stored, authorized figures; inspect Plotly's decoded numeric arrays."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        try:
            for dataset, config in SETTINGS.items():
                paths = [path for path in output.glob("*/turn-*.run.json") if path.parent.name.startswith("o-" if dataset=="olist" else "t-")]
                if not paths:
                    continue
                page = browser.new_page()
                base = f"http://127.0.0.1:{config['port']}"
                response = page.request.post(base + "/api/auth/login",data={"username":"analyst", "password":Path(config["password"]).read_text().strip()})
                if not response.ok:
                    raise RuntimeError(f"Chart audit login HTTP {response.status}")
                page.goto(base + "/embedded-copilot",wait_until="networkidle")
                for path in paths:
                    detail = json.loads(Path(str(path).replace(".run.json",".conversation.json")).read_text())
                    run = json.loads(path.read_text())
                    output_id = ((run["catalog_trace"] or {}).get("artifact_reuse") or {}).get("output_artifact_id")
                    if output_id:
                        csv = page.request.get(base + f"/api/project/conversations/{detail['conversation_id']}/result.csv",params={"artifact_id":output_id})
                        write_json(Path(str(path).replace(".run.json",".output-audit.json")),{"status":csv.status,"artifact_id":output_id})
                        if csv.ok:
                            Path(str(path).replace(".run.json",".output.csv")).write_text(csv.text())
                    if not Path(str(path).replace(".run.json",".chart.json")).exists():
                        continue
                    page.locator("vanna-chat").evaluate("(el,d)=>el.loadConversation(d.conversation_id,d.messages,d.charts)",detail)
                    plot = page.locator("vanna-chat").locator(".js-plotly-plot").last
                    plot.wait_for(state="attached",timeout=30_000)
                    points = plot.evaluate(PLOT_POINTS)
                    write_json(Path(str(path).replace(".run.json",".chart-rechecked.json")),points)
                page.close()
        finally:
            browser.close()


def run_partition(dataset, tasks, output, repetitions, username):
    from playwright.sync_api import sync_playwright
    config = SETTINGS[dataset]
    base = f"http://127.0.0.1:{config['port']}"
    completed = []
    with sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width":1440,"height":1000})
            page.set_default_timeout(240_000)
            page.add_init_script(STREAM_OBSERVER)
            login = page.request.post(base + "/api/auth/login", data={"username":username,
                "password":Path(config["password"]).read_text().strip()})
            if not login.ok:
                raise RuntimeError(f"{dataset} login HTTP {login.status}")
            page.goto(base + "/embedded-copilot", wait_until="networkidle")
            runtime = page.request.get(base + "/api/project/runtime-contract").json()
            write_json(output / f"{dataset}-runtime.json", runtime)
            for repetition in range(1, repetitions + 1):
                for task in tasks:
                    folder = output / f"{task['id']}-attempt-{repetition}"
                    folder.mkdir(mode=0o700)
                    page.locator("#new-analysis").click()
                    chat = page.locator("vanna-chat")
                    previous_state, previous_frame, turns = None, None, []
                    for number, step in enumerate(task["steps"], 1):
                        item = {"turn":number, "question":step["question"], "errors":[],
                            "prior_turn_failed":any(turn["errors"] for turn in turns)}
                        turns.append(item)
                        prefix = folder / f"turn-{number}"
                        expected = acquisition_expected = None
                        try:
                            if step.get("reference"):
                                connection = connect(dataset)
                                try:
                                    expected = reference_frame(connection, dataset, step["reference"])
                                    if step.get('acquisition_reference'):
                                        acquisition_expected=reference_frame(connection,dataset,step['acquisition_reference'])
                                finally:
                                    connection.close()
                                expected.to_csv(str(prefix) + ".expected.csv", index=False)
                                if acquisition_expected is not None:
                                    acquisition_expected.to_csv(str(prefix)+'.expected-acquisition.csv',index=False)
                            elif previous_frame is not None:
                                expected = previous_frame
                            if step.get("reload") or step.get("refresh"):
                                page.reload(wait_until="networkidle")
                            if chat.locator(".minimized-icon").is_visible():
                                chat.locator(".minimized-icon").click()
                            page.wait_for_function("() => !document.querySelector('vanna-chat').shadowRoot.querySelector('textarea.message-input').disabled")
                            chat.locator("textarea.message-input").fill(step["question"])
                            started = time.monotonic()
                            with page.expect_response(lambda response: response.url.endswith("/chat_sse")) as pending:
                                chat.locator("button.send-button").click()
                            response = pending.value
                            body = response.text()  # Wait for DONE, not just response headers.
                            item["elapsed_ms"] = round((time.monotonic() - started) * 1000)
                            Path(str(prefix) + ".sse.txt").write_text(body)
                            if not response.ok:
                                raise RuntimeError(f"SSE HTTP {response.status}")
                            page.wait_for_function("() => !document.querySelector('vanna-chat').shadowRoot.querySelector('textarea.message-input').disabled")
                            item["timing"] = page.evaluate("window.__businessTiming")
                            cid = chat.evaluate("element=>element.conversationId")
                            detail_response = page.request.get(base + f"/api/project/conversations/{cid}")
                            if not detail_response.ok:
                                raise RuntimeError(f"Conversation HTTP {detail_response.status}")
                            detail = detail_response.json()
                            write_json(Path(str(prefix) + ".conversation.json"), detail)
                            run = run_evidence(dataset, cid)
                            write_json(Path(str(prefix) + ".run.json"), run)
                            item.update(conversation_id=cid, request_id=run["request_id"],
                                termination=run["termination_reason"], sql_calls=int(run["sql_calls_used"] or 0),
                                model_calls=run["llm_rounds_used"], token_usage=run.get("total_tokens"), cost=None)
                            trace = run["catalog_trace"] or {}
                            item["explanation"] = trace.get("result_explanation") or {}
                            item["reuse_mode"] = (trace.get("artifact_reuse") or {}).get("mode")
                            item["repair"] = run.get("repair_evidence")
                            item["performance"] = trace.get("performance")
                            item["llm_observations"] = trace.get("llm_observations")
                            item["persisted_answer"] = detail["messages"][-1]["content"]
                            item["answer"], failures = wire_answer(parse_sse(body))
                            if failures:
                                item["errors"].append("sse_runtime_failure")
                            item["error_type"] = run.get("error_type")
                            item["analysis_state"] = detail["analysis_state"]
                            if not step.get("preserve_state"):
                                item["errors"].extend(check_task_scope(step.get("reference"),detail["analysis_state"]))
                            if step.get("preserve_state") and previous_state != detail["analysis_state"]:
                                item["errors"].append("current_state_changed")
                            if step.get("reuse") and item["sql_calls"] != 0:
                                item["errors"].append("unexpected_requery")
                            if step.get("require_sql") and item["sql_calls"] == 0:
                                item["errors"].append("missing_required_query")
                            if run["termination_reason"] != "completed":
                                item["errors"].append("termination:" + str(run["termination_reason"]))
                            if item["explanation"].get("answer_status") == "unverified":
                                item["errors"].append("explanation_unverified")
                            payload = evidence_payload(trace)
                            reuse = trace.get("artifact_reuse") or {}
                            artifact = (reuse.get("output_artifact_id") or (trace.get("result_artifact") or {}).get("artifact_id")
                                or (reuse.get("source_artifact_id") if step.get("analysis") else None))
                            item["artifact_id"] = artifact
                            url = base + f"/api/project/conversations/{cid}/result.csv"
                            csv_response = page.request.get(url, params={"artifact_id":artifact} if artifact else {})
                            if not csv_response.ok:
                                item["errors"].append("csv_unavailable")
                            else:
                                Path(str(prefix) + ".csv").write_text(csv_response.text())
                                observed = read_csv(csv_response.text())
                                if expected is not None:
                                    item["numeric_errors"] = check_acquisition_frame(observed, expected, acquisition_expected, ordered=step.get("ordered", False))
                                    item["errors"].extend("csv:" + error for error in item["numeric_errors"])
                            if step.get("analysis") and expected is not None:
                                item["errors"].extend(check_analyses(step["analysis"], payload, expected))
                            if step.get("pagination") and csv_response.ok:
                                if len(observed) <= 200:
                                    item["errors"].append("pagination_scenario_too_small")
                                else:
                                    active = detail["trusted_result_artifact"]["artifact_id"]
                                    response_page = page.request.get(base + f"/api/project/conversations/{cid}/artifacts/{active}/rows?cursor=200&page_size=200")
                                    if not response_page.ok:
                                        item["errors"].append("pagination_unavailable")
                                    else:
                                        rows = response_page.json()
                                        item["errors"].extend("pagination:" + error for error in compare_frames(pd.DataFrame(rows["rows"]),observed.iloc[200:400]))
                                        if rows["total_rows"] != len(observed):
                                            item["errors"].append("pagination_total")
                            if step.get("chart"):
                                contract = trace.get("chart_contract") or {}
                                if contract.get("chart_type") != step["chart"]:
                                    item["errors"].append("chart_type")
                                chat.locator(".js-plotly-plot").last.wait_for(state="visible", timeout=15_000)
                                points = chat.locator(".js-plotly-plot").last.evaluate(PLOT_POINTS)
                                write_json(Path(str(prefix) + ".chart.json"), points)
                                if expected is not None:
                                    item["errors"].extend(check_chart(points, expected))
                            previous_state = detail["analysis_state"]
                            previous_frame = expected
                        except Exception as error:
                            item["errors"].append(type(error).__name__ + ":" + str(error))
                        finally:
                            page.screenshot(path=str(prefix) + ".png", full_page=True)
                            write_json(Path(str(prefix) + ".result.json"), item)
                            print(json.dumps({"id":task["id"],"attempt":repetition,"turn":number,
                                "errors":item["errors"],"elapsed_ms":item.get("elapsed_ms")}, ensure_ascii=False), flush=True)
                    record = {"id":task["id"],"dataset":dataset,"category":task["category"],"attempt":repetition,
                        "goal":task["goal"],"status":task_status(turns),"semantic_review":"pending","turns":turns}
                    write_json(folder / "task.json", record)
                    completed.append(record)
                    write_json(output / f"{dataset}-progress.json", completed)
            end_runtime = page.request.get(base + "/api/project/runtime-contract").json()
            versions = ("dataset_version", "metric_version", "catalog_version", "policy_version", "workspace_id")
            if any(runtime.get(key) != end_runtime.get(key) for key in versions):
                raise RuntimeError("Workspace versions changed during evaluation; do not compare mixed runs")
        finally:
            browser.close()
    return completed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/cases/agent_business_tasks_v1.yaml")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--dataset", choices=SETTINGS)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--username", default="analyst")
    parser.add_argument("--audit-references", action="store_true")
    parser.add_argument("--review-directory", type=Path)
    parser.add_argument("--audit-saved-charts", type=Path)
    args = parser.parse_args()
    if args.review_directory:
        review_saved(args.review_directory)
        return 0
    if args.audit_saved_charts:
        audit_saved_charts(args.audit_saved_charts)
        return 0
    tasks = load_tasks(args.cases)
    if args.case_id:
        unknown = set(args.case_id) - {task["id"] for task in tasks}
        if unknown:
            parser.error(f"Unknown tasks: {sorted(unknown)}")
        tasks = [task for task in tasks if task["id"] in args.case_id]
    tasks = [task for task in tasks if not args.dataset or task["dataset"] == args.dataset]
    if args.audit_references:
        for dataset in SETTINGS:
            connection = connect(dataset)
            try:
                for task in tasks:
                    if task["dataset"] == dataset:
                        for n, step in enumerate(task["steps"], 1):
                            if step.get("reference"):
                                frame = reference_frame(connection, dataset, step["reference"])
                                print(task["id"],n,len(frame),list(frame.columns),flush=True)
                            if step.get('acquisition_reference'):
                                frame=reference_frame(connection,dataset,step['acquisition_reference'])
                                print(task['id'],n,'acquisition',len(frame),list(frame.columns),flush=True)
            finally:
                connection.close()
        return 0
    if args.output is None or args.output.resolve().is_relative_to(ROOT):
        parser.error("--output must be a new external directory")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_json(args.output / "invocation.json", {"started_at":datetime.now(timezone.utc).isoformat(),
        "case_ids":[task["id"] for task in tasks],"repetitions":args.repetitions,
        "semantic_review":"required","cost":"unknown unless provider supplies it"})
    write_json(args.output / "tasks.json",tasks)
    records = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run_partition,dataset,[task for task in tasks if task["dataset"]==dataset],
            args.output,args.repetitions,args.username) for dataset in SETTINGS if any(task["dataset"]==dataset for task in tasks)]
        for future in futures:
            records.extend(future.result())
    summary = {"task_attempts":len(records),"status_counts":dict(Counter(record["status"] for record in records)),
        "records":records,"note":"Automated checks and semantic review are separate; pending is not pass."}
    write_json(args.output / "report.json",summary)
    print(json.dumps({key:value for key,value in summary.items() if key!="records"},ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
