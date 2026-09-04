#!/usr/bin/env python3
"""Materialize audited Olist QuerySpec and Gold-SQL intermediate artifacts.

This is not an SFT data builder. It accepts only structural coverage seeds,
never reads questions or protected holdout cases, and never executes SQL.
All artifacts must be written outside the Git worktree.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import uuid
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.olist_queryspec import (  # noqa: E402
    QUERY_SPEC_SCHEMA_VERSION,
    RENDERER_VERSION,
    QuerySpec,
    QuerySpecValidationError,
    QueryTime,
    WorkspacePin,
    render_gold_sql,
    validate_query_spec,
)


MATERIALIZER_VERSION = "olist-queryspec-materializer-v1"
FAMILY_SCHEMA_VERSION = "olist-query-family-v1"
PROTECTED_SUMMARY_VERSION = "olist-protected-family-summary-v1"
_SPLITS = frozenset({"train", "validation", "in_domain_test"})
_SEED_FIELDS = frozenset(
    {
        "seed_id",
        "split",
        "metric_ids",
        "result_shape",
        "dimension",
        "time",
        "join_program_id",
        "attribution_rule_id",
    }
)
_REQUIRED_SEED_FIELDS = _SEED_FIELDS - {"attribution_rule_id"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds-jsonl", type=Path, required=True)
    parser.add_argument("--protected-summary-json", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--generated-at",
        default=None,
        help="UTC ISO-8601 timestamp; set this for byte-reproducible output.",
    )
    return parser.parse_args()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def family_fingerprint(family_id: str) -> str:
    """Return the only protected-family representation accepted by v1."""
    return sha256_bytes(f"{PROTECTED_SUMMARY_VERSION}:{family_id}".encode("utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    rows: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON at {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"seed at {path}:{line_number} must be an object")
        rows.append(value)
    if not rows:
        raise ValueError("seed input is empty")
    return rows


def load_protected_family_fingerprints(path: Path) -> frozenset[str]:
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("protected summary must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("protected summary must be an object")
    if set(value) != {"summary_version", "family_fingerprints"}:
        raise ValueError("protected summary fields do not match the v1 contract")
    if value["summary_version"] != PROTECTED_SUMMARY_VERSION:
        raise ValueError("unsupported protected summary version")
    fingerprints = value["family_fingerprints"]
    if not isinstance(fingerprints, list) or any(
        not isinstance(item, str) or len(item) != 64 for item in fingerprints
    ):
        raise ValueError("protected family fingerprints must be SHA-256 strings")
    if len(fingerprints) != len(set(fingerprints)):
        raise ValueError("protected family fingerprints must be unique")
    return frozenset(fingerprints)


def canonicalize_seed(raw: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(raw) - _SEED_FIELDS
    missing = _REQUIRED_SEED_FIELDS - set(raw)
    if unknown or missing:
        raise QuerySpecValidationError(
            "unsupported_query_feature",
            f"seed fields are invalid; unknown={sorted(unknown)}, missing={sorted(missing)}",
        )
    seed_id = raw["seed_id"]
    if not isinstance(seed_id, str) or not seed_id or len(seed_id) > 120:
        raise QuerySpecValidationError("invalid_query_spec", "seed_id must be a non-empty short string")
    split = raw["split"]
    if not isinstance(split, str) or split not in _SPLITS:
        raise QuerySpecValidationError("invalid_query_spec", "seed split is not supported")
    metric_ids = raw["metric_ids"]
    if not isinstance(metric_ids, list):
        raise QuerySpecValidationError("invalid_metric_ids", "metric_ids must be an ordered list")
    if not isinstance(raw["result_shape"], str):
        raise QuerySpecValidationError("coverage_shape_not_permitted", "result_shape must be a string")
    if raw["dimension"] is not None and not isinstance(raw["dimension"], str):
        raise QuerySpecValidationError("coverage_shape_not_permitted", "dimension must be a string or null")
    if not isinstance(raw["join_program_id"], str):
        raise QuerySpecValidationError("coverage_shape_not_permitted", "join_program_id must be a string")
    if raw.get("attribution_rule_id") is not None and not isinstance(raw.get("attribution_rule_id"), str):
        raise QuerySpecValidationError("attribution_not_frozen", "attribution_rule_id must be a string or null")
    time = raw["time"]
    if not isinstance(time, dict):
        raise QuerySpecValidationError("invalid_time_contract", "time must be an object")
    allowed_time_fields = {"mode", "start", "end_exclusive", "grain"}
    if set(time) - allowed_time_fields:
        raise QuerySpecValidationError("unsupported_query_feature", "time has unsupported fields")
    if not isinstance(time.get("mode"), str):
        raise QuerySpecValidationError("invalid_time_contract", "time.mode must be a string")
    if any(not isinstance(time.get(name), str) for name in ("start", "end_exclusive") if time.get(name) is not None):
        raise QuerySpecValidationError("invalid_time_contract", "time endpoints must be ISO date strings or null")
    if time.get("grain") is not None and not isinstance(time.get("grain"), str):
        raise QuerySpecValidationError("invalid_time_contract", "time.grain must be a string or null")
    return {
        "seed_id": seed_id,
        "split": split,
        "metric_ids": tuple(metric_ids),
        "result_shape": raw["result_shape"],
        "dimension": raw["dimension"],
        "time": QueryTime(
            mode=time.get("mode"),
            start=time.get("start"),
            end_exclusive=time.get("end_exclusive"),
            grain=time.get("grain"),
        ),
        "join_program_id": raw["join_program_id"],
        "attribution_rule_id": raw.get("attribution_rule_id"),
    }


def family_payload(spec: QuerySpec) -> dict[str, Any]:
    """Semantic identity intentionally excludes date endpoints and language."""
    return {
        "family_schema_version": FAMILY_SCHEMA_VERSION,
        "workspace": spec.workspace.as_dict(),
        "metric_ids": list(spec.metric_ids),
        "result_shape": spec.result_shape,
        "dimension": spec.dimension,
        "time_mode": spec.time.mode,
        "time_grain": spec.time.grain,
        "join_program_id": spec.join_program_id,
        "aggregation_contract": "olist-metrics-v2",
    }


def family_id(spec: QuerySpec) -> str:
    return "family_" + sha256_bytes(_canonical_json(family_payload(spec)).encode("utf-8"))[:24]


def _rejection(seed_id: str, split: str | None, reason_code: str) -> dict[str, Any]:
    return {
        "seed_id": seed_id,
        "split": split,
        "reason_code": reason_code,
        "materializer_version": MATERIALIZER_VERSION,
    }


def _validate_artifact(spec: QuerySpec) -> tuple[dict[str, Any], dict[str, Any]]:
    # Keep the explicit validator even though create_validated and renderer validate too.
    validate_query_spec(spec)
    artifact = render_gold_sql(spec)
    actual_hash = sha256_bytes(artifact.sql.encode("utf-8"))
    if actual_hash != artifact.sql_sha256 or actual_hash != artifact.evidence["sql_sha256"]:
        raise QuerySpecValidationError("renderer_hash_mismatch", "renderer SQL hash is inconsistent")
    if artifact.required_result_columns != spec.required_result_columns:
        raise QuerySpecValidationError("result_columns_do_not_match_contract", "renderer output contract drifted")
    return spec.as_dict(), {
        "query_spec_id": artifact.query_spec_id,
        "renderer_version": artifact.renderer_version,
        "sql": artifact.sql,
        "sql_sha256": artifact.sql_sha256,
        "metric_ids": list(artifact.metric_ids),
        "join_program_id": artifact.join_program_id,
        "required_result_columns": list(artifact.required_result_columns),
        "evidence": dict(artifact.evidence),
    }


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _check_output_dir(output_dir: Path) -> None:
    output_dir = output_dir.resolve()
    if output_dir.is_relative_to(ROOT):
        raise ValueError("materialized artifacts must stay outside the Git worktree")
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")


def materialize(
    seeds_jsonl: Path,
    protected_summary_json: Path,
    output_dir: Path,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Write structural QuerySpec/Gold records and an audit manifest atomically."""
    output_dir = output_dir.resolve()
    _check_output_dir(output_dir)
    raw_seeds = _read_jsonl(seeds_jsonl.resolve())
    protected_fingerprints = load_protected_family_fingerprints(protected_summary_json.resolve())
    generated_at = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    accepted: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    seen_seed_ids: set[str] = set()
    seen_query_specs: set[str] = set()
    seen_families: set[str] = set()
    seen_sql_hashes: set[str] = set()

    for raw_seed in raw_seeds:
        seed_id = str(raw_seed.get("seed_id", "unknown"))
        split = raw_seed.get("split") if isinstance(raw_seed.get("split"), str) else None
        if seed_id in seen_seed_ids:
            rejections.append(_rejection(seed_id, split, "duplicate_seed_id"))
            continue
        seen_seed_ids.add(seed_id)
        try:
            seed = canonicalize_seed(raw_seed)
            spec = QuerySpec.create_validated(
                metric_ids=seed["metric_ids"],
                result_shape=seed["result_shape"],
                dimension=seed["dimension"],
                time=seed["time"],
                join_program_id=seed["join_program_id"],
                attribution_rule_id=seed["attribution_rule_id"],
            )
            family = family_id(spec)
            if family_fingerprint(family) in protected_fingerprints:
                raise QuerySpecValidationError("protected_family_collision", "family collides with protected summary")
            if family in seen_families:
                raise QuerySpecValidationError("duplicate_family", "family appears more than once")
            query_spec, artifact = _validate_artifact(spec)
            if spec.query_spec_id in seen_query_specs:
                raise QuerySpecValidationError("duplicate_query_spec", "QuerySpec appears more than once")
            if artifact["sql_sha256"] in seen_sql_hashes:
                raise QuerySpecValidationError("duplicate_sql_artifact", "canonical SQL appears more than once")
        except QuerySpecValidationError as exc:
            rejections.append(_rejection(seed_id, split, exc.reason_code))
            continue
        seen_families.add(family)
        seen_query_specs.add(spec.query_spec_id)
        seen_sql_hashes.add(artifact["sql_sha256"])
        accepted.append(
            {
                "seed_id": seed["seed_id"],
                "split": seed["split"],
                "family_id": family,
                "sql_program_id": spec.join_program_id,
                "query_spec": query_spec,
                "gold_artifact": artifact,
            }
        )

    program_splits: dict[str, set[str]] = {}
    for row in accepted:
        program_splits.setdefault(row["sql_program_id"], set()).add(row["split"])
    crossing_programs = sorted(program for program, splits in program_splits.items() if len(splits) > 1)
    if crossing_programs:
        raise ValueError(f"SQL programs cross splits: {crossing_programs}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.parent / f".{output_dir.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        query_spec_rows = [
            {key: row[key] for key in ("seed_id", "split", "family_id", "sql_program_id", "query_spec")}
            for row in accepted
        ]
        gold_rows = [
            {key: row[key] for key in ("seed_id", "split", "family_id", "sql_program_id", "gold_artifact")}
            for row in accepted
        ]
        query_specs_path = staging / "query_specs.jsonl"
        gold_sql_path = staging / "gold_sql.jsonl"
        rejection_path = staging / "materialization_rejections.jsonl"
        _write_jsonl(query_specs_path, query_spec_rows)
        _write_jsonl(gold_sql_path, gold_rows)
        _write_jsonl(rejection_path, rejections)

        split_rows = Counter(row["split"] for row in accepted)
        split_families = {
            split: sorted(row["family_id"] for row in accepted if row["split"] == split)
            for split in _SPLITS
        }
        manifest = {
            "materializer_version": MATERIALIZER_VERSION,
            "generated_at": generated_at,
            "query_spec_schema_version": QUERY_SPEC_SCHEMA_VERSION,
            "renderer_version": RENDERER_VERSION,
            "workspace": WorkspacePin.current().as_dict(),
            "source": {
                "seeds_jsonl": str(seeds_jsonl.resolve()),
                "seeds_sha256": sha256_file(seeds_jsonl.resolve()),
                "protected_summary_json": str(protected_summary_json.resolve()),
                "protected_summary_sha256": sha256_file(protected_summary_json.resolve()),
                "protected_summary_version": PROTECTED_SUMMARY_VERSION,
            },
            "outputs": {
                "query_specs_jsonl": {"rows": len(query_spec_rows), "sha256": sha256_file(query_specs_path)},
                "gold_sql_jsonl": {"rows": len(gold_rows), "sha256": sha256_file(gold_sql_path)},
                "rejections_jsonl": {"rows": len(rejections), "sha256": sha256_file(rejection_path)},
            },
            "counts": {
                "input_seeds": len(raw_seeds),
                "accepted_rows": len(accepted),
                "query_specs": len(seen_query_specs),
                "families": len(seen_families),
                "sql_programs": len(program_splits),
                "canonical_sql_hashes": len(seen_sql_hashes),
                "rejections_by_reason": dict(sorted(Counter(row["reason_code"] for row in rejections).items())),
                "protected_family_collisions": sum(
                    row["reason_code"] == "protected_family_collision" for row in rejections
                ),
            },
            "splits": {
                split: {"rows": split_rows.get(split, 0), "family_ids": split_families[split]}
                for split in sorted(_SPLITS)
            },
            "checks": {
                "family_split_overlap": [],
                "query_spec_split_overlap": [],
                "sql_program_split_overlap": [],
                "protected_holdout_raw_read": False,
                "sql_executed": False,
                "prompt_or_question_materialized": False,
                "status": "pass" if accepted else "rejected_all",
            },
        }
        (staging / "materialization_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(output_dir)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    args = parse_args()
    result = materialize(
        args.seeds_jsonl,
        args.protected_summary_json,
        args.output_dir,
        generated_at=args.generated_at,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
