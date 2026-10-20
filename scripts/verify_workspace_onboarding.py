"""Real browser/model questions, with independent SQL checks of saved CSV.

Two browser sessions may run independent groups; turns within a conversation
always run sequentially. No training renderer or automatic model retries.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import io
import json
from pathlib import Path
import time

import pandas as pd
from pandas.testing import assert_frame_equal
import psycopg2
import yaml
from sqlglot import parse_one

ROOT = Path(__file__).resolve().parents[1]


def load_cases(path):
    suite = yaml.safe_load(Path(path).read_text())
    cases = suite["cases"]
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Duplicate question IDs")
    if Counter(case["category"] for case in cases) != Counter(suite["categories"]):
        raise ValueError("Question category counts do not match manifest")
    for case in cases:
        if not case.get("question") or not any(case.get(key) for key in ("reference_sql", "no_sql", "reuse")):
            raise ValueError(f"Missing independent expectation: {case['id']}")
    return suite


def canonical(frame):
    frame = frame.copy().reindex(sorted(frame.columns), axis=1)
    for column in frame.columns:
        if column == "time":
            frame[column] = pd.to_datetime(frame[column], utc=True)
        elif frame[column].dtype == object or pd.api.types.is_string_dtype(frame[column]):
            try:
                frame[column] = pd.to_numeric(frame[column])
            except (ValueError, TypeError):
                frame[column] = frame[column].fillna("<null>").astype(str)
    return frame.sort_values(list(frame.columns), na_position="last").reset_index(drop=True)


def case_groups(suite, selected=None):
    selected = set(selected or ())
    unknown = selected - {case["id"] for case in suite["cases"]}
    if unknown:
        raise ValueError(f"Unknown question IDs: {sorted(unknown)}")
    groups = {}
    for case in suite["cases"]:
        groups.setdefault(case.get("conversation_group", case["id"]), []).append(case)
    return [group for group in groups.values() if not selected or any(case["id"] in selected for case in group)]


def compare_values(observed, expected):
    assert_frame_equal(canonical(observed), canonical(expected), check_dtype=False,
                       check_exact=False, rtol=1e-7, atol=1e-7)


def read_result_csv(text):
    # A city literally named "null" is text, not a missing SQL value.
    return pd.read_csv(io.StringIO(text), keep_default_na=False, na_values=[""])


def compare_ranking(observed, reference_sql):
    tree = parse_one(reference_sql, read="postgres")
    order = tree.args.get("order")
    if order is not None and tree.args.get("limit") is not None:
        first = order.expressions[0]
        values = observed[first.this.name]
        assert values.is_monotonic_decreasing if first.args.get("desc") else values.is_monotonic_increasing, "Incorrect ranking order"


def run_partition(args, suite, groups, worker):
    from playwright.sync_api import sync_playwright

    output = args.output
    db = dict(host=args.pg_host, port=args.pg_port, dbname=args.pg_database)
    results = []

    def query(statement, params=(), *, metadata=False):
        connection = psycopg2.connect(**db, user=args.writer_role if metadata else args.reader_role)
        try:
            connection.set_session(readonly=True)
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '30s'")
                cursor.execute(statement, params)
                return pd.DataFrame(cursor.fetchall(), columns=[c.name for c in cursor.description])
        finally:
            connection.close()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.set_default_timeout(240_000)
        try:
            page.goto(args.url + "/embedded-copilot", wait_until="networkidle")
            assert page.request.get(args.url + "/api/project/runtime-contract").status == 401
            page.locator("#login-username").fill(args.username)
            page.locator("#login-password").fill(args.password_file.read_text().strip())
            page.locator("#login-submit").click()
            page.locator("#login-gate").wait_for(state="hidden")
            bootstrap = page.request.post(args.url + "/api/copilot/bootstrap", data={"surface_id": "embedded-copilot"})
            assert bootstrap.ok and bootstrap.json()["conversation_scope"]["workspace_id"] == suite["workspace_id"]
            contract = page.request.get(args.url + "/api/project/runtime-contract").json()
            assert len(contract["metric_ids"]) == suite["metric_count"]
            chat = page.locator("vanna-chat")
            for group in groups:
                page.locator("#new-analysis").click()
                previous_artifact, previous_values = None, None
                for spec in group:
                    result = {"id": spec["id"], "category": spec["category"], "status": "failed", "worker": worker}
                    results.append(result)
                    started = time.monotonic()
                    try:
                        page.wait_for_function("() => !document.querySelector('vanna-chat').shadowRoot.querySelector('textarea.message-input').disabled")
                        question = spec["question"]
                        chat.locator("textarea.message-input").fill(question)
                        with page.expect_response(lambda response, question=question: response.url.endswith("/chat_sse") and response.request.post_data_json.get("message") == question) as pending:
                            chat.locator("button.send-button").click()
                        response = pending.value
                        payload = response.text()
                        (output / f"{spec['id']}.sse.txt").write_text(payload)
                        assert response.ok, f"SSE HTTP {response.status}"
                        page.wait_for_function("() => !document.querySelector('vanna-chat').shadowRoot.querySelector('textarea.message-input').disabled")
                        cid = chat.evaluate("el => el.conversationId")
                        result["conversation_id"] = cid
                        detail = page.request.get(args.url + f"/api/project/conversations/{cid}").json()
                        (output / f"{spec['id']}.conversation.json").write_text(json.dumps(detail, ensure_ascii=False, indent=2))
                        traces = query("SELECT termination_reason,sql_calls_used,catalog_trace FROM app.agent_runs WHERE conversation_id=%s ORDER BY started_at DESC LIMIT 1", (cid,), metadata=True)
                        trace = traces.iloc[0].to_dict()
                        (output / f"{spec['id']}.trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2, default=str))
                        audits = query("SELECT policy_status,original_sql,final_sql,policy_reason,error_message FROM app.query_audits WHERE conversation_id=%s ORDER BY created_at", (cid,), metadata=True)
                        (output / f"{spec['id']}.sql.json").write_text(audits.to_json(orient="records", force_ascii=False, indent=2))
                        ct = trace["catalog_trace"] or {}
                        result.update({"termination_reason": trace["termination_reason"], "sql_calls_used": int(trace["sql_calls_used"] or 0), "route_state": ct.get("route_state")})
                        artifact = detail.get("trusted_result_artifact")
                        if spec.get("no_sql"):
                            assert result["sql_calls_used"] == 0, "Unexpected SQL for boundary request"
                            assert not artifact, "Boundary request has a numeric artifact"
                            assert result["route_state"] in spec["expected_routes"], "Unexpected boundary handling"
                        elif spec.get("empty"):
                            expected = query(spec["reference_sql"])
                            assert expected.empty, "Reference window must really be empty"
                            assert result["sql_calls_used"] > 0, "Empty-result question did not reach SQL"
                            assert not artifact, "Empty result acquired a numeric artifact"
                            assert trace["termination_reason"] not in {"budget_exhausted", "llm_error", "query_timeout"}
                            result["independent_values"] = "empty_verified"
                        else:
                            assert result["termination_reason"] == "completed", "Request did not complete"
                            assert artifact, "Missing trusted result artifact"
                            csv = page.request.get(args.url + f"/api/project/conversations/{cid}/result.csv")
                            assert csv.ok, "Authorized CSV unavailable"
                            (output / f"{spec['id']}.csv").write_text(csv.text())
                            observed = read_result_csv(csv.text())
                            result.update({"rows": len(observed), "artifact_id": artifact["artifact_id"]})
                            expected = query(spec["reference_sql"]) if spec.get("reference_sql") else previous_values
                            if spec.get("reference_sql"):
                                (output / f"{spec['id']}.expected.csv").write_text(expected.to_csv(index=False))
                            compare_values(observed, expected)
                            if spec.get("reference_sql"):
                                compare_ranking(observed, spec["reference_sql"])
                            result["independent_values"] = "passed"
                            if spec.get("reuse"):
                                assert result["sql_calls_used"] == 0, "Result reuse re-executed SQL"
                                assert artifact["artifact_id"] == previous_artifact, "Result reuse changed artifact"
                            if spec.get("chart"):
                                assert (ct.get("chart_contract") or {}).get("chart_type") == spec["chart"], "Wrong chart type"
                                chat.locator(".js-plotly-plot").last.wait_for(state="visible")
                            if spec.get("pagination"):
                                assert len(observed) > 200, "Pagination scenario does not exceed preview"
                                preview = page.request.get(args.url + f"/api/project/conversations/{cid}/artifacts/{artifact['artifact_id']}/rows?cursor=200&page_size=200")
                                result["pagination_http_status"] = preview.status
                                assert preview.ok, "Second result page unavailable"
                                page_data = preview.json()
                                assert page_data["total_rows"] == len(observed)
                                assert page_data["cursor"] == 200 and page_data["page_rows"] == 200
                                compare_values(pd.DataFrame(page_data["rows"]), observed.iloc[200:400])
                                (output / f"{spec['id']}.page2.json").write_text(json.dumps(page_data, ensure_ascii=False, indent=2))
                            previous_artifact, previous_values = artifact["artifact_id"], expected
                        result["status"] = "passed"
                    except Exception as exc:
                        result["error"] = str(exc)
                    finally:
                        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
                        page.screenshot(path=str(output / f"{spec['id']}.png"), full_page=True)
                        (output / f"{spec['id']}.result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
                        print(json.dumps(result, ensure_ascii=False), flush=True)
        finally:
            browser.close()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:32104")
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--username", default="analyst")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/cases/thelook_product_queries.yaml")
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--pg-host", default="/tmp")
    parser.add_argument("--pg-port", type=int, default=35434)
    parser.add_argument("--pg-database", default="thelook_analytics")
    parser.add_argument("--reader-role", default="daa_thelook_reader")
    parser.add_argument("--writer-role", default="daa_thelook_app_writer")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    suite = load_cases(args.cases)
    try:
        groups = case_groups(suite, args.case_id)
    except ValueError as exc:
        parser.error(str(exc))
    records = []
    partitions = [groups[i::args.workers] for i in range(args.workers)]
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(run_partition, args, suite, partition, i) for i, partition in enumerate(partitions) if partition]
        for future in as_completed(futures):
            records.extend(future.result())
    order = {case["id"]: i for i, case in enumerate(suite["cases"])}
    records.sort(key=lambda case: order[case["id"]])
    report = {"status": "passed" if all(case["status"] == "passed" for case in records) else "failed",
              "count": len(records), "passed": sum(case["status"] == "passed" for case in records),
              "numeric_checked": sum(case.get("independent_values") == "passed" for case in records),
              "categories": dict(Counter(case["category"] for case in records)), "cases": records}
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False))
    return int(report["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
