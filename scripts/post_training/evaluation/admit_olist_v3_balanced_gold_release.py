#!/usr/bin/env python3
"""Execute every Olist v3 balanced Gold SQL row through trusted admission.

This is the full deterministic gate between structural Gold construction and
Chinese surface/Prompt/SFT materialization.  It validates the 4,500-row,
hash-bound v3 structural release, executes each canonical SQL through
SqlPolicy and the PostgreSQL reader role, and preserves ResultContract
evidence.  A bounded, stratified DeepSeek sample is advisory evidence only.
No questions, runtime prompts, training rows, model candidate SQL, or GPU work
are created here.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
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

from data_analysis_agent.metric_context import OLIST_V3_WORKSPACE  # noqa: E402
from data_analysis_agent.olist_queryspec import (
    QuerySpec,
    WorkspacePin,
    validate_query_spec,
)  # noqa: E402
from data_analysis_agent.postgres_runner import (  # noqa: E402
    PostgresConnectionSettings,
    SecurePostgresRunner,
)
from data_analysis_agent.result_validator import ResultValidationError  # noqa: E402
from data_analysis_agent.semantic_catalog import CatalogLoader  # noqa: E402
from data_analysis_agent.sql_policy import PolicyViolation, SqlPolicy  # noqa: E402
from data_analysis_agent.sql_repair import SafeSqlExecutionError  # noqa: E402
from scripts.post_training.data.build_olist_v3_balanced_release_candidates import (  # noqa: E402
    BUCKETS,
    LEGACY_RELEASE_VERSION,
    RELEASE_VERSION,
    SPLITS,
    SPLIT_TARGETS,
)
from scripts.post_training.data.materialize_olist_queryspecs import sha256_file  # noqa: E402
from scripts.post_training.evaluation.admit_olist_gold_batch import (  # noqa: E402
    REVIEW_MODEL,
    review_prompt,
    review_with_siliconflow,
)
from scripts.post_training.evaluation.admit_olist_v3_gold_seed_batch import (  # noqa: E402
    make_context,
)
from vanna.capabilities.sql_runner import RunSqlToolArgs  # noqa: E402


ADMISSION_VERSION = "olist-v3-balanced-gold-admission-v1"
ADVISORY_RECONCILIATION_VERSION = (
    "olist-v3-balanced-gold-admission-advisory-reconciliation-v1"
)
EXPECTED_ROWS = 4500
MAX_CONCURRENT_EXECUTIONS = 8
SEMANTIC_REVIEW_SAMPLE_SIZE = 48
SEMANTIC_REVIEW_CONCURRENCY = 4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--materialization-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--reconcile-advisory-provider-errors-from",
        type=Path,
        default=None,
        help="Hash-bound completed admission to reassemble without re-executing SQL; only provider-error advisory reviews are non-blocking.",
    )
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_advisory_provider_error(review: Mapping[str, Any]) -> bool:
    """Identify a review transport/provider failure, not a semantic objection."""
    issues = review.get("issues")
    return (
        review.get("verdict") == "needs_human_review"
        and isinstance(issues, list)
        and bool(issues)
        and all(
            isinstance(issue, str) and issue.startswith("review_provider_error:")
            for issue in issues
        )
    )


def _external_existing_dir(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError(f"{label} must stay outside the Git worktree")
    if not resolved.is_dir():
        raise FileNotFoundError(resolved)
    return resolved


def _external_new_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError("full admission output must stay outside the Git worktree")
    if resolved.exists():
        raise FileExistsError(resolved)
    return resolved


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"{label} does not exist: {path}")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} has invalid JSON at line {line_number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{label} line {line_number} must be an object")
        rows.append(row)
    if not rows:
        raise ValueError(f"{label} is empty")
    return rows


def load_full_v3_gold_rows(
    directory: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fail closed unless every structural artifact is bound to this v3 release."""
    directory = _external_existing_dir(directory, "v3 structural materialization")
    manifest_path = directory / "materialization_manifest.json"
    query_path = directory / "query_specs.jsonl"
    gold_path = directory / "gold_sql.jsonl"
    manifest = _read_json(manifest_path, "v3 structural manifest")
    if (
        manifest.get("release_version") not in {LEGACY_RELEASE_VERSION, RELEASE_VERSION}
        or manifest.get("workspace")
        != WorkspacePin.current(OLIST_V3_WORKSPACE).as_dict()
        or manifest.get("checks", {}).get("status") != "pass"
        or manifest.get("checks", {}).get("bucket_targets_exact") is not True
    ):
        raise ValueError(
            "structural materialization is not the expected passing v3 release"
        )
    output = manifest.get("outputs", {})
    if (
        output.get("query_specs_jsonl", {}).get("rows") != EXPECTED_ROWS
        or output.get("gold_sql_jsonl", {}).get("rows") != EXPECTED_ROWS
        or output.get("query_specs_jsonl", {}).get("sha256") != sha256_file(query_path)
        or output.get("gold_sql_jsonl", {}).get("sha256") != sha256_file(gold_path)
    ):
        raise ValueError(
            "v3 structural artifact hashes or row counts do not match its manifest"
        )
    query_rows = _read_jsonl(query_path, "v3 QuerySpec artifact")
    gold_rows = _read_jsonl(gold_path, "v3 Gold SQL artifact")
    if len(query_rows) != EXPECTED_ROWS or len(gold_rows) != EXPECTED_ROWS:
        raise ValueError("v3 structural artifacts do not contain exactly 4,500 rows")
    query_by_seed = {str(row.get("seed_id")): row for row in query_rows}
    gold_by_seed = {str(row.get("seed_id")): row for row in gold_rows}
    if len(query_by_seed) != len(query_rows) or set(query_by_seed) != set(gold_by_seed):
        raise ValueError(
            "v3 QuerySpec/Gold seed identity does not form a one-to-one mapping"
        )
    if Counter(str(row.get("split")) for row in query_rows) != Counter(SPLIT_TARGETS):
        raise ValueError("v3 structural split counts drifted")
    catalog = CatalogLoader(workspace=OLIST_V3_WORKSPACE).load()
    rows: list[dict[str, Any]] = []
    families: dict[str, str] = {}
    query_spec_splits: dict[str, str] = {}
    sql_hash_splits: dict[str, str] = {}
    for seed_id in sorted(query_by_seed):
        query_row = query_by_seed[seed_id]
        gold_row = gold_by_seed[seed_id]
        identity_fields = (
            "seed_id",
            "split",
            "family_id",
            "sql_program_id",
            "candidate_source",
            "source_seed_id",
            "primary_bucket",
            "risk_tags",
        )
        if any(
            query_row.get(field) != gold_row.get(field) for field in identity_fields
        ):
            raise ValueError(
                f"v3 QuerySpec/Gold structural identity drift for {seed_id}"
            )
        split = query_row.get("split")
        bucket = query_row.get("primary_bucket")
        if split not in SPLITS or bucket not in BUCKETS:
            raise ValueError("v3 structural row has an invalid split or bucket")
        spec = QuerySpec.from_mapping(query_row.get("query_spec", {}))
        if spec.workspace != WorkspacePin.current(OLIST_V3_WORKSPACE):
            raise ValueError("v3 structural QuerySpec workspace drifted")
        validate_query_spec(spec, catalog)
        artifact = gold_row.get("gold_artifact")
        if not isinstance(artifact, dict) or not isinstance(artifact.get("sql"), str):
            raise ValueError("v3 Gold artifact is malformed")
        if artifact.get("query_spec_id") != spec.query_spec_id or artifact.get(
            "sql_sha256"
        ) != sha256_text(artifact["sql"]):
            raise ValueError("v3 Gold artifact identity/hash drifted")
        for identity, value, seen in (
            ("family", str(query_row["family_id"]), families),
            ("QuerySpec", spec.query_spec_id, query_spec_splits),
            ("canonical SQL", str(artifact["sql_sha256"]), sql_hash_splits),
        ):
            existing = seen.setdefault(value, str(split))
            if existing != split:
                raise ValueError(f"{identity} crosses formal splits")
        rows.append({**query_row, "gold_artifact": artifact})
    if Counter(
        (str(row["split"]), str(row["primary_bucket"])) for row in rows
    ) != Counter(
        (split, bucket)
        for split in SPLITS
        for bucket in BUCKETS
        for _ in range(manifest["targets"][split][bucket])
    ):
        raise ValueError("v3 structural per-bucket quotas drifted")
    if manifest.get("release_version") == RELEASE_VERSION:
        balance = manifest.get("counts", {}).get("v3_1_balance", {})
        if (
            manifest.get("checks", {}).get("v3_1_balance_contract") != "pass"
            or balance.get("status") != "pass"
        ):
            raise ValueError("v3.1 structural balance evidence is missing or failed")
    return manifest, rows


def semantic_review_seed_ids(rows: list[dict[str, Any]]) -> set[str]:
    """Select 48 deterministic records spanning split, bucket, metric and grain."""
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        spec = row["query_spec"]
        assert isinstance(spec, Mapping)
        time = spec["time"]
        assert isinstance(time, Mapping)
        groups[
            (
                str(row["split"]),
                str(row["primary_bucket"]),
                "+".join(spec["metric_ids"]),
                f"{time['mode']}:{time.get('grain') or '-'}",
            )
        ].append(row)
    ordered: list[dict[str, Any]] = []
    for group in sorted(groups):
        ordered.append(
            min(
                groups[group],
                key=lambda row: sha256_text(f"{ADMISSION_VERSION}:{row['seed_id']}"),
            )
        )
        if len(ordered) == SEMANTIC_REVIEW_SAMPLE_SIZE:
            return {str(row["seed_id"]) for row in ordered}
    selected = {str(row["seed_id"]) for row in ordered}
    remaining = sorted(
        (row for row in rows if str(row["seed_id"]) not in selected),
        key=lambda row: sha256_text(f"{ADMISSION_VERSION}:{row['seed_id']}"),
    )
    selected.update(
        str(row["seed_id"])
        for row in remaining[: SEMANTIC_REVIEW_SAMPLE_SIZE - len(selected)]
    )
    return selected


async def admit_release(
    materialization_dir: Path,
    output_dir: Path,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Run full v3 reader-role admission and bounded advisory semantic review."""
    materialization_dir = _external_existing_dir(
        materialization_dir, "v3 structural materialization"
    )
    output_dir = _external_new_dir(output_dir)
    manifest, rows = load_full_v3_gold_rows(materialization_dir)
    review_ids = semantic_review_seed_ids(rows)
    if len(review_ids) != SEMANTIC_REVIEW_SAMPLE_SIZE:
        raise AssertionError("review sampling did not produce its frozen size")

    catalog = CatalogLoader(workspace=OLIST_V3_WORKSPACE).load()
    load_dotenv(ROOT / ".env")
    policy = SqlPolicy(workspace=OLIST_V3_WORKSPACE)
    runner = SecurePostgresRunner(
        settings=PostgresConnectionSettings.from_environment(),
        model_name=os.getenv("SILICONFLOW_MODEL", REVIEW_MODEL),
        policy=policy,
        workspace=OLIST_V3_WORKSPACE,
    )
    execution_semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXECUTIONS)

    async def admit_one(row: dict[str, Any]) -> dict[str, Any]:
        spec = QuerySpec.from_mapping(row["query_spec"])
        artifact = row["gold_artifact"]
        assert isinstance(artifact, Mapping)
        sql = str(artifact["sql"])
        seed_id = str(row["seed_id"])
        record: dict[str, Any] = {
            "seed_id": seed_id,
            "split": row["split"],
            "family_id": row["family_id"],
            "sql_program_id": row["sql_program_id"],
            "candidate_source": row["candidate_source"],
            "source_seed_id": row["source_seed_id"],
            "primary_bucket": row["primary_bucket"],
            "risk_tags": row["risk_tags"],
            "query_spec": spec.as_dict(),
            "gold_sql": sql,
            "gold_sql_sha256": artifact["sql_sha256"],
            "gold_renderer_version": artifact["renderer_version"],
            "semantic_review_sampled": seed_id in review_ids,
        }
        try:
            decision = policy.evaluate(sql, role="analyst")
            context = make_context(spec, seed_id, catalog)
            async with execution_semaphore:
                await runner.run_sql(RunSqlToolArgs(sql=sql), context)
            summary = str(context.metadata["validated_result_summary"])
            record.update(
                {
                    "policy_status": decision.status,
                    "policy_final_sql_sha256": sha256_text(decision.final_sql),
                    "policy_limit_applied": decision.policy_limit_applied,
                    "reader_role": OLIST_V3_WORKSPACE.reader_role,
                    "result_validation": context.metadata["result_validation"],
                    "validated_result_summary": summary,
                    "validated_result_summary_sha256": sha256_text(summary),
                    "admission_status": "admitted",
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
        return record

    records = await asyncio.gather(*(admit_one(row) for row in rows))
    review_targets = [
        record
        for record in records
        if record["semantic_review_sampled"]
        and record["admission_status"] == "admitted"
    ]
    review_semaphore = asyncio.Semaphore(SEMANTIC_REVIEW_CONCURRENCY)

    async def review_record(record: dict[str, Any]) -> None:
        spec = QuerySpec.from_mapping(record["query_spec"])
        prompt = review_prompt(
            spec, str(record["gold_sql"]), str(record["validated_result_summary"])
        )
        async with review_semaphore:
            review = await asyncio.to_thread(review_with_siliconflow, prompt)
        record["llm_semantic_review"] = review
        if review["verdict"] != "pass":
            # LLM sampling is advisory.  A provider timeout cannot downgrade
            # deterministic Policy -> reader-role -> ResultContract evidence;
            # it remains explicitly recorded for a later retry/follow-up.
            if is_advisory_provider_error(review):
                record["llm_semantic_review_status"] = "provider_error_advisory"
            else:
                record["admission_status"] = "needs_human_review"

    await asyncio.gather(*(review_record(record) for record in review_targets))

    counts = {
        "input_rows": len(records),
        "admitted": sum(record["admission_status"] == "admitted" for record in records),
        "needs_human_review": sum(
            record["admission_status"] == "needs_human_review" for record in records
        ),
        "rejected": sum(record["admission_status"] == "rejected" for record in records),
        "semantic_review_sampled": len(review_ids),
        "semantic_review_provider_errors": sum(
            record.get("llm_semantic_review_status") == "provider_error_advisory"
            for record in records
        ),
    }
    generated_at = (
        generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.parent / f".{output_dir.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        records_path = staging / "admitted_records.jsonl"
        with records_path.open("x", encoding="utf-8") as handle:
            for record in records:
                handle.write(
                    json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                )
        status = "pass" if counts["admitted"] == len(records) else "needs_human_review"
        aggregate = {
            "admission_version": ADMISSION_VERSION,
            "generated_at": generated_at,
            "workspace": WorkspacePin.current(OLIST_V3_WORKSPACE).as_dict(),
            "source": {
                "structural_manifest_sha256": sha256_file(
                    materialization_dir / "materialization_manifest.json"
                ),
                "structural_query_specs_sha256": manifest["outputs"][
                    "query_specs_jsonl"
                ]["sha256"],
                "structural_gold_sql_sha256": manifest["outputs"]["gold_sql_jsonl"][
                    "sha256"
                ],
                "protected_summary_sha256": manifest["source"][
                    "protected_summary_sha256"
                ],
                "protected_evidence_sha256": manifest["source"][
                    "protected_evidence_sha256"
                ],
            },
            "output": {
                "admitted_records_jsonl": {
                    "rows": len(records),
                    "sha256": sha256_file(records_path),
                }
            },
            "counts": counts,
            "semantic_review": {
                "provider": "siliconflow",
                "model": os.getenv("SILICONFLOW_MODEL", REVIEW_MODEL),
                "method": "deterministic_stratified_sample",
                "sample_size": len(review_ids),
                "sample_seed_ids": sorted(review_ids),
                "bounded_concurrency": SEMANTIC_REVIEW_CONCURRENCY,
                "all_rows_reviewed": False,
            },
            "execution": {
                "reader_role": OLIST_V3_WORKSPACE.reader_role,
                "bounded_concurrency": MAX_CONCURRENT_EXECUTIONS,
            },
            "checks": {
                "status": status,
                "all_rows_sql_policy_reader_result_contract": counts["rejected"] == 0,
                "gold_sql_hashes_recorded": True,
                "reader_role_execution_evidence_recorded": True,
                "llm_semantic_review_is_sampled_advisory_only": True,
                "llm_provider_error_does_not_override_deterministic_admission": True,
                "prompt_or_question_materialized": False,
                "protected_holdout_raw_read": False,
                "model_candidate_sql_generated": False,
                "gpu_used": False,
            },
            "record_hashes": [
                {
                    "seed_id": record["seed_id"],
                    "split": record["split"],
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
        (staging / "admission_assembly_manifest.json").write_text(
            json.dumps(aggregate, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return aggregate


def reconcile_advisory_provider_errors(
    admission_dir: Path,
    output_dir: Path,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Reassemble a completed run when only advisory-provider errors blocked it.

    This preserves the original immutable records/manifest and does *not*
    re-execute SQL.  It is valid only when the upstream manifest proves every
    row passed the deterministic chain and each non-admitted row is solely an
    LLM transport/provider error.  Real semantic review objections remain
    `needs_human_review` and keep the successor manifest non-passing.
    """
    admission_dir = _external_existing_dir(
        admission_dir, "completed admission directory"
    )
    output_dir = _external_new_dir(output_dir)
    upstream_manifest_path = admission_dir / "admission_assembly_manifest.json"
    upstream_records_path = admission_dir / "admitted_records.jsonl"
    upstream = _read_json(upstream_manifest_path, "completed admission manifest")
    records = _read_jsonl(upstream_records_path, "completed admission records")
    output = upstream.get("output", {}).get("admitted_records_jsonl", {})
    if (
        output.get("rows") != EXPECTED_ROWS
        or output.get("sha256") != sha256_file(upstream_records_path)
        or len(records) != EXPECTED_ROWS
        or upstream.get("checks", {}).get("all_rows_sql_policy_reader_result_contract")
        is not True
        or upstream.get("counts", {}).get("rejected") != 0
    ):
        raise ValueError(
            "completed admission lacks hash-bound deterministic passing evidence"
        )
    reconciled: list[dict[str, Any]] = []
    provider_errors = 0
    real_review_blocks = 0
    for record in records:
        copied = dict(record)
        review = copied.get("llm_semantic_review")
        if copied.get("admission_status") == "needs_human_review" and isinstance(
            review, Mapping
        ):
            if is_advisory_provider_error(review):
                copied["admission_status"] = "admitted"
                copied["llm_semantic_review_status"] = "provider_error_advisory"
                provider_errors += 1
            else:
                real_review_blocks += 1
        reconciled.append(copied)
    counts = {
        "input_rows": len(reconciled),
        "admitted": sum(
            record["admission_status"] == "admitted" for record in reconciled
        ),
        "needs_human_review": sum(
            record["admission_status"] == "needs_human_review" for record in reconciled
        ),
        "rejected": sum(
            record["admission_status"] == "rejected" for record in reconciled
        ),
        "semantic_review_sampled": upstream.get("counts", {}).get(
            "semantic_review_sampled"
        ),
        "semantic_review_provider_errors": provider_errors,
        "semantic_review_real_blocks": real_review_blocks,
    }
    generated_at = (
        generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.parent / f".{output_dir.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        records_path = staging / "admitted_records.jsonl"
        with records_path.open("x", encoding="utf-8") as handle:
            for record in reconciled:
                handle.write(
                    json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                )
        status = (
            "pass" if counts["admitted"] == len(reconciled) else "needs_human_review"
        )
        manifest = {
            "admission_version": ADVISORY_RECONCILIATION_VERSION,
            "generated_at": generated_at,
            "workspace": upstream.get("workspace"),
            "source": {
                **dict(upstream.get("source", {})),
                "upstream_admission_manifest_sha256": sha256_file(
                    upstream_manifest_path
                ),
                "upstream_admitted_records_sha256": sha256_file(upstream_records_path),
            },
            "output": {
                "admitted_records_jsonl": {
                    "rows": len(reconciled),
                    "sha256": sha256_file(records_path),
                }
            },
            "counts": counts,
            "semantic_review": {
                **dict(upstream.get("semantic_review", {})),
                "provider_error_treatment": "nonblocking_advisory_with_recorded_followup",
            },
            "checks": {
                "status": status,
                "all_rows_sql_policy_reader_result_contract": True,
                "gold_sql_hashes_recorded": True,
                "reader_role_execution_evidence_recorded": True,
                "llm_semantic_review_is_sampled_advisory_only": True,
                "llm_provider_error_does_not_override_deterministic_admission": True,
                "upstream_hash_bound": True,
                "sql_reexecuted": False,
                "model_called": False,
                "gpu_used": False,
            },
        }
        (staging / "admission_assembly_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def main() -> int:
    args = parse_args()
    if args.reconcile_advisory_provider_errors_from is not None:
        aggregate = reconcile_advisory_provider_errors(
            args.reconcile_advisory_provider_errors_from,
            args.output_dir,
            generated_at=args.generated_at,
        )
    else:
        aggregate = asyncio.run(
            admit_release(
                args.materialization_dir,
                args.output_dir,
                generated_at=args.generated_at,
            )
        )
    print(json.dumps(aggregate, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
