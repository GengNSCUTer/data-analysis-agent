#!/usr/bin/env python3
"""Run the live Phase-3 semantic-context and result-artifact verification.

The safe report stores only IDs, hashes and structural runtime evidence. Raw
SSE streams and any service diagnostics remain in the repository-external raw
root because they may contain user prompts, assistant prose or data results.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from time import perf_counter
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request
from uuid import uuid4

import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.postgres_runner import PostgresConnectionSettings
from data_analysis_agent.working_memory import WorkingMemory
from scripts.run_product_capability_online import (
    DemoSseClient,
    _audit_evidence,
    _safe_event_summary,
    _safe_runtime,
)


DEFAULT_ROOT = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/"
    "product-capability-baseline-v1/phase3-context-artifacts"
)


def _sha(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _artifact_from_conversation(client: DemoSseClient, conversation_id: str) -> dict[str, Any] | None:
    request = Request(
        f"{client.base_url}/api/project/conversations/{conversation_id}",
        headers={"Accept": "application/json"},
    )
    try:
        with client.opener.open(request, timeout=client.timeout_seconds) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, json.JSONDecodeError):
        return None
    artifact = value.get("trusted_result_artifact") if isinstance(value, dict) else None
    return artifact if isinstance(artifact, dict) else None


def _manifest_status(
    client: DemoSseClient, conversation_id: str, artifact_id: str
) -> tuple[int | None, dict[str, Any] | None]:
    request = Request(
        f"{client.base_url}/api/project/conversations/{conversation_id}/artifacts/"
        f"{artifact_id}/manifest",
        headers={"Accept": "application/json"},
    )
    try:
        with client.opener.open(request, timeout=client.timeout_seconds) as response:
            value = json.loads(response.read().decode("utf-8"))
            return response.status, value if isinstance(value, dict) else None
    except HTTPError as exc:
        return exc.code, None
    except (URLError, OSError, json.JSONDecodeError):
        return None, None


def _summary_persistence_projection(
    *, conversation_id: str, settings: PostgresConnectionSettings
) -> dict[str, Any] | None:
    """Read only summary metadata, never the owner-scoped summary prose."""
    connection = psycopg2.connect(
        host=settings.host,
        port=settings.port,
        database=settings.database,
        user=settings.writer_user,
    )
    try:
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(
                "SELECT working_memory FROM app.conversations WHERE conversation_id = %s",
                (conversation_id,),
            )
            row = cursor.fetchone()
    finally:
        connection.close()
    memory = row.get("working_memory") if row else None
    if not isinstance(memory, dict):
        return None
    summary = memory.get("__history_summary")
    if not isinstance(summary, dict):
        return {"present": False}
    sql_state = WorkingMemory.from_mapping(memory).as_dict()
    return {
        "present": True,
        "version": summary.get("version"),
        "summary_status": summary.get("summary_status"),
        "source_sha256_present": isinstance(summary.get("source_sha256"), str)
        and len(summary["source_sha256"]) == 64,
        "summary_text_persisted": isinstance(summary.get("text"), str)
        and bool(summary["text"]),
        # SQL state is reconstructed through the typed parser, which discards
        # the private persistence key and all summary prose.
        "sql_state_contains_summary_key": "__history_summary" in sql_state,
        "sql_state_keys": sorted(sql_state),
    }


def _run_turn(
    *,
    client: DemoSseClient,
    settings: PostgresConnectionSettings,
    conversation_id: str,
    case_id: str,
    question: str,
    raw_root: Path,
) -> dict[str, Any]:
    request_id = f"phase3-{case_id}-{uuid4().hex[:12]}"
    started = perf_counter()
    meta, events, error = client.stream(
        case_id=case_id,
        question=question,
        conversation_id=conversation_id,
        request_id=request_id,
    )
    raw_path = raw_root / f"{case_id}-{request_id}.json"
    raw_path.write_text(
        json.dumps({"client": meta, "events": events}, ensure_ascii=False),
        encoding="utf-8",
    )
    os.chmod(raw_path, 0o600)
    runtime = _safe_runtime(_audit_evidence(request_id, settings))
    runtime.update(_safe_event_summary(events))
    return {
        "request_id": request_id,
        "runtime": runtime,
        "client_error": error,
        "raw_sha256": sha256(raw_path.read_bytes()).hexdigest(),
        "latency_ms": round((perf_counter() - started) * 1000),
    }


def run_live_verification(
    *, base_url: str, raw_root: Path, report_path: Path, timeout_seconds: int
) -> dict[str, Any]:
    raw_root.mkdir(parents=True, exist_ok=True)
    settings = PostgresConnectionSettings.from_environment()
    client = DemoSseClient(base_url, "analyst", timeout_seconds)
    conversation_id = f"phase3-context-{uuid4().hex[:12]}"

    # This server-derived chart flow should write the full validated CSV first,
    # then add Plotly JSON to the same artifact manifest.
    first = _run_turn(
        client=client,
        settings=settings,
        conversation_id=conversation_id,
        case_id="validated-chart",
        question="按州统计 GMV 前十名并生成柱状图",
        raw_root=raw_root,
    )
    artifact = _artifact_from_conversation(client, conversation_id)
    artifact_id = artifact.get("artifact_id") if isinstance(artifact, dict) else None
    manifest_status, manifest = (
        _manifest_status(client, conversation_id, artifact_id)
        if isinstance(artifact_id, str)
        else (None, None)
    )

    # Catalog-definition turns are stored as normal conversation turns. Their
    # text is intentionally long enough to force an omitted history range when
    # the final SQL turn enters the isolated low-budget verification service.
    padding = "本次讨论只用于产品复盘背景，不应改变后续 SQL 的指标、时间、维度或权限。"
    history_turns = []
    for index in range(4):
        history_turns.append(
            _run_turn(
                client=client,
                settings=settings,
                conversation_id=conversation_id,
                case_id=f"history-{index + 1}",
                question="GMV 的统计口径是什么？" + padding * 28,
                raw_root=raw_root,
            )
        )
    final = _run_turn(
        client=client,
        settings=settings,
        conversation_id=conversation_id,
        case_id="summary-trigger",
        question="2017 年按月统计有效订单数",
        raw_root=raw_root,
    )
    budget = final["runtime"].get("context_budget") or {}
    persistence = _summary_persistence_projection(
        conversation_id=conversation_id, settings=settings
    )

    # New signed session gets a different demo identity and must not resolve
    # the analyst-owned conversation/artifact even when it knows both IDs.
    admin = DemoSseClient(base_url, "admin", timeout_seconds)
    cross_status, _ = (
        _manifest_status(admin, conversation_id, artifact_id)
        if isinstance(artifact_id, str)
        else (None, None)
    )

    manifest_projection = None
    if isinstance(manifest, dict):
        manifest_projection = {
            key: manifest.get(key)
            for key in (
                "version",
                "artifact_id",
                "workspace_id",
                "row_count",
                "csv_sha256",
                "csv_bytes",
                "csv_ref",
                "plotly_sha256",
                "plotly_bytes",
                "plotly_ref",
            )
        }
    report = {
        "phase": "phase3_semantic_context_artifacts",
        "mode": "live_sse_with_isolated_low_history_budget",
        "conversation_id_sha256": _sha(conversation_id),
        "checks": {
            "validated_csv_plotly_restore": {
                "status": "pass"
                if manifest_status == 200
                and isinstance(manifest, dict)
                and bool(manifest.get("csv_sha256"))
                and bool(manifest.get("plotly_sha256"))
                else "fail",
                "manifest_status": manifest_status,
                "manifest": manifest_projection,
            },
            "long_conversation_semantic_summary": {
                "status": "pass"
                if budget.get("summary_inserted")
                and (budget.get("summary") or {}).get("summary_status")
                == "semantic_generated"
                and final["runtime"].get("termination_reason") == "completed"
                and int(final["runtime"].get("allowed_audit_count") or 0) > 0
                else "fail",
                "context_budget": budget,
                "persistence": persistence,
            },
            "summary_not_sql_authority": {
                "status": "pass"
                if isinstance(final["runtime"].get("working_memory"), dict)
                and "__history_summary" not in final["runtime"]["working_memory"]
                and isinstance(persistence, dict)
                and not persistence.get("sql_state_contains_summary_key")
                else "fail",
                "route_state": final["runtime"].get("route_state"),
                "working_memory_keys": sorted(
                    (final["runtime"].get("working_memory") or {}).keys()
                ),
                "persistence": persistence,
            },
            "cross_user_workspace_isolation": {
                "status": "pass" if cross_status == 404 else "fail",
                "cross_user_manifest_status": cross_status,
            },
            "summary_failure_fallback": {
                "status": "covered_by_deterministic_fault_injection",
                "evidence": "tests/test_context_builder.py::test_summary_failure_falls_back_to_provenance_boundary",
            },
            "summary_digest_reuse": {
                "status": "covered_by_deterministic_fault_injection",
                "evidence": "tests/test_context_builder.py::test_context_filter_uses_semantic_summary_and_reuses_by_source_digest",
            },
            "artifact_checksum_tamper": {
                "status": "covered_by_deterministic_fault_injection",
                "evidence": "tests/test_result_artifact_store.py::test_result_artifact_store_detects_payload_tampering",
            },
        },
        "turns": {
            "validated_chart": {
                "request_id_sha256": _sha(first["request_id"]),
                "runtime": first["runtime"],
                "raw_sha256": first["raw_sha256"],
                "latency_ms": first["latency_ms"],
            },
            "history_turn_count": len(history_turns),
            "summary_trigger": {
                "request_id_sha256": _sha(final["request_id"]),
                "runtime": final["runtime"],
                "raw_sha256": final["raw_sha256"],
                "latency_ms": final["latency_ms"],
            },
        },
        "privacy": (
            "Safe report excludes prompts, assistant content, SQL, result rows, "
            "cookies and semantic-summary text. Raw SSE is external and mode 0600."
        ),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:32012")
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_ROOT / "raw")
    parser.add_argument("--output", type=Path, default=DEFAULT_ROOT / "safe-report.json")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    report = run_live_verification(
        base_url=args.base_url,
        raw_root=args.raw_root,
        report_path=args.output,
        timeout_seconds=args.timeout_seconds,
    )
    failed = [name for name, check in report["checks"].items() if check["status"] == "fail"]
    print(f"Phase 3 checks: {len(report['checks']) - len(failed)}/{len(report['checks'])} complete; report={args.output}")
    if failed:
        print("Failed: " + ", ".join(failed))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
