#!/usr/bin/env python3
"""Create a deterministic, schema-disjoint train/validation split externally.

The source candidate JSONL must already be train-only and execution-checked.
This script groups all samples from a Spider ``db_id`` together, so a database
schema never appears in both train and validation. It only writes derived
artifacts to the explicitly supplied external output directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates-jsonl", type=Path, required=True)
    parser.add_argument("--holdout-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--validation-ratio", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=20260825)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def holdout_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("- case_id:"):
            ids.add(line.split(":", maxsplit=1)[1].strip())
    if not ids:
        raise ValueError(f"no case IDs found in holdout manifest: {path}")
    return ids


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not rows:
        raise ValueError("candidate input is empty")
    required = {"sample_id", "split", "query_plan", "execution_outcome", "workspace_id"}
    for row in rows:
        missing = required - row.keys()
        if missing:
            raise ValueError(f"candidate {row.get('sample_id')} missing {sorted(missing)}")
        if row["split"].get("name") != "train":
            raise ValueError(f"candidate is not train-only: {row['sample_id']}")
        if row["execution_outcome"].get("sqlite_readonly_explain") != "pass":
            raise ValueError(f"candidate lacks read-only execution evidence: {row['sample_id']}")
    return rows


def db_id(row: dict[str, Any]) -> str:
    """Recover the Spider database ID from the stable candidate split group."""

    group = row["split"].get("group", "")
    database, separator, _ = group.partition(":")
    if not separator or not database:
        raise ValueError(f"candidate has invalid database group: {row['sample_id']}")
    return database


def rank_group(group: str, seed: int) -> str:
    return hashlib.sha256(f"{seed}:{group}".encode()).hexdigest()


def choose_validation_groups(
    groups: dict[str, list[dict[str, Any]]], validation_ratio: float, seed: int
) -> set[str]:
    total_rows = sum(len(rows) for rows in groups.values())
    target_rows = max(1, round(total_rows * validation_ratio))
    selected: set[str] = set()
    selected_rows = 0
    for group in sorted(groups, key=lambda item: rank_group(item, seed)):
        if selected_rows >= target_rows:
            break
        selected.add(group)
        selected_rows += len(groups[group])
    if len(selected) == len(groups):
        raise ValueError("validation split selected every group")
    return selected


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    args = parse_args()
    if not 0 < args.validation_ratio < 0.5:
        raise ValueError("--validation-ratio must be between 0 and 0.5")
    rows = load_rows(args.candidates_jsonl)
    forbidden_ids = holdout_ids(args.holdout_manifest)
    collisions = sorted({row["sample_id"] for row in rows}.intersection(forbidden_ids))
    if collisions:
        raise ValueError(f"holdout collision: {collisions[:5]}")

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[db_id(row)].append(row)
    validation_groups = choose_validation_groups(groups, args.validation_ratio, args.seed)
    train_rows = [row for row in rows if db_id(row) not in validation_groups]
    validation_rows = [row for row in rows if db_id(row) in validation_groups]
    if not train_rows or not validation_rows:
        raise AssertionError("both splits must be non-empty")

    for row in train_rows:
        row["split"] = {**row["split"], "name": "train"}
    for row in validation_rows:
        row["split"] = {**row["split"], "name": "validation"}

    train_db_ids = {db_id(row) for row in train_rows}
    validation_db_ids = {db_id(row) for row in validation_rows}
    train_shapes = {row["query_plan"].get("sql_shape") for row in train_rows}
    validation_shapes = {row["query_plan"].get("sql_shape") for row in validation_rows}
    if train_db_ids.intersection(validation_db_ids):
        raise AssertionError("schema/db groups overlap between train and validation")
    if train_shapes.intersection(validation_shapes):
        raise AssertionError("SQL shapes overlap between train and validation")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    train_path = args.output_dir / "train.jsonl"
    validation_path = args.output_dir / "validation.jsonl"
    audit_path = args.output_dir / "split_audit.json"
    write_jsonl(train_path, train_rows)
    write_jsonl(validation_path, validation_rows)
    generated_at = args.generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    audit = {
        "generated_at": generated_at,
        "generator": "scripts/split_post_training_candidates.py",
        "generator_version": "1",
        "source": {
            "candidates_jsonl": str(args.candidates_jsonl),
            "candidates_sha256": sha256_file(args.candidates_jsonl),
            "holdout_manifest": str(args.holdout_manifest),
        },
        "policy": {
            "seed": args.seed,
            "validation_ratio_requested": args.validation_ratio,
            "primary_group": "spider_db_id",
            "secondary_overlap_check": "normalized_sql_shape",
            "v2_holdout_case_collisions": collisions,
        },
        "splits": {
            "train": {
                "rows": len(train_rows),
                "database_groups": len(train_db_ids),
                "sha256": sha256_file(train_path),
            },
            "validation": {
                "rows": len(validation_rows),
                "database_groups": len(validation_db_ids),
                "sha256": sha256_file(validation_path),
            },
        },
        "checks": {
            "train_validation_database_overlap": [],
            "train_validation_sql_shape_overlap": [],
            "v2_holdout_used": False,
            "raw_data_in_git": False,
            "status": "pass",
        },
    }
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
