#!/usr/bin/env python3
"""Run the Phase-B single-turn baseline through the trusted demo SSE API.

Raw SSE and database evidence are written outside the repository. The report
written by this script is aggregate-safe and contains no question, SQL,
result rows, assistant text, credentials, or raw provider payloads.
"""

from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
from time import perf_counter, sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

from dotenv import load_dotenv
import psycopg2
import psycopg2.extras
import yaml

from data_analysis_agent.postgres_runner import PostgresConnectionSettings
from data_analysis_agent.product_capability_contract import (
    SUITE_ID,
    validate_product_capability_suite,
    validate_safe_evidence,
)
from data_analysis_agent.product_capability_review import validate_manual_review


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASE_FILE = ROOT / "evals/cases/product_capability_v1.yaml"
DEFAULT_RAW_ROOT = Path("/disk2/gengnan/data-analysis-agent-data/evals/product-capability-baseline-v1/phase-b")
LABEL_FIELDS = ("route_correct", "sql_executable", "result_contract_valid", "permission_compliant", "answer_grounded", "chart_quality")


class DemoSseClient:
    def __init__(self, base_url: str, role: str, timeout_seconds: int):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self._post_json("/api/project/demo-session", {"role": role})

    def _post_json(self, path: str, payload: dict[str, Any]) -> bytes:
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        with self.opener.open(request, timeout=self.timeout_seconds) as response:
            return response.read()

    def stream(
        self,
        *,
        case_id: str,
        question: str,
        conversation_id: str | None = None,
        request_id: str | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
        request_id = request_id or f"pcv1-b-{case_id}-{uuid4().hex[:12]}"
        conversation_id = conversation_id or f"pcv1-b-{case_id}-{uuid4().hex[:12]}"
        payload = {
            "message": question,
            "conversation_id": conversation_id,
            "request_id": request_id,
            "metadata": {"evaluation_case_id": case_id},
        }
        events: list[dict[str, Any]] = []
        error: str | None = None
        try:
            request = Request(
                f"{self.base_url}/api/vanna/v2/chat_sse",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Accept": "text/event-stream", "Content-Type": "application/json"},
                method="POST",
            )
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    if data:
                        try:
                            events.append(json.loads(data))
                        except json.JSONDecodeError:
                            continue
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            error = type(exc).__name__
        return {
            "request_id": request_id,
            "conversation_id": conversation_id,
            "client_error": error,
        }, events, error


def _audit_evidence(request_id: str, settings: PostgresConnectionSettings) -> dict[str, Any] | None:
    connection = psycopg2.connect(host=settings.host, port=settings.port, database=settings.database, user=settings.writer_user)
    try:
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT ar.termination_reason, ar.error_type,
                       ar.tool_calls_used, ar.sql_calls_used, ar.visualization_calls_used,
                       ar.llm_rounds_used, ar.context_chars, ar.context_truncated,
                       ar.catalog_trace, ar.repair_evidence,
                       COUNT(qa.audit_id) AS audit_count,
                       COUNT(*) FILTER (WHERE qa.policy_status = 'allowed') AS allowed_audit_count,
                       COUNT(*) FILTER (WHERE qa.policy_status = 'rejected') AS rejected_audit_count,
                       COUNT(*) FILTER (WHERE qa.policy_status = 'execution_error') AS execution_error_audit_count,
                       MAX(qa.row_count) FILTER (WHERE qa.policy_status = 'allowed') AS row_count
                FROM app.agent_runs ar
                LEFT JOIN app.query_audits qa ON qa.request_id = ar.request_id
                WHERE ar.request_id = %s
                GROUP BY ar.run_id
                """,
                (request_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    finally:
        connection.close()


def external_failure_diagnostic(
    request_id: str, settings: PostgresConnectionSettings
) -> dict[str, Any]:
    """Read restricted, failure-classification evidence for one live request.

    This payload intentionally contains candidate/final SQL and the persisted
    safe execution error text, so it must only be written to a repository-
    external diagnostic artifact.  It is never returned by ``_safe_runtime``
    or embedded in a safe report.  Keeping this read path separate makes the
    privacy boundary mechanically visible in the Phase-B/C runners.
    """
    connection = psycopg2.connect(
        host=settings.host,
        port=settings.port,
        database=settings.database,
        user=settings.writer_user,
    )
    try:
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT run_id, termination_reason, error_type, tool_calls_used,
                       sql_calls_used, llm_rounds_used, catalog_trace,
                       repair_evidence, finished_at
                FROM app.agent_runs
                WHERE request_id = %s
                """,
                (request_id,),
            )
            run = cursor.fetchone()
            cursor.execute(
                """
                SELECT audit_id, original_sql, final_sql, policy_status,
                       policy_reason, elapsed_ms, row_count, error_message,
                       repair_evidence
                FROM app.query_audits
                WHERE request_id = %s
                ORDER BY audit_id ASC
                """,
                (request_id,),
            )
            audits = [dict(item) for item in cursor.fetchall()]
    finally:
        connection.close()
    return {
        "schema_version": "external-failure-diagnostic-v1",
        "request_id_sha256": sha256(request_id.encode("utf-8")).hexdigest(),
        "agent_run": dict(run) if run else None,
        "query_audits": audits,
        "privacy": (
            "Restricted repository-external diagnostic. May contain candidate SQL "
            "and safe execution-error text; do not copy into Git or safe reports."
        ),
    }


def write_external_failure_diagnostic(
    *,
    request_id: str,
    settings: PostgresConnectionSettings,
    output: Path,
) -> str:
    """Write one owner-only diagnostic file and return its content digest."""
    payload = external_failure_diagnostic(request_id, settings)
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n").encode(
        "utf-8"
    )
    output.write_bytes(encoded)
    # These artifacts include data-plane diagnostics and must not inherit a
    # permissive umask from a shared evaluation shell.
    os.chmod(output, 0o600)
    return sha256(encoded).hexdigest()


def _safe_runtime(row: dict[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {"agent_run_found": False}
    trace = row.get("catalog_trace") if isinstance(row.get("catalog_trace"), dict) else {}
    repair = row.get("repair_evidence") if isinstance(row.get("repair_evidence"), dict) else {}
    return {
        "agent_run_found": True,
        "termination_reason": row.get("termination_reason"),
        "error_type": row.get("error_type"),
        "tool_calls_used": row.get("tool_calls_used"),
        "sql_calls_used": row.get("sql_calls_used"),
        "visualization_calls_used": row.get("visualization_calls_used"),
        "llm_rounds_used": row.get("llm_rounds_used"),
        "context_chars": row.get("context_chars"),
        "context_truncated": bool(row.get("context_truncated")),
        "context_budget": _safe_context_budget(trace.get("context_budget")),
        "audit_count": int(row.get("audit_count") or 0),
        "allowed_audit_count": int(row.get("allowed_audit_count") or 0),
        "rejected_audit_count": int(row.get("rejected_audit_count") or 0),
        "execution_error_audit_count": int(row.get("execution_error_audit_count") or 0),
        "row_count": row.get("row_count"),
        "repair_attempted": bool(repair.get("repair_attempted")),
        "repair_succeeded": repair.get("repair_execution_status") == "succeeded",
        "route_intent": trace.get("route_intent") if isinstance(trace, dict) else None,
        "route_evidence_mode": trace.get("route_evidence_mode") if isinstance(trace, dict) else None,
        "route_state": trace.get("route_state") if isinstance(trace, dict) else None,
        "route_requires_database": trace.get("route_requires_database") if isinstance(trace, dict) else None,
        "working_memory": trace.get("working_memory") if isinstance(trace.get("working_memory"), dict) else None,
        "deterministic_chart_status": (
            (trace.get("deterministic_chart") or {}).get("status")
            if isinstance(trace.get("deterministic_chart"), dict)
            else None
        ),
    }


def _safe_context_budget(value: Any) -> dict[str, Any] | None:
    """Project content-free context provenance into a safe runtime report."""
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for key in (
        "version",
        "max_context_chars",
        "max_context_messages",
        "max_context_tokens",
        "max_prompt_tokens",
        "input_chars",
        "input_messages",
        "input_turns",
        "output_chars",
        "output_messages",
        "retained_turns",
        "omitted_turns",
        "summary_inserted",
        "current_turn_compacted",
        "current_user_exceeds_budget",
        "input_context_tokens",
        "output_context_tokens",
        "tokenizer_mode",
        "tokenizer_id",
    ):
        item = value.get(key)
        if isinstance(item, (str, int, bool)):
            result[key] = item
    summary = value.get("summary")
    if isinstance(summary, dict):
        safe_summary = {
            key: summary.get(key)
            for key in (
                "version",
                "source_turn_start",
                "source_turn_end",
                "source_message_count",
                "source_chars",
                "source_sha256",
                "reason",
                "summary_status",
                "summary_model",
                "summary_tokens",
            )
            if isinstance(summary.get(key), (str, int))
        }
        result["summary"] = safe_summary
    return result


def _automatic_labels(case: dict[str, Any], runtime: dict[str, Any]) -> dict[str, str]:
    expected = case["expected"]
    labels = dict.fromkeys(LABEL_FIELDS, "pending_manual")
    if case["category"] != "chart_request":
        labels["chart_quality"] = "not_applicable"
    if not runtime.get("agent_run_found"):
        return labels
    termination = runtime.get("termination_reason")
    sql_calls = int(runtime.get("sql_calls_used") or 0)
    allowed = int(runtime.get("allowed_audit_count") or 0)
    audits = int(runtime.get("audit_count") or 0)
    if expected["requires_database"]:
        labels["route_correct"] = "pass" if sql_calls > 0 else "fail"
        labels["sql_executable"] = "pass" if allowed > 0 else "fail"
        labels["result_contract_valid"] = "pass" if termination == "completed" and allowed > 0 else "fail"
        labels["permission_compliant"] = "pass" if sql_calls == 0 or audits >= sql_calls else "fail"
    else:
        labels["route_correct"] = "pass" if sql_calls == 0 else "fail"
        labels["sql_executable"] = "not_applicable"
        labels["result_contract_valid"] = "not_applicable"
        labels["permission_compliant"] = "pass" if sql_calls == 0 else "fail"
    if case["category"] != "chart_request" or not runtime.get("chart_component_emitted"):
        labels["chart_quality"] = "not_applicable"
    return labels


def _safe_event_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    rich_types = Counter(str((event.get("rich") or {}).get("type") or "unknown") for event in events)
    text_parts = []
    for event in events:
        simple = event.get("simple") or {}
        # Vanna's SimpleTextComponent is serialized as {"text": ...}; keep
        # the nested form as a compatibility fallback for older providers.
        text = simple.get("text")
        if not isinstance(text, str):
            text = (simple.get("data") or {}).get("text")
        if isinstance(text, str):
            text_parts.append(text)
    response_text = "\n".join(text_parts)
    return {
        "sse_event_count": len(events),
        "rich_component_types": dict(sorted(rich_types.items())),
        "chart_component_emitted": bool(rich_types.get("chart")),
        "dataframe_component_emitted": bool(rich_types.get("dataframe")),
        "has_text_response": bool(response_text.strip()),
        "answer_sha256": sha256(response_text.encode("utf-8")).hexdigest() if response_text else None,
    }


def run_suite(*, cases_path: Path, output: Path, raw_root: Path, base_url: str, role: str, timeout: int, pause: float) -> dict[str, Any]:
    raw = yaml.safe_load(cases_path.read_text(encoding="utf-8")) or {}
    validate_product_capability_suite(raw, require_complete=False)
    cases = raw.get("single_turn_cases") or []
    if len(cases) != 16:
        raise ValueError("Phase B requires exactly 16 single_turn_cases")
    settings = PostgresConnectionSettings.from_environment()
    client = DemoSseClient(base_url, role, timeout)
    raw_root.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for index, case in enumerate(cases, 1):
        started = perf_counter()
        client_meta, events, _ = client.stream(case_id=case["case_id"], question=case["question"])
        raw_path = raw_root / f"{case['case_id']}.json"
        raw_path.write_text(json.dumps({"client": client_meta, "events": events}, ensure_ascii=False), encoding="utf-8")
        evidence = _safe_runtime(_audit_evidence(client_meta["request_id"], settings))
        client_safe = {
            "client_error": client_meta["client_error"],
            "latency_ms": round((perf_counter() - started) * 1000),
            **_safe_event_summary(events),
        }
        runtime = {**evidence, **client_safe}
        labels = _automatic_labels(case, runtime)
        records.append({
            "case_id": case["case_id"],
            "status": "observed",
            "failure_layer": None,
            "evidence_codes": [
                "sse_completed" if client_meta["client_error"] is None else "sse_client_error",
                "agent_run_found" if evidence.get("agent_run_found") else "agent_run_missing",
            ],
            "artifact_sha256": sha256(raw_path.read_bytes()).hexdigest(),
            "latency_ms": runtime["latency_ms"],
            "manual_review_status": "pending",
            "runtime": runtime,
            "automatic_labels": labels,
        })
        print(f"[{index}/16] {case['case_id']}: run={evidence.get('agent_run_found')}, sql={runtime.get('sql_calls_used', 0)}, termination={runtime.get('termination_reason')}", flush=True)
        if pause and index < len(cases):
            sleep(pause)
    return {
        "suite_id": SUITE_ID,
        "suite_version": raw["suite_version"],
        "phase": "phase_b",
        "mode": "live_siliconflow_sse",
        "case_count": len(records),
        "records": records,
        "raw_artifact_root": str(raw_root),
        "report_privacy": "No question, assistant text, SQL text, result rows, cookie or credential is stored in this report.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASE_FILE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--base-url", default="http://127.0.0.1:32010")
    parser.add_argument("--role", choices=("analyst", "admin"), default="analyst")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--pause-seconds", type=float, default=1.0)
    parser.add_argument("--manual-review", type=Path)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    known = {
        str(item["case_id"])
        for item in (yaml.safe_load(args.cases.read_text(encoding="utf-8")) or {}).get("single_turn_cases", [])
    }
    manual = {}
    if args.manual_review:
        manual = validate_manual_review(json.loads(args.manual_review.read_text(encoding="utf-8")), known_case_ids=known)
    report = run_suite(cases_path=args.cases, output=args.output, raw_root=args.raw_root, base_url=args.base_url, role=args.role, timeout=args.timeout_seconds, pause=max(0.0, args.pause_seconds))
    if manual:
        for record in report["records"]:
            if record["case_id"] in manual:
                record["manual_review_status"] = manual[record["case_id"]]["review_status"]
                record["manual_labels"] = manual[record["case_id"]]
    validate_safe_evidence(
        {"suite_id": report["suite_id"], "phase": report["phase"], "records": report["records"]},
        known_case_ids=known,
        phase="phase_b",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
