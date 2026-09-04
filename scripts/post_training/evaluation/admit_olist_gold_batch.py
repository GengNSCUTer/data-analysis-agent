#!/usr/bin/env python3
"""Execute a small materialized Olist Gold SQL batch through trust and review gates.

This is an offline Gold-admission gate, not a candidate generator or SFT data
builder. It consumes no natural-language questions and never reads the
protected holdout. Detailed SQL, result summaries, and provider responses are
written only to a new external output directory.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Mapping
import uuid

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.olist_queryspec import (  # noqa: E402
    METRIC_SQL_REGISTRY,
    QuerySpec,
    WorkspacePin,
)
from data_analysis_agent.postgres_runner import (  # noqa: E402
    PostgresConnectionSettings,
    SecurePostgresRunner,
)
from data_analysis_agent.result_validator import ResultValidationError  # noqa: E402
from data_analysis_agent.sql_policy import PolicyViolation  # noqa: E402
from data_analysis_agent.sql_repair import SafeSqlExecutionError  # noqa: E402
from vanna.capabilities.sql_runner import RunSqlToolArgs  # noqa: E402
from vanna.core.tool import ToolContext  # noqa: E402
from vanna.core.user import User  # noqa: E402
from vanna.integrations.local.agent_memory import DemoAgentMemory  # noqa: E402


ADMISSION_VERSION = "olist-small-gold-admission-v1"
REVIEW_MODEL = "deepseek-ai/DeepSeek-V4-Flash"
MAX_BATCH_ROWS = 6
_ALLOWED_VERDICTS = frozenset({"pass", "needs_human_review"})
_RATE_METRICS = frozenset(
    {"positive_review_rate", "on_time_delivery_rate", "cancellation_rate"}
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--materialization-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--seed-id",
        action="append",
        default=None,
        help=(
            "Select one Gold seed from a larger verified materialization. Repeat at most six "
            "times; omitting this retains the historical whole-directory small-batch contract."
        ),
    )
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_external_existing_dir(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError(f"{label} must stay outside the Git worktree")
    if not resolved.is_dir():
        raise FileNotFoundError(resolved)
    return resolved


def _require_external_new_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError("admission output must stay outside the Git worktree")
    if resolved.exists():
        raise FileExistsError(f"admission output already exists: {resolved}")
    return resolved


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON at {path}:{line_number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"row at {path}:{line_number} must be an object")
        rows.append(row)
    return rows


def _validate_selection(seed_ids: list[str] | None, available_seed_ids: set[str]) -> set[str] | None:
    """Validate an explicit small-batch selection without trusting caller ordering."""
    if seed_ids is None:
        return None
    if not seed_ids or len(seed_ids) > MAX_BATCH_ROWS:
        raise ValueError(f"admission selection must contain 1-{MAX_BATCH_ROWS} seed IDs")
    if any(not isinstance(seed_id, str) or not seed_id.strip() for seed_id in seed_ids):
        raise ValueError("admission selection seed IDs must be non-empty strings")
    normalized = [seed_id.strip() for seed_id in seed_ids]
    if len(set(normalized)) != len(normalized):
        raise ValueError("admission selection seed IDs must be unique")
    unknown = sorted(set(normalized) - available_seed_ids)
    if unknown:
        raise ValueError(f"admission selection contains unknown seed IDs: {unknown}")
    return set(normalized)


def load_materialized_gold_rows(
    directory: Path,
    *,
    seed_ids: list[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate all source artifacts, then return a requested bounded subset."""
    manifest_path = directory / "materialization_manifest.json"
    gold_path = directory / "gold_sql.jsonl"
    query_specs_path = directory / "query_specs.jsonl"
    if not manifest_path.is_file() or not gold_path.is_file() or not query_specs_path.is_file():
        raise FileNotFoundError(
            "materialization manifest, query_specs.jsonl, and gold_sql.jsonl are required"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("materialization manifest must be an object")
    if manifest.get("checks", {}).get("status") != "pass":
        raise ValueError("materialization manifest does not report a passing structural audit")
    if manifest.get("workspace") != WorkspacePin.current().as_dict():
        raise ValueError("materialization workspace does not match the current pin")
    outputs = manifest.get("outputs", {})
    if outputs.get("gold_sql_jsonl", {}).get("sha256") != sha256_file(gold_path):
        raise ValueError("materialization gold SQL hash does not match its manifest")
    if outputs.get("query_specs_jsonl", {}).get("sha256") != sha256_file(query_specs_path):
        raise ValueError("materialization QuerySpec hash does not match its manifest")
    source = manifest.get("source", {})
    if not source.get("protected_evidence_sha256") or not source.get("protected_summary_sha256"):
        raise ValueError("materialization is missing protected summary evidence binding")
    query_spec_rows = _read_jsonl(query_specs_path)
    query_specs_by_seed = {str(row.get("seed_id")): row for row in query_spec_rows}
    if len(query_specs_by_seed) != len(query_spec_rows):
        raise ValueError("materialization QuerySpec seed IDs are not unique")
    gold_rows = _read_jsonl(gold_path)
    gold_seed_ids = [str(row.get("seed_id")) for row in gold_rows]
    if len(gold_seed_ids) != len(set(gold_seed_ids)):
        raise ValueError("materialization Gold seed IDs are not unique")
    if set(gold_seed_ids) != set(query_specs_by_seed):
        raise ValueError("materialization Gold and QuerySpec seed ID sets do not match")
    rows: list[dict[str, Any]] = []
    for gold_row in gold_rows:
        seed_id = str(gold_row.get("seed_id"))
        query_row = query_specs_by_seed.get(seed_id)
        if query_row is None:
            raise ValueError("Gold row has no matching QuerySpec record")
        if any(gold_row.get(field) != query_row.get(field) for field in ("split", "family_id", "sql_program_id")):
            raise ValueError("Gold and QuerySpec records disagree on structural identity")
        rows.append({**gold_row, "query_spec": query_row.get("query_spec")})
    if len(query_specs_by_seed) != len(rows):
        raise ValueError("materialization Gold and QuerySpec row counts do not match")
    if len(rows) != outputs.get("gold_sql_jsonl", {}).get("rows"):
        raise ValueError("materialization Gold row count does not match its manifest")
    if len(rows) != outputs.get("query_specs_jsonl", {}).get("rows"):
        raise ValueError("materialization QuerySpec row count does not match its manifest")
    for row in rows:
        artifact = row.get("gold_artifact")
        if not isinstance(artifact, dict) or not isinstance(artifact.get("sql"), str):
            raise ValueError("materialized Gold row is malformed")
        if artifact.get("sql_sha256") != hashlib.sha256(artifact["sql"].encode("utf-8")).hexdigest():
            raise ValueError("materialized Gold SQL hash mismatch")
        spec = QuerySpec.from_mapping(row.get("query_spec", {}))
        if artifact.get("query_spec_id") != spec.query_spec_id:
            raise ValueError("Gold artifact QuerySpec ID does not match its QuerySpec record")
    selected_seed_ids = _validate_selection(seed_ids, set(query_specs_by_seed))
    if selected_seed_ids is not None:
        rows = [row for row in rows if str(row["seed_id"]) in selected_seed_ids]
    if not rows or len(rows) > MAX_BATCH_ROWS:
        raise ValueError(f"admission batch must contain 1-{MAX_BATCH_ROWS} Gold rows")
    return manifest, rows


def metric_constraints(metric_ids: tuple[str, ...]) -> dict[str, dict[str, float | bool]]:
    constraints: dict[str, dict[str, float | bool]] = {}
    for metric_id in metric_ids:
        if metric_id in _RATE_METRICS:
            constraints[metric_id] = {"minimum": 0, "maximum": 1}
        elif metric_id in {"paid_order_count", "item_count"}:
            constraints[metric_id] = {"minimum": 0, "integer_like": True}
        elif metric_id in METRIC_SQL_REGISTRY:
            constraints[metric_id] = {"minimum": 0}
    return constraints


def make_context(spec: QuerySpec, seed_id: str) -> ToolContext:
    metadata: dict[str, Any] = {
        "question": None,
        "required_result_columns": list(spec.required_result_columns),
        "metric_result_columns": list(spec.metric_ids),
        "exact_result_columns": True,
        "metric_value_constraints": metric_constraints(spec.metric_ids),
        "gold_admission_seed_id": seed_id,
    }
    if spec.time.start is not None:
        metadata["requested_start"] = spec.time.start
        metadata["requested_end"] = spec.time.end_exclusive
    if spec.result_shape == "time_series":
        metadata["result_time_column"] = "time"
    return ToolContext(
        user=User(id="olist-gold-admission", group_memberships=["analyst"]),
        conversation_id=f"olist-gold-admission-{seed_id}",
        request_id=f"olist-gold-admission-{seed_id}",
        agent_memory=DemoAgentMemory(),
        metadata=metadata,
    )


def review_prompt(spec: QuerySpec, sql: str, validated_summary: str) -> str:
    metric_notes = {
        metric_id: METRIC_SQL_REGISTRY[metric_id].value_notes
        for metric_id in spec.metric_ids
    }
    payload = {
        "review_task": "offline_gold_sql_semantic_advisory",
        "rules": [
            "Do not propose replacement SQL or execute anything.",
            "Check only whether the supplied SQL appears consistent with the frozen QuerySpec and metric notes.",
            "Treat the deterministic policy/result gates as separate evidence, not as proof of business semantics.",
            "Return JSON only with verdict, issues, and rationale.",
        ],
        "query_spec": spec.as_dict(),
        "metric_notes": metric_notes,
        "canonical_sql": sql,
        "validated_result_summary": validated_summary,
        "response_schema": {
            "verdict": "pass or needs_human_review",
            "issues": ["short concrete issue strings"],
            "rationale": "short evidence-based explanation",
        },
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def parse_review_response(content: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("review model did not return JSON") from exc
    if not isinstance(value, dict) or set(value) != {"verdict", "issues", "rationale"}:
        raise ValueError("review model returned an unsupported schema")
    if value["verdict"] not in _ALLOWED_VERDICTS:
        raise ValueError("review model returned an unsupported verdict")
    if not isinstance(value["issues"], list) or any(not isinstance(item, str) for item in value["issues"]):
        raise ValueError("review model issues must be string list")
    if not isinstance(value["rationale"], str):
        raise ValueError("review model rationale must be a string")
    return value


def review_with_siliconflow(prompt: str) -> dict[str, Any]:
    """Call the configured OpenAI-compatible reviewer; never downgrade errors to pass."""
    from openai import OpenAI

    api_key = os.getenv("SILICONFLOW_API_KEY", "")
    if not api_key:
        return {"verdict": "needs_human_review", "issues": ["review_api_key_missing"], "rationale": ""}
    client = OpenAI(
        api_key=api_key,
        base_url=os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
        timeout=30.0,
    )
    try:
        response = client.chat.completions.create(
            model=os.getenv("SILICONFLOW_MODEL", REVIEW_MODEL),
            temperature=0,
            max_tokens=600,
            messages=[
                {"role": "system", "content": "You are a cautious SQL semantic reviewer. Return strict JSON only."},
                {"role": "user", "content": prompt},
            ],
        )
        content = response.choices[0].message.content or ""
        return parse_review_response(content)
    except Exception as exc:
        return {
            "verdict": "needs_human_review",
            "issues": [f"review_provider_error:{type(exc).__name__}"],
            "rationale": "",
        }


async def admit(
    materialization_dir: Path,
    output_dir: Path,
    *,
    generated_at: str | None = None,
    seed_ids: list[str] | None = None,
) -> dict[str, Any]:
    materialized_path = _require_external_existing_dir(materialization_dir, "materialization directory")
    output_path = _require_external_new_dir(output_dir)
    manifest, gold_rows = load_materialized_gold_rows(materialized_path, seed_ids=seed_ids)
    load_dotenv(ROOT / ".env")
    runner = SecurePostgresRunner(
        settings=PostgresConnectionSettings.from_environment(),
        model_name=os.getenv("SILICONFLOW_MODEL", REVIEW_MODEL),
    )
    generated_at = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    records: list[dict[str, Any]] = []
    for row in gold_rows:
        spec = QuerySpec.from_mapping(row["query_spec"])
        artifact = row["gold_artifact"]
        context = make_context(spec, str(row["seed_id"]))
        status = "rejected"
        record: dict[str, Any] = {
            "seed_id": row["seed_id"],
            "split": row["split"],
            "family_id": row["family_id"],
            "sql_program_id": row["sql_program_id"],
            "query_spec": spec.as_dict(),
            "gold_sql": artifact["sql"],
            "gold_sql_sha256": artifact["sql_sha256"],
        }
        try:
            await runner.run_sql(RunSqlToolArgs(sql=artifact["sql"]), context)
            validation = context.metadata["result_validation"]
            summary = context.metadata["validated_result_summary"]
            review = review_with_siliconflow(review_prompt(spec, artifact["sql"], summary))
            status = "admitted" if review["verdict"] == "pass" else "needs_human_review"
            record.update(
                {
                    "policy_status": "allowed",
                    "result_validation": validation,
                    "validated_result_summary": summary,
                    "llm_semantic_review": review,
                }
            )
        except (PolicyViolation, ResultValidationError, SafeSqlExecutionError) as exc:
            record.update({"policy_status": "rejected", "failure_type": type(exc).__name__})
        record["admission_status"] = status
        records.append(record)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = output_path.parent / f".{output_path.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        details_path = staging / "admission_records.jsonl"
        with details_path.open("x", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        aggregate = {
            "admission_version": ADMISSION_VERSION,
            "generated_at": generated_at,
            "workspace": WorkspacePin.current().as_dict(),
            "materialization_manifest_sha256": sha256_file(
                materialized_path / "materialization_manifest.json"
            ),
            "selection": {
                "source_materialized_rows": manifest["outputs"]["gold_sql_jsonl"]["rows"],
                "requested_seed_ids": list(seed_ids) if seed_ids is not None else None,
                "selected_seed_ids": [str(record["seed_id"]) for record in records],
            },
            "protected_summary_sha256": manifest["source"]["protected_summary_sha256"],
            "protected_evidence_sha256": manifest["source"]["protected_evidence_sha256"],
            "counts": {
                "input_rows": len(records),
                "admitted": sum(record["admission_status"] == "admitted" for record in records),
                "needs_human_review": sum(
                    record["admission_status"] == "needs_human_review" for record in records
                ),
                "rejected": sum(record["admission_status"] == "rejected" for record in records),
            },
            "checks": {
                "protected_holdout_raw_read": False,
                "prompt_or_question_materialized": False,
                "sql_executed_via_reader_role": True,
                "llm_is_advisory_only": True,
            },
            "record_hashes": [
                {
                    "seed_id": record["seed_id"],
                    "gold_sql_sha256": record["gold_sql_sha256"],
                    "admission_status": record["admission_status"],
                }
                for record in records
            ],
        }
        (staging / "admission_aggregate.json").write_text(
            json.dumps(aggregate, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(output_path)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return aggregate


def main() -> int:
    args = parse_args()
    aggregate = asyncio.run(
        admit(
            args.materialization_dir,
            args.output_dir,
            generated_at=args.generated_at,
            seed_ids=args.seed_id,
        )
    )
    print(json.dumps(aggregate, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
