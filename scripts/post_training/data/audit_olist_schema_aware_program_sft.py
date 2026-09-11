#!/usr/bin/env python3
"""Recompute and audit an external Olist Schema-aware Program SFT release.

The materializer emits data and a self-description.  This separate read-only
gate re-derives every expected Task A and Task B event from the frozen Olist
Release v2 sources, then compares canonical records and hashes without
printing or copying questions, prompts, SQL, plan JSON, or model artifacts
into Git.  It never reads TheLook, executes SQL, calls an LLM, or uses a GPU.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any, Mapping
import uuid


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.olist_queryspec import WorkspacePin  # noqa: E402
from data_analysis_agent.semantic_catalog import CatalogLoader  # noqa: E402
from scripts.post_training.data.materialize_olist_schema_aware_program_sft import (  # noqa: E402
    CONTRACT_VERSION,
    TASK_A,
    TASK_B,
    SchemaAwareMaterializationError,
    _load_admission_records,
    _load_source_audit,
    _read_json,
    _read_jsonl,
    _require_olist_release_path,
    build_events,
    load_source_split,
    load_tokenizer,
    sha256_file,
)


AUDIT_VERSION = "olist-schema-aware-program-sft-audit-v1"


class SchemaAwareAuditError(ValueError):
    """Raised when materialized Task A/B evidence differs from its source."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--materialization-dir", type=Path, required=True)
    parser.add_argument("--source-split-audit", type=Path, required=True)
    parser.add_argument("--source-train-jsonl", type=Path, required=True)
    parser.add_argument("--source-validation-jsonl", type=Path, required=True)
    parser.add_argument("--admission-dir", type=Path, required=True)
    parser.add_argument("--tokenizer-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-train", type=int, default=2400)
    parser.add_argument("--expected-validation", type=int, default=600)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args(argv)


def _external_existing_dir(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise SchemaAwareAuditError(f"{label} must stay outside the Git worktree")
    if not resolved.is_dir():
        raise SchemaAwareAuditError(f"{label} does not exist: {resolved}")
    return resolved


def _external_new_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise SchemaAwareAuditError("audit output must stay outside the Git worktree")
    if resolved.exists():
        raise SchemaAwareAuditError(f"audit output already exists: {resolved}")
    return resolved


def _canonical_row(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def records_sha256(rows: list[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_canonical_row(row).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def require_exact_records(
    actual: list[Mapping[str, Any]], expected: list[Mapping[str, Any]], label: str
) -> None:
    """Compare complete records without exposing protected text in errors."""

    if len(actual) != len(expected):
        raise SchemaAwareAuditError(
            f"{label} row count differs: actual={len(actual)}, expected={len(expected)}"
        )
    actual_hash = records_sha256(actual)
    expected_hash = records_sha256(expected)
    if actual_hash != expected_hash:
        raise SchemaAwareAuditError(
            f"{label} record content differs: actual_sha256={actual_hash}, expected_sha256={expected_hash}"
        )


def _read_external_rows(
    path: Path, label: str, *, allow_empty: bool = False
) -> list[dict[str, Any]]:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT) or not resolved.is_file():
        raise SchemaAwareAuditError(f"{label} is not an external readable file")
    if allow_empty and not resolved.read_text(encoding="utf-8").strip():
        return []
    try:
        return _read_jsonl(resolved, label)
    except SchemaAwareMaterializationError as exc:
        raise SchemaAwareAuditError(str(exc)) from exc


def _require_materialization_file(directory: Path, name: str) -> Path:
    path = directory / name
    if not path.is_file():
        raise SchemaAwareAuditError(f"materialization is missing {name}")
    return path


def _metadata_count(metadata: Mapping[str, Any], field: str, expected: int) -> None:
    if metadata.get(field) != expected:
        raise SchemaAwareAuditError(
            f"materialization {field} differs: actual={metadata.get(field)}, expected={expected}"
        )


def audit(
    materialization_dir: Path,
    source_audit_path: Path,
    source_train_path: Path,
    source_validation_path: Path,
    admission_dir: Path,
    tokenizer_dir: Path,
    output_dir: Path,
    *,
    expected_train: int,
    expected_validation: int,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Rebuild every event from Olist-only sources and write a redacted report."""

    if expected_train <= 0 or expected_validation <= 0:
        raise SchemaAwareAuditError("expected train and validation counts must be positive")
    materialization = _external_existing_dir(materialization_dir, "materialization directory")
    output = _external_new_dir(output_dir)
    materialization_audit_path = _require_materialization_file(
        materialization, "materialization_audit.json"
    )
    materialization_audit = _read_json(materialization_audit_path, "materialization audit")
    if materialization_audit.get("audit_version") != CONTRACT_VERSION:
        raise SchemaAwareAuditError("materialization audit version is unsupported")
    checks = materialization_audit.get("checks")
    if not isinstance(checks, Mapping) or checks.get("status") != "pass":
        raise SchemaAwareAuditError("materialization audit did not pass")
    policy = materialization_audit.get("policy")
    if (
        not isinstance(policy, Mapping)
        or policy.get("source_splits") != ["train", "validation"]
        or policy.get("in_domain_test_materialized") is not False
        or policy.get("thelook_read") is not False
        or policy.get("task_a_runtime_prompt_unchanged") is not True
        or policy.get("sql_and_plan_targets_separate") is not True
    ):
        raise SchemaAwareAuditError("materialization policy boundary is incomplete")
    if materialization_audit.get("workspace") != WorkspacePin.current().as_dict():
        raise SchemaAwareAuditError("materialization workspace differs from current frozen workspace")

    train_path = _require_olist_release_path(source_train_path, "source train JSONL")
    validation_path = _require_olist_release_path(
        source_validation_path, "source validation JSONL"
    )
    expected = {"train": expected_train, "validation": expected_validation}
    try:
        source_audit = _load_source_audit(
            source_audit_path, train_path, validation_path, expected
        )
    except SchemaAwareMaterializationError as exc:
        raise SchemaAwareAuditError(str(exc)) from exc
    source_by_split = {
        "train": load_source_split(train_path, "train", expected_train),
        "validation": load_source_split(
            validation_path, "validation", expected_validation
        ),
    }
    source_meta = materialization_audit.get("source")
    if not isinstance(source_meta, Mapping):
        raise SchemaAwareAuditError("materialization audit lacks source evidence")
    source_audit_resolved = _require_olist_release_path(
        source_audit_path, "source split audit"
    )
    source_paths_and_hashes = {
        "source_split_audit_sha256": sha256_file(source_audit_resolved),
        "source_train_sha256": sha256_file(train_path),
        "source_validation_sha256": sha256_file(validation_path),
    }
    if any(source_meta.get(name) != value for name, value in source_paths_and_hashes.items()):
        raise SchemaAwareAuditError("materialization source hashes differ from supplied Olist Release v2")
    admission_manifest_hash = source_audit.get("source", {}).get(
        "admission_assembly_manifest_sha256"
    )
    if not isinstance(admission_manifest_hash, str):
        raise SchemaAwareAuditError("source audit lacks admission manifest hash")
    try:
        admissions = _load_admission_records(admission_dir, admission_manifest_hash)
    except SchemaAwareMaterializationError as exc:
        raise SchemaAwareAuditError(str(exc)) from exc
    admission_records_path = Path(admission_dir).resolve() / "admitted_records.jsonl"
    if (
        source_meta.get("admission_manifest_sha256") != admission_manifest_hash
        or source_meta.get("admission_records_sha256") != sha256_file(admission_records_path)
    ):
        raise SchemaAwareAuditError("materialization admission provenance differs from source audit")

    tokenizer = load_tokenizer(tokenizer_dir)
    length_contract = materialization_audit.get("training_length_contract")
    tokenizer_meta = materialization_audit.get("tokenizer")
    if (
        not isinstance(length_contract, Mapping)
        or not isinstance(tokenizer_meta, Mapping)
        or length_contract.get("max_seq_length") != 3072
        or length_contract.get("silent_truncation") is not False
        or tokenizer_meta.get("eos_token_id") != tokenizer.eos_token_id
    ):
        raise SchemaAwareAuditError("materialization tokenizer or length contract differs")
    try:
        expected_events, expected_pairing, expected_exclusions = build_events(
            source_by_split,
            admissions,
            tokenizer,
            int(length_contract["max_seq_length"]),
            catalog=CatalogLoader().load(),
        )
    except SchemaAwareMaterializationError as exc:
        raise SchemaAwareAuditError(
            "re-derived source split violates the Schema-aware Program SFT contract"
        ) from exc
    if expected_exclusions:
        raise SchemaAwareAuditError("re-derived events contain length exclusions")

    actual_events: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for split in ("train", "validation"):
        actual_events[split] = {}
        for task, name in ((TASK_A, "task_a"), (TASK_B, "task_b"), ("interleaved", "events")):
            actual_events[split][task] = _read_external_rows(
                _require_materialization_file(materialization, f"{split}_{name}.jsonl"),
                f"{split} {name}",
            )
            require_exact_records(
                actual_events[split][task], expected_events[split][task], f"{split} {name}"
            )
        splits_meta = materialization_audit.get("splits")
        metadata = splits_meta.get(split) if isinstance(splits_meta, Mapping) else None
        if not isinstance(metadata, Mapping):
            raise SchemaAwareAuditError(f"materialization audit has no {split} metadata")
        _metadata_count(metadata, "query_instances", expected[split])
        _metadata_count(metadata, "task_a_events", expected[split])
        _metadata_count(metadata, "task_b_events", expected[split])
        _metadata_count(metadata, "interleaved_events", expected[split] * 2)
        for task, name, field in (
            (TASK_A, "task_a", "task_a_sha256"),
            (TASK_B, "task_b", "task_b_sha256"),
            ("interleaved", "events", "events_sha256"),
        ):
            path = _require_materialization_file(materialization, f"{split}_{name}.jsonl")
            if metadata.get(field) != sha256_file(path):
                raise SchemaAwareAuditError(f"materialization {split} {name} hash differs")

    pairing_path = _require_materialization_file(materialization, "pairing.jsonl")
    actual_pairing = _read_external_rows(pairing_path, "pairing")
    require_exact_records(actual_pairing, expected_pairing, "pairing")
    pairing_meta = materialization_audit.get("pairing")
    if (
        not isinstance(pairing_meta, Mapping)
        or pairing_meta.get("rows") != expected_train + expected_validation
        or pairing_meta.get("fully_eligible_pairs") != expected_train + expected_validation
        or pairing_meta.get("sha256") != sha256_file(pairing_path)
    ):
        raise SchemaAwareAuditError("materialization pairing metadata differs")
    exclusions_path = _require_materialization_file(materialization / "exclusions", "length.jsonl")
    actual_exclusions = _read_external_rows(
        exclusions_path, "length exclusions", allow_empty=True
    )
    if actual_exclusions:
        raise SchemaAwareAuditError("materialization has length exclusions")
    exclusion_meta = materialization_audit.get("exclusions")
    if (
        not isinstance(exclusion_meta, Mapping)
        or exclusion_meta.get("rows") != 0
        or exclusion_meta.get("sha256") != sha256_file(exclusions_path)
        or exclusion_meta.get("contains_question_or_sql") is not False
    ):
        raise SchemaAwareAuditError("materialization exclusion evidence differs")

    generated_at = (
        generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        report = {
            "audit_version": AUDIT_VERSION,
            "generated_at": generated_at,
            "materialization": {
                "dir": str(materialization),
                "audit_sha256": sha256_file(materialization_audit_path),
                "audit_version": materialization_audit["audit_version"],
            },
            "workspace": WorkspacePin.current().as_dict(),
            "counts": {
                "query_instances": expected,
                "task_a_events": expected,
                "task_b_events": expected,
                "interleaved_events": {
                    split: expected[split] * 2 for split in ("train", "validation")
                },
                "pairs": len(actual_pairing),
                "length_exclusions": 0,
            },
            "evidence": {
                "source_split_audit_sha256": sha256_file(source_audit_resolved),
                "source_train_sha256": sha256_file(train_path),
                "source_validation_sha256": sha256_file(validation_path),
                "pairing_sha256": sha256_file(pairing_path),
                "train_events_sha256": sha256_file(
                    _require_materialization_file(materialization, "train_events.jsonl")
                ),
                "validation_events_sha256": sha256_file(
                    _require_materialization_file(materialization, "validation_events.jsonl")
                ),
            },
            "checks": {
                "status": "pass",
                "source_hashes_match": True,
                "family_and_query_spec_isolation_rederived": True,
                "task_a_prompt_sql_and_training_text_exact": True,
                "task_b_schema_link_plan_rederived": True,
                "task_a_task_b_pairing_exact": True,
                "interleaved_event_order_exact": True,
                "no_length_exclusions": True,
                "in_domain_test_used": False,
                "thelook_read": False,
                "sql_executed": False,
                "model_called": False,
                "gpu_used": False,
                "raw_question_or_sql_in_report": False,
            },
        }
        (staging / "audit-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = audit(
        args.materialization_dir,
        args.source_split_audit,
        args.source_train_jsonl,
        args.source_validation_jsonl,
        args.admission_dir,
        args.tokenizer_dir,
        args.output_dir,
        expected_train=args.expected_train,
        expected_validation=args.expected_validation,
        generated_at=args.generated_at,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SchemaAwareAuditError, SchemaAwareMaterializationError) as exc:
        print(f"Schema-aware SFT audit error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
