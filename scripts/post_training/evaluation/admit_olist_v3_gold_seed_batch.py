#!/usr/bin/env python3
"""Admit a fixed small Olist v3 family-seed batch through the trusted SQL path.

This is an offline construction gate.  It consumes the committed structural
family fixture, never creates questions or prompts, and writes all detailed
SQL, result summaries, reader-role execution evidence, and LLM advisory output
only to a new directory outside the Git worktree.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Mapping, Sequence
import uuid

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.metric_context import OLIST_V3_WORKSPACE  # noqa: E402
from data_analysis_agent.olist_queryspec import (  # noqa: E402
    QuerySpec,
    WorkspacePin,
    render_gold_sql,
    validate_query_spec,
)
from data_analysis_agent.postgres_runner import (  # noqa: E402
    PostgresConnectionSettings,
    SecurePostgresRunner,
)
from data_analysis_agent.result_validator import ResultValidationError  # noqa: E402
from data_analysis_agent.semantic_catalog import Catalog, CatalogLoader  # noqa: E402
from data_analysis_agent.sql_policy import PolicyViolation, SqlPolicy  # noqa: E402
from data_analysis_agent.sql_repair import SafeSqlExecutionError  # noqa: E402
from scripts.post_training.data.materialize_olist_queryspecs import family_id  # noqa: E402
from scripts.post_training.evaluation.admit_olist_gold_batch import (  # noqa: E402
    REVIEW_MODEL,
    review_prompt,
    review_with_siliconflow,
)
from vanna.capabilities.sql_runner import RunSqlToolArgs  # noqa: E402
from vanna.core.tool import ToolContext  # noqa: E402
from vanna.core.user import User  # noqa: E402
from vanna.integrations.local.agent_memory import DemoAgentMemory  # noqa: E402


ADMISSION_VERSION = "olist-v3-small-gold-admission-v1"
SELECTION_SCHEMA_VERSION = "olist-v3-small-gold-admission-selection-v1"
SEED_SCHEMA_VERSION = "olist-v3-coverage-family-seed-v1"
MAX_BATCH_ROWS = 12
_SEED_FIELDS = frozenset(
    {
        "seed_schema_version",
        "seed_id",
        "split",
        "primary_bucket",
        "family_id",
        "risk_tags",
        "instance_window_policy",
        "query_spec",
    }
)
_SELECTION_FIELDS = frozenset(
    {
        "selection_schema_version",
        "source_seed_fixture_sha256",
        "seed_ids",
        "required_primary_buckets",
        "required_split_counts",
        "required_new_metric_ids",
        "required_risk_tags",
    }
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seeds-jsonl",
        type=Path,
        default=ROOT / "data" / "fixtures" / "olist_v3_coverage_family_seeds_v1.jsonl",
    )
    parser.add_argument(
        "--selection-json",
        type=Path,
        default=ROOT / "data" / "fixtures" / "olist_v3_small_gold_admission_selection_v1.json",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--review-mode",
        choices=("required", "disabled"),
        default="required",
        help="A required review failure becomes needs_human_review; it never becomes pass.",
    )
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON at {path}:{line_number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"seed at {path}:{line_number} must be an object")
        rows.append(row)
    if not rows:
        raise ValueError("v3 seed fixture is empty")
    return rows


def _read_selection(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        selection = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("selection must be valid JSON") from exc
    if not isinstance(selection, dict) or set(selection) != _SELECTION_FIELDS:
        raise ValueError("selection fields do not match the v1 contract")
    if selection["selection_schema_version"] != SELECTION_SCHEMA_VERSION:
        raise ValueError("unsupported v3 admission selection version")
    return selection


def load_validated_seed_fixture(path: Path) -> list[dict[str, Any]]:
    """Validate all 300 structural seeds before choosing the 12-row admission slice."""
    rows = _read_jsonl(path)
    catalog = CatalogLoader(workspace=OLIST_V3_WORKSPACE).load()
    expected_pin = WorkspacePin.current(OLIST_V3_WORKSPACE)
    seed_ids: set[str] = set()
    family_splits: dict[str, str] = {}
    for row in rows:
        if set(row) != _SEED_FIELDS or row.get("seed_schema_version") != SEED_SCHEMA_VERSION:
            raise ValueError("v3 seed fixture schema drifted")
        seed_id = row.get("seed_id")
        if not isinstance(seed_id, str) or not seed_id or seed_id in seed_ids:
            raise ValueError("v3 seed IDs must be unique non-empty strings")
        seed_ids.add(seed_id)
        split = row.get("split")
        if split not in {"train", "validation", "in_domain_test"}:
            raise ValueError("v3 seed has an unsupported split")
        if not isinstance(row.get("risk_tags"), list) or not all(
            isinstance(tag, str) for tag in row["risk_tags"]
        ):
            raise ValueError("v3 seed risk tags must be string lists")
        spec = QuerySpec.from_mapping(row.get("query_spec", {}))
        if spec.workspace != expected_pin:
            raise ValueError("v3 seed workspace pin drifted")
        if validate_query_spec(spec, catalog) != spec:
            raise ValueError("v3 seed QuerySpec failed validation")
        if row.get("family_id") != family_id(spec):
            raise ValueError("v3 seed family ID does not match the QuerySpec")
        previous_split = family_splits.setdefault(str(row["family_id"]), str(split))
        if previous_split != split:
            raise ValueError("v3 seed family crosses splits")
    if len(rows) != 300 or len(family_splits) != len(rows):
        raise ValueError("v3 seed fixture must contain 300 unique families")
    return rows


def select_admission_rows(
    rows: Sequence[dict[str, Any]], selection: Mapping[str, Any], *, fixture_sha256: str
) -> list[dict[str, Any]]:
    """Select an explicit, coverage-complete 12-row batch without random sampling."""
    if selection.get("source_seed_fixture_sha256") != fixture_sha256:
        raise ValueError("admission selection is not bound to this seed fixture hash")
    seed_ids = selection.get("seed_ids")
    if (
        not isinstance(seed_ids, list)
        or len(seed_ids) != MAX_BATCH_ROWS
        or any(not isinstance(seed_id, str) or not seed_id for seed_id in seed_ids)
        or len(set(seed_ids)) != len(seed_ids)
    ):
        raise ValueError("admission selection must contain exactly 12 unique seed IDs")
    by_id = {str(row["seed_id"]): row for row in rows}
    unknown = sorted(set(seed_ids) - set(by_id))
    if unknown:
        raise ValueError(f"admission selection has unknown seed IDs: {unknown}")
    selected = [by_id[seed_id] for seed_id in seed_ids]
    split_counts = Counter(str(row["split"]) for row in selected)
    if split_counts != Counter(selection.get("required_split_counts")):
        raise ValueError("admission selection split coverage drifted")
    buckets = {str(row["primary_bucket"]) for row in selected}
    if buckets != set(selection.get("required_primary_buckets", [])):
        raise ValueError("admission selection primary-bucket coverage drifted")
    metrics = {
        metric_id
        for row in selected
        for metric_id in row["query_spec"]["metric_ids"]
    }
    required_metrics = set(selection.get("required_new_metric_ids", []))
    if not required_metrics <= metrics:
        raise ValueError("admission selection new-metric coverage drifted")
    risk_tags = {tag for row in selected for tag in row["risk_tags"]}
    required_risk_tags = set(selection.get("required_risk_tags", []))
    if not required_risk_tags <= risk_tags:
        raise ValueError("admission selection risk coverage drifted")
    return selected


def make_context(spec: QuerySpec, seed_id: str, catalog: Catalog) -> ToolContext:
    """Build the same QuerySpec-derived result contract used by trusted execution."""
    metadata: dict[str, Any] = {
        "question": None,
        "dataset_version_id": spec.workspace.dataset_version,
        "metric_version": spec.workspace.metric_version,
        # `app.query_audits.run_id` is a foreign key to live Agent runs.
        # This offline gate has no corresponding agent_runs record, so retain
        # the admission identity in request/conversation IDs below rather than
        # inserting a fabricated run_id that would make audit persistence fail.
        "required_result_columns": list(spec.required_result_columns),
        "metric_result_columns": list(spec.metric_ids),
        "exact_result_columns": True,
        "metric_value_constraints": {
            metric_id: dict(catalog.metrics_by_id[metric_id].result_value_constraints)
            for metric_id in spec.metric_ids
        },
        "gold_admission_seed_id": seed_id,
    }
    if spec.time.start is not None:
        metadata["requested_start"] = spec.time.start
        metadata["requested_end"] = spec.time.end_exclusive
    if spec.result_shape == "time_series":
        metadata["result_time_column"] = "time"
        metadata["result_time_grain"] = spec.time.grain
    return ToolContext(
        user=User(id="olist-v3-gold-admission", group_memberships=["analyst"]),
        conversation_id=f"olist-v3-gold-admission-{seed_id}",
        request_id=f"olist-v3-gold-admission-{seed_id}",
        agent_memory=DemoAgentMemory(),
        metadata=metadata,
    )


def _require_external_new_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError("v3 admission output must stay outside the Git worktree")
    if resolved.exists():
        raise FileExistsError(f"v3 admission output already exists: {resolved}")
    return resolved


async def admit(
    seeds_jsonl: Path,
    selection_json: Path,
    output_dir: Path,
    *,
    review_mode: str = "required",
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Run each frozen representative program through render, policy, DB and result gates."""
    if review_mode not in {"required", "disabled"}:
        raise ValueError("unsupported review mode")
    output_path = _require_external_new_dir(output_dir)
    fixture_path = seeds_jsonl.resolve()
    selection_path = selection_json.resolve()
    fixture_sha256 = sha256_file(fixture_path)
    rows = load_validated_seed_fixture(fixture_path)
    selection = _read_selection(selection_path)
    selected_rows = select_admission_rows(rows, selection, fixture_sha256=fixture_sha256)
    catalog = CatalogLoader(workspace=OLIST_V3_WORKSPACE).load()
    load_dotenv(ROOT / ".env")
    policy = SqlPolicy(workspace=OLIST_V3_WORKSPACE)
    runner = SecurePostgresRunner(
        settings=PostgresConnectionSettings.from_environment(),
        model_name=os.getenv("SILICONFLOW_MODEL", REVIEW_MODEL),
        policy=policy,
        workspace=OLIST_V3_WORKSPACE,
    )
    records: list[dict[str, Any]] = []
    for row in selected_rows:
        seed_id = str(row["seed_id"])
        spec = QuerySpec.from_mapping(row["query_spec"])
        artifact = render_gold_sql(spec, catalog)
        if artifact.sql_sha256 != sha256_text(artifact.sql):
            raise ValueError(f"renderer hash drift for {seed_id}")
        if artifact.query_spec_id != spec.query_spec_id:
            raise ValueError(f"renderer QuerySpec identity drift for {seed_id}")
        record: dict[str, Any] = {
            "seed_id": seed_id,
            "split": row["split"],
            "primary_bucket": row["primary_bucket"],
            "family_id": row["family_id"],
            "risk_tags": row["risk_tags"],
            "query_spec": spec.as_dict(),
            "gold_sql": artifact.sql,
            "gold_sql_sha256": artifact.sql_sha256,
            "gold_renderer_version": artifact.renderer_version,
        }
        try:
            decision = policy.evaluate(artifact.sql, role="analyst")
            context = make_context(spec, seed_id, catalog)
            await runner.run_sql(RunSqlToolArgs(sql=artifact.sql), context)
            validation = context.metadata["result_validation"]
            summary = context.metadata["validated_result_summary"]
            review = (
                review_with_siliconflow(review_prompt(spec, artifact.sql, summary))
                if review_mode == "required"
                else {"verdict": "pass", "issues": [], "rationale": "review disabled by explicit mode"}
            )
            record.update(
                {
                    "policy_status": decision.status,
                    "policy_final_sql_sha256": sha256_text(decision.final_sql),
                    "policy_limit_applied": decision.policy_limit_applied,
                    "reader_role": OLIST_V3_WORKSPACE.reader_role,
                    "result_validation": validation,
                    "validated_result_summary": summary,
                    "validated_result_summary_sha256": sha256_text(summary),
                    "llm_semantic_review": review,
                    "admission_status": (
                        "admitted" if review["verdict"] == "pass" else "needs_human_review"
                    ),
                }
            )
        except (PolicyViolation, ResultValidationError, SafeSqlExecutionError) as exc:
            record.update(
                {
                    "policy_status": "rejected",
                    "failure_type": type(exc).__name__,
                    "admission_status": "rejected",
                }
            )
        records.append(record)

    generated_at = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = output_path.parent / f".{output_path.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        records_path = staging / "admission_records.jsonl"
        with records_path.open("x", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        counts = {
            "input_rows": len(records),
            "admitted": sum(row["admission_status"] == "admitted" for row in records),
            "needs_human_review": sum(
                row["admission_status"] == "needs_human_review" for row in records
            ),
            "rejected": sum(row["admission_status"] == "rejected" for row in records),
        }
        aggregate = {
            "admission_version": ADMISSION_VERSION,
            "generated_at": generated_at,
            "workspace": WorkspacePin.current(OLIST_V3_WORKSPACE).as_dict(),
            "source": {
                "seed_fixture": str(fixture_path),
                "seed_fixture_sha256": fixture_sha256,
                "selection": str(selection_path),
                "selection_sha256": sha256_file(selection_path),
            },
            "selection": {
                "seed_ids": [str(row["seed_id"]) for row in selected_rows],
                "splits": dict(sorted(Counter(str(row["split"]) for row in selected_rows).items())),
                "primary_buckets": sorted({str(row["primary_bucket"]) for row in selected_rows}),
            },
            "output": {
                "admission_records_jsonl": {
                    "rows": len(records),
                    "sha256": sha256_file(records_path),
                }
            },
            "counts": counts,
            "checks": {
                "status": "pass" if counts["admitted"] == len(records) else "needs_human_review",
                "all_rows_sql_policy_reader_result_contract": counts["rejected"] == 0,
                "fixture_family_split_isolation_verified": True,
                "gold_sql_hashes_recorded": True,
                "reader_role_execution_evidence_recorded": True,
                "llm_semantic_review_is_advisory_only": True,
                "prompt_or_question_materialized": False,
                "protected_holdout_raw_read": False,
            },
            "record_hashes": [
                {
                    "seed_id": record["seed_id"],
                    "family_id": record["family_id"],
                    "gold_sql_sha256": record["gold_sql_sha256"],
                    "policy_final_sql_sha256": record.get("policy_final_sql_sha256"),
                    "validated_result_summary_sha256": record.get(
                        "validated_result_summary_sha256"
                    ),
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
    result = asyncio.run(
        admit(
            args.seeds_jsonl,
            args.selection_json,
            args.output_dir,
            review_mode=args.review_mode,
            generated_at=args.generated_at,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
