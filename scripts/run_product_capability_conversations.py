#!/usr/bin/env python3
"""Run Phase-C conversation scripts through the trusted demo SSE boundary.

The raw event streams are written outside the repository. The JSON report only
contains hashed conversation IDs and bounded structural evidence: route state,
database/tool usage, trusted-result-summary presence, WorkingMemory projection,
and ContextBudget truncation. It never stores user text, SQL, result rows, or
assistant completions.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from time import perf_counter, sleep
from typing import Any
from uuid import uuid4

import yaml

from data_analysis_agent.postgres_runner import PostgresConnectionSettings
from data_analysis_agent.product_capability_contract import (
    SUITE_ID,
    validate_product_capability_suite,
    validate_safe_evidence,
)
from scripts.run_product_capability_online import (
    DemoSseClient,
    _audit_evidence,
    _safe_event_summary,
    _safe_runtime,
    write_external_failure_diagnostic,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASE_FILE = ROOT / "evals/cases/product_capability_v1.yaml"
DEFAULT_RAW_ROOT = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/product-capability-baseline-v1/phase-c"
)


def _hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _expected_projection(expected: dict[str, Any]) -> dict[str, Any]:
    memory = expected.get("memory")
    projection: dict[str, Any] = {
        "terminal_state": expected.get("terminal_state"),
        "requires_database": expected.get("requires_database"),
        "intent": expected.get("intent"),
        "evidence_mode": expected.get("evidence_mode"),
    }
    if isinstance(memory, dict):
        projection["memory"] = memory
    for key in (
        "trusted_result_summary",
        "tool_free",
        "context_truncated_allowed",
    ):
        if key in expected:
            projection[key] = expected[key]
    return projection


def _actual_projection(runtime: dict[str, Any]) -> dict[str, Any]:
    return {
        "terminal_state": runtime.get("route_state"),
        "termination_reason": runtime.get("termination_reason"),
        "requires_database": runtime.get("route_requires_database"),
        "intent": runtime.get("route_intent"),
        "evidence_mode": runtime.get("route_evidence_mode"),
        "memory": runtime.get("working_memory"),
        "trusted_result_summary": bool(
            (runtime.get("working_memory") or {}).get(
                "previous_result_summary_present"
            )
        ),
        "tool_free": int(runtime.get("sql_calls_used") or 0) == 0,
        "context_truncated": bool(runtime.get("context_truncated")),
        "context_budget": runtime.get("context_budget"),
    }


def _compare_step(expected: dict[str, Any], runtime: dict[str, Any]) -> tuple[str, list[str]]:
    actual = _actual_projection(runtime)
    mismatches: list[str] = []
    if expected.get("terminal_state") != actual["terminal_state"]:
        # Clarification routes terminate with the same state-specific route
        # metadata, while completed SQL turns terminate as completed.
        mismatches.append("terminal_state")
    for key in ("requires_database", "intent", "evidence_mode"):
        if key in expected and expected[key] != actual[key]:
            mismatches.append(key)
    if expected.get("requires_database") and runtime.get("termination_reason") != "completed":
        mismatches.append("execution_not_completed")
    if "trusted_result_summary" in expected and expected["trusted_result_summary"] != actual["trusted_result_summary"]:
        mismatches.append("trusted_result_summary")
    if "tool_free" in expected and expected["tool_free"] != actual["tool_free"]:
        mismatches.append("tool_free")
    expected_memory = expected.get("memory")
    actual_memory = actual.get("memory")
    if isinstance(expected_memory, dict):
        if not isinstance(actual_memory, dict):
            mismatches.append("memory")
        else:
            for key, value in expected_memory.items():
                actual_value = actual_memory.get(key)
                if key == "time_range" and isinstance(value, list):
                    actual_value = [
                        actual_value.get("start"),
                        actual_value.get("end"),
                    ] if isinstance(actual_value, dict) else None
                if actual_value != value:
                    mismatches.append(f"memory.{key}")
    if expected.get("context_truncated_allowed") is False and actual["context_truncated"]:
        mismatches.append("unexpected_context_truncation")
    return ("pass" if not mismatches else "fail", mismatches)


def _select_scripts(
    scripts: list[dict[str, Any]], case_ids: tuple[str, ...] | None
) -> list[dict[str, Any]]:
    """Select an explicit regression subset without changing the frozen suite."""
    if not case_ids:
        return scripts
    requested = set(case_ids)
    known = {str(script["case_id"]) for script in scripts}
    unknown = requested - known
    if unknown:
        raise ValueError(f"unknown Phase-C case IDs: {', '.join(sorted(unknown))}")
    return [script for script in scripts if script["case_id"] in requested]


def run_scripts(
    *,
    cases_path: Path,
    output: Path,
    raw_root: Path,
    base_url: str,
    role: str,
    timeout: int,
    pause: float,
    case_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    raw = yaml.safe_load(cases_path.read_text(encoding="utf-8")) or {}
    validate_product_capability_suite(raw, require_complete=False)
    scripts = raw.get("conversation_scripts") or []
    if len(scripts) != 10:
        raise ValueError("Phase C requires exactly 10 conversation_scripts")
    selected_scripts = _select_scripts(scripts, case_ids)

    settings = PostgresConnectionSettings.from_environment()
    raw_root.mkdir(parents=True, exist_ok=True)
    diagnostic_root = raw_root / "diagnostics"
    records: list[dict[str, Any]] = []
    for script_index, script in enumerate(selected_scripts, 1):
        conversation_id = f"pcv1-c-{script['case_id']}-{uuid4().hex[:12]}"
        client = DemoSseClient(base_url, role, timeout)
        step_records: list[dict[str, Any]] = []
        for step_index, step in enumerate(script["steps"], 1):
            if step.get("new_conversation_before"):
                conversation_id = f"pcv1-c-{script['case_id']}-new-{uuid4().hex[:12]}"
            if step.get("reload_client_before"):
                client = DemoSseClient(base_url, role, timeout)
            request_id = f"pcv1-c-{script['case_id']}-{step['step_id']}-{uuid4().hex[:12]}"
            started = perf_counter()
            meta, events, error = client.stream(
                case_id=f"{script['case_id']}-{step['step_id']}",
                question=step["message"],
                conversation_id=conversation_id,
                request_id=request_id,
            )
            runtime = _safe_runtime(_audit_evidence(meta["request_id"], settings))
            runtime.update(_safe_event_summary(events))
            runtime["client_error"] = error
            status, mismatches = _compare_step(step["expected"], runtime)
            raw_path = raw_root / f"{script['case_id']}-{step['step_id']}.json"
            raw_path.write_text(
                json.dumps({"client": meta, "events": events}, ensure_ascii=False),
                encoding="utf-8",
            )
            diagnostic_sha256 = None
            if status == "fail":
                diagnostic_sha256 = write_external_failure_diagnostic(
                    request_id=meta["request_id"],
                    settings=settings,
                    output=diagnostic_root
                    / f"{script['case_id']}-{step['step_id']}.json",
                )
            step_records.append(
                {
                    "step_id": step["step_id"],
                    "conversation_id_sha256": _hash(meta["conversation_id"]),
                    "request_id_sha256": _hash(meta["request_id"]),
                    "status": status,
                    "mismatches": mismatches,
                    "runtime": runtime,
                    "artifact_sha256": sha256(raw_path.read_bytes()).hexdigest(),
                    # The digest proves that a restricted diagnostic exists
                    # without disclosing SQL/error text in the safe report.
                    "diagnostic_artifact_sha256": diagnostic_sha256,
                    "latency_ms": round((perf_counter() - started) * 1000),
                }
            )
            if pause:
                sleep(pause)
        all_passed = all(step["status"] == "pass" for step in step_records)
        records.append(
            {
                "case_id": script["case_id"],
                "status": "pass" if all_passed else "fail",
                "failure_layer": None if all_passed else "conversation_state",
                "evidence_codes": [
                    "all_steps_matched" if all_passed else "state_transition_mismatch",
                    "working_memory_projected",
                    "context_budget_projected",
                ],
                "artifact_sha256": sha256(
                    "".join(step["artifact_sha256"] for step in step_records).encode("ascii")
                ).hexdigest(),
                "latency_ms": sum(int(step["latency_ms"]) for step in step_records),
                "manual_review_status": "pending",
                "runtime": {
                    "category": script["category"],
                    "step_count": len(step_records),
                    "passed_steps": sum(step["status"] == "pass" for step in step_records),
                    "steps": [
                        {
                            "step_id": step["step_id"],
                            "status": step["status"],
                            "mismatches": step["mismatches"],
                            "actual": _actual_projection(step["runtime"]),
                        }
                        for step in step_records
                    ],
                },
            }
        )
        print(
            f"[{script_index}/{len(selected_scripts)}] {script['case_id']}: {records[-1]['status']}",
            flush=True,
        )
    report = {
        "suite_id": SUITE_ID,
        "suite_version": raw["suite_version"],
        "phase": "phase_c",
        "mode": "live_siliconflow_sse",
        "script_count": len(records),
        "selected_case_ids": [item["case_id"] for item in selected_scripts],
        "records": records,
        "raw_artifact_root": str(raw_root),
        "report_privacy": "No question, assistant text, SQL text, result rows, cookie or credential is stored in this report.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASE_FILE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--base-url", default="http://127.0.0.1:32010")
    parser.add_argument("--role", choices=("analyst", "admin"), default="analyst")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--pause-seconds", type=float, default=1.0)
    parser.add_argument(
        "--case-id",
        action="append",
        dest="case_ids",
        help="Optional frozen Phase-C case ID; repeat for a targeted regression subset.",
    )
    args = parser.parse_args()
    run_scripts(
        cases_path=args.cases,
        output=args.output,
        raw_root=args.raw_root,
        base_url=args.base_url,
        role=args.role,
        timeout=args.timeout_seconds,
        pause=max(0.0, args.pause_seconds),
        case_ids=tuple(args.case_ids) if args.case_ids else None,
    )


if __name__ == "__main__":
    main()
