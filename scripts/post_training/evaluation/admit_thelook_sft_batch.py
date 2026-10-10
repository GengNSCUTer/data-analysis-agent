#!/usr/bin/env python3
"""Admit TheLook SFT Gold SQL under the real reader-role contract."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence
import uuid

import pandas as pd
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from data_analysis_agent.semantic_catalog import CatalogLoader  # noqa: E402
from data_analysis_agent.sql_policy import PolicyViolation, SqlPolicy  # noqa: E402
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE  # noqa: E402
from data_analysis_agent.thelook_sft_coverage import load_thelook_sft_contract  # noqa: E402
from data_analysis_agent.thelook_v3_release import (  # noqa: E402
    build_thelook_v3_result_contract,
    validate_thelook_result,
)


ADMISSION_VERSION = "thelook-sql-only-sft-admission-v1"
DEFAULT_GOLD = Path("/disk2/gengnan/data-analysis-agent-data/evals/thelook-sql-only-sft-v1-r4/gold")


class TheLookSftAdmissionError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def execute_as_reader(sql: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    connection = psycopg2.connect(host="/tmp", port=35434, database="thelook_analytics", user="postgres")
    try:
        connection.set_session(readonly=True, autocommit=False)
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(f"SET LOCAL ROLE {THELOOK_V2_WORKSPACE.reader_role}")
            cursor.execute("SET LOCAL statement_timeout = 5000")
            cursor.execute(sql)
            rows = [dict(row) for row in cursor.fetchmany(1_000)]
            columns = tuple(description.name for description in cursor.description or ())
        return pd.DataFrame(rows, columns=columns), {
            "reader_role": THELOOK_V2_WORKSPACE.reader_role,
            "database": "thelook_analytics",
            "schema": THELOOK_V2_WORKSPACE.analytics_schema,
            "returned_rows_before_execution_cap": len(rows),
            "returned_columns": list(columns),
            "statement_timeout_ms": 5000,
        }
    finally:
        connection.close()


def load_gold_rows(gold_dir: Path, split: str) -> list[dict[str, Any]]:
    path = gold_dir / f"{split}_gold.jsonl"
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TheLookSftAdmissionError(f"invalid Gold JSON at {path}:{line_no}") from exc
        if row.get("split") != split:
            raise TheLookSftAdmissionError(f"Gold split mismatch at {path}:{line_no}")
        rows.append(row)
    return rows


def admit_row(
    row: Mapping[str, Any],
    *,
    catalog: Any,
    policy: SqlPolicy,
    execute: Callable[[str], tuple[pd.DataFrame, dict[str, Any]]],
) -> dict[str, Any]:
    result = dict(row)
    result["admission_status"] = "rejected"
    result["database_executed"] = False
    try:
        decision = policy.evaluate(str(row["gold_sql"]), role="analyst")
        frame, execution = execute(decision.final_sql)
        spec = row["query_spec"]
        from data_analysis_agent.thelook_v2_queryspec import TheLookV2QuerySpec, validate_thelook_v2_query_spec
        validated_spec = validate_thelook_v2_query_spec(TheLookV2QuerySpec.from_mapping(spec), catalog)
        contract = build_thelook_v3_result_contract(validated_spec, catalog)
        validation = validate_thelook_result(
            frame,
            contract,
            time_bucket_grain=validated_spec.time.grain,
            max_rows=int(load_thelook_sft_contract()["execution_contract"]["analyst_execution_row_cap"]),
            display_row_budget=int(load_thelook_sft_contract()["execution_contract"]["display_preview_row_budget"]),
        )
        result.update({
            "admission_status": "admitted" if validation.safe_to_answer else "rejected",
            "database_executed": True,
            "runtime_policy_status": decision.status,
            "runtime_policy_final_sql_sha256": sha256_text(decision.final_sql),
            "runtime_policy_row_limit": decision.row_limit,
            "runtime_policy_limit_applied": decision.policy_limit_applied,
            "execution_evidence": execution,
            "result_contract": contract.as_evidence(),
            "result_validation": validation.as_dict(),
        })
        if not validation.safe_to_answer:
            result["failure_type"] = "ResultValidation"
    except (OSError, ValueError, PolicyViolation, psycopg2.Error) as exc:
        result["failure_type"] = type(exc).__name__
        result["failure_reason"] = str(exc)[:500]
    return result


def _select_pilot(rows: list[dict[str, Any]], per_family: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["scenario_family_id"])].append(row)
    selected: list[dict[str, Any]] = []
    for family in sorted(grouped):
        selected.extend(sorted(grouped[family], key=lambda row: row["seed_id"])[:per_family])
    return selected


def _external_new_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError("admission output must stay outside the Git worktree")
    if resolved.exists():
        raise FileExistsError(resolved)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def admit(
    gold_dir: Path,
    output_dir: Path,
    *,
    per_family: int | None = None,
    generated_at: str | None = None,
    execute: Callable[[str], tuple[pd.DataFrame, dict[str, Any]]] = execute_as_reader,
) -> dict[str, Any]:
    output = _external_new_dir(output_dir)
    contract = load_thelook_sft_contract()
    catalog = CatalogLoader(THELOOK_V2_WORKSPACE).load()
    execution = contract["execution_contract"]
    policy = SqlPolicy(
        analyst_limit=int(execution["analyst_execution_row_cap"]),
        admin_limit=int(execution["analyst_execution_row_cap"]),
        workspace=THELOOK_V2_WORKSPACE,
    )
    rows = load_gold_rows(gold_dir, "train") + load_gold_rows(gold_dir, "validation")
    selected = _select_pilot(rows, per_family) if per_family else rows
    records = [admit_row(row, catalog=catalog, policy=policy, execute=execute) for row in selected]
    admitted = [row for row in records if row["admission_status"] == "admitted"]
    rejected = [row for row in records if row["admission_status"] != "admitted"]
    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        records_path = staging / "admission_records.jsonl"
        records_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in records), encoding="utf-8")
        exclusions_path = staging / "exclusions.jsonl"
        exclusions_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rejected), encoding="utf-8")
        family_selected = Counter(str(row["scenario_family_id"]) for row in records)
        family_admitted = Counter(str(row["scenario_family_id"]) for row in admitted)
        manifest = {
            "admission_version": ADMISSION_VERSION,
            "generated_at": generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "source": {"gold_dir": str(gold_dir.resolve()), "train_gold_sha256": sha256_file(gold_dir / "train_gold.jsonl"), "validation_gold_sha256": sha256_file(gold_dir / "validation_gold.jsonl"), "pilot_per_family": per_family},
            "counts": {"selected": len(records), "admitted": len(admitted), "rejected": len(rejected), "selected_by_family": dict(sorted(family_selected.items())), "admitted_by_family": dict(sorted(family_admitted.items())), "failure_types": dict(sorted(Counter(str(row.get("failure_type", "unknown")) for row in rejected).items()))},
            "output": {"admission_records_jsonl": {"rows": len(records), "sha256": sha256_file(records_path)}, "exclusions_jsonl": {"rows": len(rejected), "sha256": sha256_file(exclusions_path)}},
            "checks": {"all_selected_rows_admitted": not rejected, "gold_sql_hashes_recorded": all(row.get("gold_sql_sha256") for row in records), "reader_role_evidence_recorded": all(row.get("execution_evidence", {}).get("reader_role") == THELOOK_V2_WORKSPACE.reader_role for row in admitted), "result_contract_and_validator_recorded": all(row.get("result_contract") and row.get("result_validation") for row in admitted), "database_executed": True},
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        staging.rename(output)
    except Exception:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold-dir", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--per-family", type=int, default=None, help="pilot mode: select N rows per family")
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    print(json.dumps(admit(args.gold_dir, args.output_dir, per_family=args.per_family, generated_at=args.generated_at), ensure_ascii=False, sort_keys=True, indent=2))
