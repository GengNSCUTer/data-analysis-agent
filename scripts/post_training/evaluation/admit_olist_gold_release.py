#!/usr/bin/env python3
"""Run a medium Olist Gold release through deterministic admission gates.

Unlike the six-row advisory tool, this command checks every frozen Gold SQL
artifact through the application's SQL policy, PostgreSQL reader role, and
result contract.  It samples the release for advisory semantic review instead
of pretending that an LLM has manually reviewed every repeated construction.
All detailed artifacts remain outside Git.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any
import uuid

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.olist_queryspec import QuerySpec, WorkspacePin  # noqa: E402
from data_analysis_agent.postgres_runner import (  # noqa: E402
    PostgresConnectionSettings,
    SecurePostgresRunner,
)
from data_analysis_agent.result_validator import ResultValidationError  # noqa: E402
from data_analysis_agent.sql_policy import PolicyViolation  # noqa: E402
from data_analysis_agent.sql_repair import SafeSqlExecutionError  # noqa: E402
from scripts.post_training.evaluation.admit_olist_gold_batch import (  # noqa: E402
    REVIEW_MODEL,
    load_materialized_gold_rows,
    make_context,
    review_prompt,
    review_with_siliconflow,
    sha256_file,
)
from vanna.capabilities.sql_runner import RunSqlToolArgs  # noqa: E402


RELEASE_VERSION = "olist-medium-gold-admission-v1"
MAX_RELEASE_ROWS = 1500
SEMANTIC_REVIEW_SAMPLE_SIZE = 40
SEMANTIC_REVIEW_CONCURRENCY = 4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--materialization-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prior-admission-dir", type=Path, default=None)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args()


def _external_new_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError("admission output must stay outside the Git worktree")
    if resolved.exists():
        raise FileExistsError(resolved)
    return resolved


def _external_existing_dir(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError(f"{label} must stay outside the Git worktree")
    if not resolved.is_dir():
        raise FileNotFoundError(resolved)
    return resolved


def _load_prior_records(directory: Path, materialization_sha256: str) -> dict[str, dict[str, Any]]:
    """Reuse only hash-bound deterministic admissions after a contract repair."""
    directory = _external_existing_dir(directory, "prior admission directory")
    manifest_path = directory / "admission_assembly_manifest.json"
    records_path = directory / "admitted_records.jsonl"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines() if line]
    output = manifest.get("output", {}).get("admitted_records_jsonl", {})
    if (
        manifest.get("source", {}).get("materialization_manifest_sha256") != materialization_sha256
        or output.get("rows") != len(records)
        or output.get("sha256") != sha256_file(records_path)
    ):
        raise ValueError("prior admission is not bound to this materialization")
    by_seed = {str(row.get("seed_id")): row for row in records}
    if len(by_seed) != len(records):
        raise ValueError("prior admission has duplicate seed IDs")
    return by_seed


def _review_bucket(row: dict[str, Any]) -> tuple[str, str, str]:
    spec = row["query_spec"]
    time = spec["time"]
    return (
        "+".join(spec["metric_ids"]),
        spec["result_shape"],
        f"{time['mode']}:{time.get('grain') or '-'}",
    )


def semantic_review_seed_ids(rows: list[dict[str, Any]]) -> set[str]:
    """Choose a deterministic, coverage-oriented sample capped at 40 rows."""
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_review_bucket(row)].append(row)
    selected: list[dict[str, Any]] = []
    for bucket in sorted(grouped):
        selected.append(
            min(
                grouped[bucket],
                key=lambda row: hashlib.sha256(str(row["seed_id"]).encode()).hexdigest(),
            )
        )
        if len(selected) == SEMANTIC_REVIEW_SAMPLE_SIZE:
            return {str(row["seed_id"]) for row in selected}
    already = {str(row["seed_id"]) for row in selected}
    remaining = sorted(
        (row for row in rows if str(row["seed_id"]) not in already),
        key=lambda row: hashlib.sha256(str(row["seed_id"]).encode()).hexdigest(),
    )
    selected.extend(remaining[: SEMANTIC_REVIEW_SAMPLE_SIZE - len(selected)])
    return {str(row["seed_id"]) for row in selected}


async def admit_release(
    materialization_dir: Path,
    output_dir: Path,
    *,
    generated_at: str | None = None,
    prior_admission_dir: Path | None = None,
) -> dict[str, Any]:
    output_path = _external_new_dir(output_dir)
    materialization_path = materialization_dir.resolve()
    manifest, gold_rows = load_materialized_gold_rows(
        materialization_path, max_rows=MAX_RELEASE_ROWS
    )
    materialization_sha256 = sha256_file(materialization_path / "materialization_manifest.json")
    prior_by_seed = (
        _load_prior_records(prior_admission_dir, materialization_sha256)
        if prior_admission_dir is not None
        else {}
    )
    review_ids = semantic_review_seed_ids(gold_rows)
    load_dotenv(ROOT / ".env")
    runner = SecurePostgresRunner(
        settings=PostgresConnectionSettings.from_environment(),
        model_name=os.getenv("SILICONFLOW_MODEL", REVIEW_MODEL),
    )
    records: list[dict[str, Any]] = []
    for row in gold_rows:
        spec = QuerySpec.from_mapping(row["query_spec"])
        artifact = row["gold_artifact"]
        seed_id = str(row["seed_id"])
        context = make_context(spec, seed_id)
        record: dict[str, Any] = {
            "seed_id": seed_id,
            "split": row["split"],
            "family_id": row["family_id"],
            "sql_program_id": row["sql_program_id"],
            "query_spec": spec.as_dict(),
            "gold_sql": artifact["sql"],
            "gold_sql_sha256": artifact["sql_sha256"],
            "semantic_review_sampled": seed_id in review_ids,
        }
        prior = prior_by_seed.get(seed_id)
        if prior is not None:
            identity_fields = ("seed_id", "split", "family_id", "sql_program_id", "gold_sql_sha256")
            if any(prior.get(field) != record.get(field) for field in identity_fields):
                raise ValueError(f"prior admission identity drift for {seed_id}")
            if (
                prior.get("policy_status") == "allowed"
                and prior.get("admission_status") in {"admitted", "needs_human_review"}
                and isinstance(prior.get("validated_result_summary"), str)
                and isinstance(prior.get("result_validation"), dict)
            ):
                record.update(
                    {
                        "policy_status": "allowed",
                        "result_validation": prior["result_validation"],
                        "validated_result_summary": prior["validated_result_summary"],
                        "admission_status": "admitted",
                        "reused_deterministic_admission": True,
                    }
                )
                records.append(record)
                continue
        try:
            await runner.run_sql(RunSqlToolArgs(sql=artifact["sql"]), context)
            record.update(
                {
                    "policy_status": "allowed",
                    "result_validation": context.metadata["result_validation"],
                    "validated_result_summary": context.metadata["validated_result_summary"],
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
        records.append(record)

    # The advisory sample is re-reviewed after deterministic evidence exists.
    # Calls are bounded: review remains optional evidence and must not turn a
    # 40-row sample into 40 serial network waits.
    review_targets = [
        record
        for record in records
        if record["semantic_review_sampled"] and record["admission_status"] == "admitted"
    ]
    semaphore = asyncio.Semaphore(SEMANTIC_REVIEW_CONCURRENCY)

    async def review_record(record: dict[str, Any]) -> None:
        spec = QuerySpec.from_mapping(record["query_spec"])
        prompt = review_prompt(spec, record["gold_sql"], record["validated_result_summary"])
        async with semaphore:
            review = await asyncio.to_thread(review_with_siliconflow, prompt)
        record["llm_semantic_review"] = review
        if review["verdict"] != "pass":
            record["admission_status"] = "needs_human_review"

    await asyncio.gather(*(review_record(record) for record in review_targets))

    generated_at = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    counts = {
        "input_rows": len(records),
        "admitted": sum(row["admission_status"] == "admitted" for row in records),
        "needs_human_review": sum(
            row["admission_status"] == "needs_human_review" for row in records
        ),
        "rejected": sum(row["admission_status"] == "rejected" for row in records),
        "semantic_review_sampled": len(review_ids),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = output_path.parent / f".{output_path.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        records_path = staging / "admitted_records.jsonl"
        with records_path.open("x", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        status = "pass" if counts["admitted"] == len(records) else "needs_human_review"
        release = {
            "admission_version": RELEASE_VERSION,
            "generated_at": generated_at,
            "workspace": WorkspacePin.current().as_dict(),
            "source": {
                "materialization_manifest_sha256": sha256_file(
                    materialization_path / "materialization_manifest.json"
                ),
                "protected_summary_sha256": manifest["source"]["protected_summary_sha256"],
                "protected_evidence_sha256": manifest["source"]["protected_evidence_sha256"],
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
                "all_rows_reviewed": False,
            },
            "reuse": {
                "prior_admission_manifest_sha256": (
                    sha256_file(prior_admission_dir.resolve() / "admission_assembly_manifest.json")
                    if prior_admission_dir is not None
                    else None
                ),
                "reused_deterministic_admissions": sum(
                    bool(row.get("reused_deterministic_admission")) for row in records
                ),
            },
            "checks": {
                "status": status,
                "all_rows_sql_policy_reader_result_contract": counts["rejected"] == 0,
                "semantic_review_is_sampled_advisory": True,
                "protected_holdout_raw_read": False,
                "prompt_or_question_materialized": False,
            },
        }
        (staging / "admission_assembly_manifest.json").write_text(
            json.dumps(release, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(output_path)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return release


def main() -> int:
    args = parse_args()
    result = asyncio.run(
        admit_release(
            args.materialization_dir,
            args.output_dir,
            generated_at=args.generated_at,
            prior_admission_dir=args.prior_admission_dir,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
