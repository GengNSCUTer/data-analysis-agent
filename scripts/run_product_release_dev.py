#!/usr/bin/env python3
"""Run the independent product-development suite through the online SSE path.

The case file contains questions and structural expectations only. Raw SSE,
SQL audit rows and database result values stay outside the repository. The
safe report records the layer where an expectation diverged; it does not call
that divergence a model failure without the corresponding external evidence.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from time import perf_counter, sleep
from typing import Any, Mapping
from uuid import uuid4

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from data_analysis_agent.postgres_runner import PostgresConnectionSettings
from run_product_capability_online import (
    DemoSseClient,
    _audit_evidence,
    _safe_event_summary,
    _safe_runtime,
    write_external_failure_diagnostic,
)
DEFAULT_CASES = ROOT / "evals/cases/product_release_dev_v1.yaml"
DEFAULT_OUTPUT = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/product-release-dev-v1/"
    "safe-report.json"
)
DEFAULT_RAW_ROOT = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/product-release-dev-v1/raw"
)
CATEGORY_NAMES = (
    "single_turn",
    "clarification",
    "comparison_trend",
    "multi_turn",
    "safety_scope",
    "chart_download_empty",
)
REPORT_FIELDS = frozenset(
    {
        "case_id",
        "category",
        "status",
        "failure_layer",
        "mismatches",
        "evidence_codes",
        "latency_ms",
        "sql_calls_used",
        "termination_reason",
        "chart_component_emitted",
        "independent_check_id",
        "manual_review_status",
        "step_count",
    }
)


class ProductReleaseSuiteError(ValueError):
    """Raised when the development manifest is malformed."""


def load_suite(path: Path) -> list[dict[str, Any]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if raw.get("suite_id") != "product-release-dev-v1":
        raise ProductReleaseSuiteError("unexpected product release suite")
    if raw.get("usage") != "runtime_regression_only":
        raise ProductReleaseSuiteError("product suite must remain runtime-only")
    cases = raw.get("cases")
    if not isinstance(cases, list) or len(cases) != 60:
        raise ProductReleaseSuiteError("product release suite must contain 60 cases")
    expected_counts = raw.get("category_targets")
    counts = Counter(str(item.get("category")) for item in cases)
    if counts != Counter(expected_counts or {}):
        raise ProductReleaseSuiteError(
            f"category quota mismatch: expected={expected_counts}, actual={dict(counts)}"
        )
    ids: set[str] = set()
    for item in cases:
        case_id = item.get("case_id")
        category = item.get("category")
        if not isinstance(case_id, str) or case_id in ids:
            raise ProductReleaseSuiteError(f"invalid or duplicate case_id: {case_id!r}")
        if category not in CATEGORY_NAMES:
            raise ProductReleaseSuiteError(f"unknown category: {category!r}")
        if category == "multi_turn":
            steps = item.get("steps")
            if not isinstance(steps, list) or not steps:
                raise ProductReleaseSuiteError(f"{case_id}: multi-turn steps required")
            if any(not isinstance(step.get("message"), str) for step in steps):
                raise ProductReleaseSuiteError(f"{case_id}: every step needs a message")
            if any(not isinstance(step.get("expected"), Mapping) for step in steps):
                raise ProductReleaseSuiteError(f"{case_id}: every step needs expected metadata")
        elif not isinstance(item.get("question"), str):
            raise ProductReleaseSuiteError(f"{case_id}: question required")
        elif not isinstance(item.get("expected"), Mapping):
            raise ProductReleaseSuiteError(f"{case_id}: expected must be an object")
        ids.add(case_id)
    return cases


def _safe_plan(trace: Mapping[str, Any]) -> dict[str, Any]:
    plan = trace.get("query_plan")
    if not isinstance(plan, Mapping):
        return {}
    keys = (
        "intent",
        "metric_ids",
        "dimensions",
        "filters",
        "time_grain",
        "time_range",
        "top_n",
        "comparison_baseline",
        "query_plan_version",
        "required_result_columns",
    )
    return {
        key: plan.get(key)
        for key in keys
        if isinstance(plan.get(key), (str, int, float, bool, list, dict))
        or plan.get(key) is None
    }


def _safe_chart(trace: Mapping[str, Any]) -> dict[str, Any]:
    chart = trace.get("chart_contract")
    if not isinstance(chart, Mapping):
        return {}
    return {
        key: chart.get(key)
        for key in ("status", "requested", "chart_type", "x_column", "y_columns")
        if isinstance(chart.get(key), (str, int, float, bool, list)) or chart.get(key) is None
    }


def _actual(case: Mapping[str, Any], runtime: Mapping[str, Any], trace: Mapping[str, Any]) -> dict[str, Any]:
    plan = _safe_plan(trace)
    chart = _safe_chart(trace)
    semantic = trace.get("semantic_intent") or {}
    filters = plan.get("filters") or []
    return {
        "route_state": trace.get("route_state"),
        "intent": trace.get("route_intent") or plan.get("intent"),
        "requires_database": trace.get("route_requires_database"),
        "metric_ids": plan.get("metric_ids", semantic.get("metric_ids", [])),
        "dimensions": plan.get("dimensions", []),
        "filter_dimensions": sorted({item["dimension"] for item in filters}),
        "result_reused": trace.get("result_reuse") == "validated_result",
        "time_grain": plan.get("time_grain"),
        "time_range": plan.get("time_range"),
        "top_n": plan.get("top_n"),
        "comparison_baseline": plan.get("comparison_baseline"),
        "chart_type": chart.get("chart_type"),
        "chart_status": chart.get("status"),
        "sql_calls_used": int(runtime.get("sql_calls_used") or 0),
        "termination_reason": runtime.get("termination_reason"),
        "has_chart_component": bool(runtime.get("chart_component_emitted")),
    }


def _compare(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> list[str]:
    mismatches: list[str] = []
    for key in (
        "route_state",
        "intent",
        "requires_database",
        "metric_ids",
        "dimensions",
        "time_grain",
    ):
        if key not in expected:
            continue
        expected_value = expected[key]
        actual_value = actual.get(key)
        if key == "metric_ids":
            if set(expected_value) != set(actual_value or ()):
                mismatches.append(key)
        elif expected_value != actual_value:
            mismatches.append(key)
    if expected.get("top_n") is not None:
        actual_top_n = actual.get("top_n")
        if isinstance(actual_top_n, Mapping):
            values = set(actual_top_n.values())
            top_n_matches = len(values) == 1 and next(iter(values), None) == expected["top_n"]
        else:
            top_n_matches = actual_top_n == expected["top_n"]
        if not top_n_matches:
            mismatches.append("top_n")
    comparison = expected.get("comparison")
    if comparison == "year_over_year":
        time_range = actual.get("time_range") or {}
        if actual.get("time_grain") != "year" or not (
            isinstance(time_range, Mapping)
            and str(time_range.get("start", ""))[:4].isdigit()
            and str(time_range.get("end", ""))[:4].isdigit()
            and int(str(time_range["end"])[:4]) - int(str(time_range["start"])[:4]) >= 2
        ):
            mismatches.append("comparison_shape")
    elif comparison == "half_year":
        if not actual.get("time_range"):
            mismatches.append("comparison_shape")
    elif comparison == "dimension_comparison":
        # A grouped result across the requested dimension is already a valid
        # comparison. Numeric/member correctness is checked independently.
        dimensions = actual.get("dimensions") or []
        if not dimensions:
            mismatches.append("comparison_shape")
    if expected.get("requires_filter") and expected["requires_filter"] not in actual.get("filter_dimensions", []):
        mismatches.append("filter_missing")
    if expected.get("requires_filter_removed") in actual.get("filter_dimensions", []):
        mismatches.append("filter_not_removed")
    if expected.get("requires_reference") and expected["requires_reference"] not in actual.get("filter_dimensions", []):
        mismatches.append("reference_filter_missing")
    if expected.get("reuse_result") and not actual.get("result_reused"):
        mismatches.append("result_not_reused")
    if expected.get("chart_type"):
        if expected["chart_type"] != actual.get("chart_type"):
            mismatches.append("chart_type")
        if not actual.get("has_chart_component"):
            mismatches.append("chart_component")
    if expected.get("requires_database") and actual.get("termination_reason") != "completed":
        mismatches.append("execution")
    if not expected.get("requires_database") and actual.get("sql_calls_used"):
        mismatches.append("unexpected_sql")
    return sorted(set(mismatches))


def _one_turn(
    *,
    client: DemoSseClient,
    case_id: str,
    message: str,
    conversation_id: str,
    settings: PostgresConnectionSettings,
    raw_path: Path,
    diagnostic_path: Path,
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    started = perf_counter()
    meta, events, error = client.stream(
        case_id=case_id,
        question=message,
        conversation_id=conversation_id,
        request_id=f"prd-{case_id}-{uuid4().hex[:12]}",
    )
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps({"client": meta, "events": events}, ensure_ascii=False), encoding="utf-8")
    raw_path.chmod(0o600)
    evidence = _audit_evidence(meta["request_id"], settings)
    runtime = _safe_runtime(evidence)
    runtime.update(_safe_event_summary(events))
    runtime["client_error"] = error
    trace = evidence.get("catalog_trace") if isinstance(evidence, dict) else {}
    trace = trace if isinstance(trace, Mapping) else {}
    actual = _actual(expected, runtime, trace)
    mismatches = _compare(expected, actual)
    if error:
        mismatches.append("client_error")
    if not evidence:
        mismatches.append("missing_agent_run")
    if mismatches:
        write_external_failure_diagnostic(request_id=meta["request_id"], settings=settings, output=diagnostic_path)
    return {
        "status": "pass" if not mismatches else "fail",
        "mismatches": sorted(set(mismatches)),
        "actual": actual,
        "evidence_codes": [
            "agent_run_found" if evidence else "agent_run_missing",
            "sse_completed" if not error else "sse_client_error",
        ],
        "latency_ms": round((perf_counter() - started) * 1000),
        "sql_calls_used": actual["sql_calls_used"],
        "termination_reason": actual["termination_reason"],
        "chart_component_emitted": actual["has_chart_component"],
        "independent_check_id": expected.get("check_id"),
        "manual_review_status": "pending",
    }


def run_suite(*, cases_path: Path, output: Path, raw_root: Path, base_url: str, timeout: int, pause: float, selected: set[str] | None) -> dict[str, Any]:
    cases = load_suite(cases_path)
    if selected:
        unknown = selected - {str(item["case_id"]) for item in cases}
        if unknown:
            raise ProductReleaseSuiteError(f"unknown case IDs: {sorted(unknown)}")
        cases = [item for item in cases if item["case_id"] in selected]
    settings = PostgresConnectionSettings.from_environment()
    records: list[dict[str, Any]] = []
    raw_root.mkdir(parents=True, exist_ok=True)
    diagnostics = raw_root.parent / "diagnostics"
    for index, case in enumerate(cases, 1):
        case_id = str(case["case_id"])
        client = DemoSseClient(base_url, "analyst", timeout)
        conversation_id = f"prd-{case_id}-{uuid4().hex[:12]}"
        steps = case.get("steps") or [{"step_id": "turn-1", "message": case["question"], "expected": case["expected"]}]
        step_records: list[dict[str, Any]] = []
        for step in steps:
            if step.get("new_conversation_before"):
                conversation_id = f"prd-{case_id}-new-{uuid4().hex[:12]}"
            if step.get("reload_client_before"):
                client = DemoSseClient(base_url, "analyst", timeout)
            step_id = str(step["step_id"])
            result = _one_turn(
                client=client,
                case_id=f"{case_id}-{step_id}",
                message=str(step["message"]),
                conversation_id=conversation_id,
                settings=settings,
                raw_path=raw_root / f"{case_id}-{step_id}.json",
                diagnostic_path=diagnostics / f"{case_id}-{step_id}.json",
                expected=step["expected"],
            )
            step_records.append(result)
            if pause:
                sleep(pause)
        mismatches = sorted({item for result in step_records for item in result["mismatches"]})
        records.append({
            "case_id": case_id,
            "category": case["category"],
            "status": "pass" if not mismatches else "fail",
            "failure_layer": "runtime_or_expectation" if mismatches else None,
            "mismatches": mismatches,
            "actual": step_records[-1]["actual"],
            "evidence_codes": sorted({code for result in step_records for code in result["evidence_codes"]}),
            "latency_ms": sum(int(result["latency_ms"]) for result in step_records),
            "sql_calls_used": sum(int(result["sql_calls_used"]) for result in step_records),
            "termination_reason": step_records[-1]["termination_reason"],
            "chart_component_emitted": any(result["chart_component_emitted"] for result in step_records),
            "independent_check_id": case.get("expected", {}).get("check_id"),
            "manual_review_status": "pending",
            "step_count": len(step_records),
            "steps": step_records,
        })
        print(f"[{index}/{len(cases)}] {case_id}: {'PASS' if not mismatches else 'FAIL ' + ','.join(mismatches)}", flush=True)
    report = {
        "suite_id": "product-release-dev-v1",
        "suite_version": "1.0",
        "mode": "live_siliconflow_sse",
        "case_count": len(records),
        "passed_count": sum(record["status"] == "pass" for record in records),
        "failed_count": sum(record["status"] == "fail" for record in records),
        "category_counts": dict(Counter(record["category"] for record in records)),
        "failure_counts": dict(Counter(item for record in records for item in record["mismatches"])),
        "records": records,
        "raw_artifact_root": str(raw_root),
        "report_privacy": "Safe aggregate report: no question, assistant text, SQL, rows, cookie or credential.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--base-url", default="http://127.0.0.1:32013")
    parser.add_argument("--timeout-seconds", type=int, default=240)
    parser.add_argument("--pause-seconds", type=float, default=1.0)
    parser.add_argument("--case-id", action="append", dest="case_ids")
    args = parser.parse_args()
    report = run_suite(
        cases_path=args.cases,
        output=args.output,
        raw_root=args.raw_root,
        base_url=args.base_url,
        timeout=args.timeout_seconds,
        pause=max(0.0, args.pause_seconds),
        selected=set(args.case_ids) if args.case_ids else None,
    )
    print(json.dumps({key: report[key] for key in ("case_count", "passed_count", "failed_count", "failure_counts")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
