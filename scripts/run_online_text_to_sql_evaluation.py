#!/usr/bin/env python3
"""Run a bounded live SiliconFlow Text-to-SQL evaluation against the trusted demo.

The runner uses the local FastAPI/SSE boundary, not a provider-private API. It
therefore measures the same route, Catalog, budget, SQL Policy, PostgreSQL,
ResultContract and response path that the embedded product uses. Reports avoid
questions, assistant text, SQL text, raw result rows and credentials; manual
semantic labels are deliberately supplied separately after evidence review.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from http.cookiejar import CookieJar
import json
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


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASE_FILE = ROOT / "evals/cases/text_to_sql_online_v1.yaml"
DEFAULT_SOURCE_FILE = ROOT / "evals/cases/text_to_sql_v2.yaml"
LABEL_FIELDS = (
    "route_correct",
    "clarification_correct",
    "sql_executable",
    "metric_semantics_correct",
    "result_contract_valid",
    "permission_compliant",
    "answer_grounded",
)
LABEL_VALUES = {"pass", "fail", "not_applicable", "pending_manual"}


@dataclass(frozen=True)
class LiveCase:
    """A live selection merged with its deterministic source contract."""

    case_id: str
    category: str
    question: str
    expected_state: str
    expected_intent: str | None
    requires_database: bool
    expected_metric_ids: tuple[str, ...]
    expected_required_result_columns: tuple[str, ...]
    review_focus: str


class DemoSseClient:
    """Cookie-aware client for the trusted local demo's SSE boundary."""

    def __init__(self, base_url: str, role: str, timeout_seconds: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self._post_json("/api/project/demo-session", {"role": role})

    def _post_json(self, path: str, payload: dict[str, Any]) -> bytes:
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Accept": "text/event-stream" if path.endswith("chat_sse") else "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with self.opener.open(request, timeout=self.timeout_seconds) as response:
            return response.read()

    def _stream_chat(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Read only through the SSE completion event, as the browser does.

        The server may keep the HTTP connection alive after emitting `[DONE]`.
        Waiting for physical connection close would turn a completed response
        into an artificial client timeout and stall a serial evaluation batch.
        """
        request = Request(
            f"{self.base_url}/api/vanna/v2/chat_sse",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Accept": "text/event-stream", "Content-Type": "application/json"},
            method="POST",
        )
        events: list[dict[str, Any]] = []
        with self.opener.open(request, timeout=self.timeout_seconds) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                if not data:
                    continue
                try:
                    events.append(json.loads(data))
                except json.JSONDecodeError:
                    continue
        return events

    def run_case(self, case: LiveCase) -> dict[str, Any]:
        request_id = f"online-{case.case_id}-{uuid4().hex[:12]}"
        conversation_id = f"online-{case.case_id}-{uuid4().hex[:12]}"
        payload = {
            "message": case.question,
            "conversation_id": conversation_id,
            "request_id": request_id,
            "metadata": {"evaluation_case_id": case.case_id},
        }
        started = perf_counter()
        raw_events: list[dict[str, Any]] = []
        raw_text_parts: list[str] = []
        response_error: str | None = None
        try:
            raw_events = self._stream_chat(payload)
            for event in raw_events:
                simple = event.get("simple") or {}
                simple_data = simple.get("data") or {}
                text = simple_data.get("text")
                if isinstance(text, str) and text:
                    raw_text_parts.append(text)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            response_error = type(exc).__name__

        response_text = "\n".join(raw_text_parts)
        rich_types = Counter(
            str((event.get("rich") or {}).get("type") or "unknown")
            for event in raw_events
        )
        return {
            "request_id": request_id,
            "conversation_id": conversation_id,
            "client_latency_ms": round((perf_counter() - started) * 1000),
            "response_error": response_error,
            "sse_event_count": len(raw_events),
            "rich_component_types": dict(sorted(rich_types.items())),
            "answer_sha256": sha256(response_text.encode("utf-8")).hexdigest()
            if response_text
            else None,
            "has_text_response": bool(response_text.strip()),
        }


def _load_live_cases(case_file: Path, source_file: Path) -> tuple[dict[str, Any], list[LiveCase]]:
    manifest = yaml.safe_load(case_file.read_text(encoding="utf-8")) or {}
    source = yaml.safe_load(source_file.read_text(encoding="utf-8")) or {}
    source_cases = {str(case["id"]): case for case in source.get("cases", [])}
    selected = manifest.get("cases") or []
    identifiers = [str(item.get("source_id", "")) for item in selected]
    if not 20 <= len(selected) <= 30:
        raise ValueError("live evaluation must select 20–30 representative cases")
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("live evaluation case IDs must be unique")

    cases: list[LiveCase] = []
    for selection in selected:
        source_id = str(selection.get("source_id", ""))
        source_case = source_cases.get(source_id)
        if source_case is None:
            raise ValueError(f"unknown source case: {source_id}")
        review_focus = str(selection.get("review_focus", "")).strip()
        if not review_focus:
            raise ValueError(f"missing review_focus for {source_id}")
        cases.append(
            LiveCase(
                case_id=source_id,
                category=str(source_case.get("category", "uncategorized")),
                question=str(source_case.get("question", "")),
                expected_state=str(source_case.get("expected_state", "")),
                expected_intent=source_case.get("expected_intent"),
                requires_database=bool(source_case.get("requires_database")),
                expected_metric_ids=tuple(source_case.get("expected_metric_ids", [])),
                expected_required_result_columns=tuple(
                    source_case.get("expected_required_result_columns", [])
                ),
                review_focus=review_focus,
            )
        )
    return manifest, cases


def _database_evidence(request_id: str, settings: PostgresConnectionSettings) -> dict[str, Any] | None:
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
                SELECT
                    ar.termination_reason, ar.error_type,
                    ar.tool_calls_used, ar.sql_calls_used, ar.visualization_calls_used,
                    ar.llm_rounds_used, ar.input_tokens, ar.output_tokens, ar.total_tokens,
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
    finally:
        connection.close()
    return dict(row) if row else None


def _automatic_labels(case: LiveCase, evidence: dict[str, Any] | None) -> dict[str, str]:
    labels = dict.fromkeys(LABEL_FIELDS, "pending_manual")
    if evidence is None:
        return labels
    termination = evidence.get("termination_reason")
    sql_calls = int(evidence.get("sql_calls_used") or 0)
    allowed = int(evidence.get("allowed_audit_count") or 0)
    rejected = int(evidence.get("rejected_audit_count") or 0)

    if case.expected_state == "catalog_answered":
        labels.update(
            route_correct="pass" if termination == "catalog_answered" and sql_calls == 0 else "fail",
            clarification_correct="not_applicable",
            sql_executable="not_applicable",
            metric_semantics_correct="pending_manual",
            result_contract_valid="not_applicable",
            permission_compliant="pass" if sql_calls == 0 else "fail",
            answer_grounded="pending_manual",
        )
    elif case.expected_state in {"missing_time", "missing_metric", "missing_comparison", "clarification_required"}:
        labels.update(
            route_correct="pass" if termination == "clarification_required" and sql_calls == 0 else "fail",
            clarification_correct="pass" if termination == "clarification_required" and sql_calls == 0 else "fail",
            sql_executable="not_applicable",
            metric_semantics_correct="not_applicable",
            result_contract_valid="not_applicable",
            permission_compliant="pass" if sql_calls == 0 else "fail",
            answer_grounded="pending_manual",
        )
    elif case.expected_state in {"unauthorized", "unsupported"}:
        labels.update(
            route_correct="pass" if termination == "unsupported_request" and sql_calls == 0 else "fail",
            clarification_correct="not_applicable",
            sql_executable="not_applicable",
            metric_semantics_correct="not_applicable",
            result_contract_valid="not_applicable",
            permission_compliant="pass" if sql_calls == 0 and rejected == 0 else "fail",
            answer_grounded="pending_manual",
        )
    elif case.requires_database:
        labels.update(
            route_correct="pass" if sql_calls > 0 else "fail",
            clarification_correct="not_applicable",
            sql_executable="pass" if allowed > 0 else "fail",
            metric_semantics_correct="pending_manual",
            result_contract_valid=(
                "pass" if termination == "completed" and allowed > 0 else "fail"
            ),
            # A rejected candidate is a policy enforcement success, not a
            # permission breach. A real breach would be an unrecorded SQL call
            # or a query bypassing the only registered SQL tool.
            permission_compliant=(
                "pass" if sql_calls == 0 or int(evidence.get("audit_count") or 0) >= sql_calls else "fail"
            ),
            answer_grounded="pending_manual",
        )
    else:
        labels.update(
            route_correct="pass" if termination == "completed" and sql_calls == 0 else "fail",
            clarification_correct="not_applicable",
            sql_executable="not_applicable",
            metric_semantics_correct="not_applicable",
            result_contract_valid="not_applicable",
            permission_compliant="pass" if sql_calls == 0 else "fail",
            answer_grounded="pending_manual",
        )
    return labels


def _load_manual_labels(path: Path | None, known_case_ids: set[str]) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases = payload.get("cases") or {}
    if not isinstance(cases, dict):
        raise ValueError("manual label file must contain a cases mapping")
    unknown = set(cases) - known_case_ids
    if unknown:
        raise ValueError(f"manual label file references unknown cases: {sorted(unknown)}")
    labels: dict[str, dict[str, str]] = {}
    for case_id, item in cases.items():
        if not isinstance(item, dict):
            raise ValueError(f"manual labels for {case_id} must be a mapping")
        reviewed = {field: item[field] for field in LABEL_FIELDS if field in item}
        for field, value in reviewed.items():
            if value not in LABEL_VALUES:
                raise ValueError(f"invalid {field} label for {case_id}: {value}")
        labels[str(case_id)] = reviewed
    return labels


def _redacted_evidence(row: dict[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {"agent_run_found": False}
    trace = row.get("catalog_trace") or {}
    repair = row.get("repair_evidence") or {}
    performance = trace.get("performance") if isinstance(trace, dict) else None
    has_usage = any(
        row.get(field) is not None
        for field in ("input_tokens", "output_tokens", "total_tokens")
    )
    return {
        "agent_run_found": True,
        "termination_reason": row.get("termination_reason"),
        "error_type": row.get("error_type"),
        "tool_calls_used": row.get("tool_calls_used"),
        "sql_calls_used": row.get("sql_calls_used"),
        "visualization_calls_used": row.get("visualization_calls_used"),
        "llm_rounds_used": row.get("llm_rounds_used"),
        "input_tokens": row.get("input_tokens"),
        "output_tokens": row.get("output_tokens"),
        "total_tokens": row.get("total_tokens"),
        # Some OpenAI-compatible provider responses currently omit `usage`.
        # Preserve that distinction for every case instead of reporting zero.
        "token_usage_status": "reported" if has_usage else "unknown",
        "audit_count": int(row.get("audit_count") or 0),
        "allowed_audit_count": int(row.get("allowed_audit_count") or 0),
        "rejected_audit_count": int(row.get("rejected_audit_count") or 0),
        "execution_error_audit_count": int(row.get("execution_error_audit_count") or 0),
        "row_count": row.get("row_count"),
        "performance": performance if isinstance(performance, dict) else {},
        "repair_attempted": bool(repair.get("repair_attempted")) if isinstance(repair, dict) else False,
        "repair_succeeded": bool(repair.get("repair_execution_status") == "succeeded") if isinstance(repair, dict) else False,
        "repair_terminal_reason": repair.get("terminal_reason") if isinstance(repair, dict) else None,
    }


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    labels = {
        field: Counter(result["labels"][field] for result in results)
        for field in LABEL_FIELDS
    }
    return {
        "total_cases": len(results),
        "agent_runs_found": sum(result["runtime"]["agent_run_found"] for result in results),
        "client_errors": sum(bool(result["client"]["response_error"]) for result in results),
        "total_client_latency_ms": sum(result["client"]["client_latency_ms"] for result in results),
        "labels": {field: dict(counter) for field, counter in labels.items()},
        "pending_manual_cases": sum(
            any(value == "pending_manual" for value in result["labels"].values())
            for result in results
        ),
    }


def run_suite(
    *,
    case_file: Path,
    source_file: Path,
    base_url: str,
    role: str,
    timeout_seconds: int,
    pause_seconds: float,
    manual_labels: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    manifest, cases = _load_live_cases(case_file, source_file)
    settings = PostgresConnectionSettings.from_environment()
    client = DemoSseClient(base_url, role, timeout_seconds)
    results: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        client_result = client.run_case(case)
        evidence = _database_evidence(client_result["request_id"], settings)
        runtime = _redacted_evidence(evidence)
        results.append(
            {
                "id": case.case_id,
                "category": case.category,
                "expected": {
                    "state": case.expected_state,
                    "intent": case.expected_intent,
                    "requires_database": case.requires_database,
                    "metric_ids": list(case.expected_metric_ids),
                    "required_result_columns": list(case.expected_required_result_columns),
                    "review_focus": case.review_focus,
                },
                "client": client_result,
                "runtime": runtime,
                "labels": {
                    **_automatic_labels(case, evidence),
                    **(manual_labels or {}).get(case.case_id, {}),
                },
            }
        )
        print(
            f"[{index + 1}/{len(cases)}] {case.case_id}: "
            f"run={'yes' if runtime['agent_run_found'] else 'no'}, "
            f"sql={runtime.get('sql_calls_used', 0)}, "
            f"termination={runtime.get('termination_reason')}",
            flush=True,
        )
        if pause_seconds and index + 1 < len(cases):
            sleep(pause_seconds)
    return {
        "suite_version": manifest.get("version"),
        "source_suite": manifest.get("source_suite"),
        "dataset": manifest.get("dataset"),
        "dataset_version": manifest.get("dataset_version"),
        "catalog_version": manifest.get("catalog_version"),
        "model_provider": manifest.get("model_provider"),
        "model": manifest.get("model"),
        "mode": "live_siliconflow_sse",
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "report_privacy": "No question, assistant text, SQL text, raw result rows, cookie or credential is stored.",
        "results": results,
        "summary": _summary(results),
    }


def refresh_report(
    report_path: Path,
    *,
    case_file: Path,
    source_file: Path,
    manual_labels: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Refresh only local run evidence and labels without making model calls."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    _, cases = _load_live_cases(case_file, source_file)
    case_map = {case.case_id: case for case in cases}
    settings = PostgresConnectionSettings.from_environment()
    for result in report.get("results", []):
        case_id = str(result.get("id", ""))
        case = case_map.get(case_id)
        if case is None:
            raise ValueError(f"report references unknown case: {case_id}")
        request_id = str((result.get("client") or {}).get("request_id", ""))
        evidence = _database_evidence(request_id, settings)
        result["runtime"] = _redacted_evidence(evidence)
        result["labels"] = {
            **_automatic_labels(case, evidence),
            **(manual_labels or {}).get(case_id, {}),
        }
    report["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    report["summary"] = _summary(report["results"])
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASE_FILE)
    parser.add_argument("--source-cases", type=Path, default=DEFAULT_SOURCE_FILE)
    parser.add_argument("--base-url", default="http://127.0.0.1:32010")
    parser.add_argument("--role", choices=("analyst", "admin"), default="analyst")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--pause-seconds", type=float, default=1.0)
    parser.add_argument(
        "--manual-labels",
        type=Path,
        help="Optional YAML map of human-reviewed labels; never contains raw answers or SQL.",
    )
    parser.add_argument(
        "--refresh-report",
        type=Path,
        help="Refresh an existing report from PostgreSQL without calling the model.",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    _, cases = _load_live_cases(args.cases, args.source_cases)
    manual_labels = _load_manual_labels(
        args.manual_labels, {case.case_id for case in cases}
    )
    if args.refresh_report:
        report = refresh_report(
            args.refresh_report,
            case_file=args.cases,
            source_file=args.source_cases,
            manual_labels=manual_labels,
        )
    else:
        report = run_suite(
            case_file=args.cases,
            source_file=args.source_cases,
            base_url=args.base_url,
            role=args.role,
            timeout_seconds=args.timeout_seconds,
            pause_seconds=max(0.0, args.pause_seconds),
            manual_labels=manual_labels,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary = report["summary"]
    print(
        f"{summary['agent_runs_found']}/{summary['total_cases']} agent runs recorded; "
        f"{summary['pending_manual_cases']} cases still require semantic/grounding review"
    )


if __name__ == "__main__":
    main()
