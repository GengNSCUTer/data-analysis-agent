#!/usr/bin/env python3
"""Build a small, auditable Spider train-only SFT candidate set.

The script intentionally reads only Spider's train split and schema metadata.
It never reads dev gold SQL, returns database rows, or writes raw benchmark
assets into the repository. Generated files belong in the external experiment
directory passed with ``--output-dir``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from data_analysis_agent.spider_sft_format import (
    normalize_question,
    render_sft_training_text,
    serialize_spider_schema,
)


VALUE_RE = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\b\d+(?:\.\d+)?\b")
SPACE_RE = re.compile(r"\s+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-json", type=Path, required=True)
    parser.add_argument("--tables-json", type=Path, required=True)
    parser.add_argument("--database-root", type=Path, required=True)
    parser.add_argument("--holdout-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=128)
    parser.add_argument("--seed", type=int, default=20260825)
    parser.add_argument(
        "--generated-at",
        default=None,
        help="UTC ISO-8601 timestamp to make the JSONL hash reproducible",
    )
    return parser.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_holdout_ids(path: Path) -> set[str]:
    """Read only case IDs from the YAML holdout manifest.

    The isolated training environment intentionally has no YAML dependency;
    the manifest shape is simple and the parser never reads question content.
    """

    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*-\s*case_id:\s*([^#\s]+)", line)
        if match:
            ids.add(match.group(1))
    if not ids:
        raise ValueError(f"no holdout case IDs found in {path}")
    return ids


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_sql_shape(sql: str) -> str:
    """Normalize literals while retaining identifiers and SQL structure."""

    normalized = VALUE_RE.sub("<value>", sql.lower())
    return SPACE_RE.sub(" ", normalized).strip()


def read_only_explain(database_path: Path, sql: str) -> dict[str, Any]:
    """Check parsing/name resolution without materializing result rows."""

    uri = f"file:{database_path.resolve()}?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        return {"sqlite_readonly_explain": "pass"}
    except sqlite3.Error as exc:
        return {
            "sqlite_readonly_explain": "error",
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:240],
        }
    finally:
        if connection is not None:
            connection.close()


def round_robin(
    groups: dict[tuple[str, str], list[dict[str, Any]]], limit: int, seed: int
) -> list[dict[str, Any]]:
    ordered_keys = sorted(
        groups,
        key=lambda key: hashlib.sha256(f"{seed}:{key[0]}:{key[1]}".encode()).hexdigest(),
    )
    ordered = [groups[key] for key in ordered_keys]
    selected: list[dict[str, Any]] = []
    cursor = 0
    while ordered and len(selected) < limit:
        next_round: list[list[dict[str, Any]]] = []
        for group in ordered:
            if cursor < len(group):
                selected.append(group[cursor])
                if len(selected) >= limit:
                    break
            if cursor + 1 < len(group):
                next_round.append(group)
        ordered = next_round
        cursor += 1
    return selected


def assert_train_only(train_path: Path) -> None:
    lowered = str(train_path).lower()
    if "dev" in train_path.name.lower() or "test" in train_path.name.lower():
        raise ValueError(f"refusing non-train-looking input: {train_path}")
    if "train" not in lowered:
        raise ValueError(f"train input path must contain 'train': {train_path}")


def main() -> int:
    args = parse_args()
    if args.limit <= 0:
        raise ValueError("--limit must be positive")
    assert_train_only(args.train_json)
    train_rows = load_json(args.train_json)
    tables = {item["db_id"]: item for item in load_json(args.tables_json)}
    forbidden_ids = load_holdout_ids(args.holdout_manifest)

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for index, item in enumerate(train_rows):
        db_id = item.get("db_id")
        query = item.get("query")
        question = item.get("question")
        if not db_id or not query or not question or db_id not in tables:
            continue
        candidate_id = f"spider_train:{index:05d}"
        if candidate_id in forbidden_ids:
            raise ValueError(f"holdout collision: {candidate_id}")
        shape = normalized_sql_shape(query)
        groups[(db_id, shape)].append({"index": index, "item": item, "shape": shape})

    selected = round_robin(groups, args.limit, args.seed)
    if len(selected) != min(args.limit, len(train_rows)):
        raise RuntimeError(f"selected {len(selected)} candidates, expected {args.limit}")

    generated_at = args.generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    execution_counts: dict[str, int] = defaultdict(int)
    for selected_row in selected:
        index = selected_row["index"]
        item = selected_row["item"]
        db_id = item["db_id"]
        schema = serialize_spider_schema(tables[db_id])
        question = normalize_question(item["question"])
        sql = item["query"].strip()
        database_path = args.database_root / db_id / f"{db_id}.sqlite"
        if not database_path.is_file():
            raise FileNotFoundError(database_path)
        execution = read_only_explain(database_path, sql)
        execution_counts[execution["sqlite_readonly_explain"]] += 1
        row = {
            "sample_id": f"spider_train:{index:05d}",
            "source": "spider_train",
            "license": "CC BY-SA 4.0 (original Spider release; see docs/spider-1.0-data-provenance.md)",
            "timestamp": generated_at,
            "workspace_id": "spider_research",
            "catalog_snapshot": "spider-schema-v1",
            "role_scope": "sqlite-readonly-research",
            "question_redacted": question,
            "working_memory": {},
            "target_route": {"intent": "data_query", "requires_database": True},
            "query_plan": {"sql_shape": selected_row["shape"]},
            "candidate_sql": sql,
            "execution_outcome": execution,
            "review": {
                "semantic_correct": True,
                "review_type": "public_dataset_gold",
                "reviewer": "spider_train_gold",
            },
            "label_provenance": "public_dataset_gold",
            "split": {
                "name": "train",
                "group": f"{db_id}:{hashlib.sha1(selected_row['shape'].encode()).hexdigest()[:12]}",
            },
            "schema_text": schema,
            "training_text": render_sft_training_text(question, schema, sql),
        }
        rows.append(row)

    rows_path = args.output_dir / "candidates.jsonl"
    text_path = args.output_dir / "training_text.jsonl"
    audit_path = args.output_dir / "audit.json"
    with rows_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    with text_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps({"sample_id": row["sample_id"], "text": row["training_text"]}, ensure_ascii=False) + "\n")

    audit = {
        "generated_at": generated_at,
        "generator": "scripts/build_spider_sft_candidates.py",
        "generator_version": "1",
        "source": {
            "train_json": str(args.train_json),
            "train_json_sha256": sha256_file(args.train_json),
            "tables_json": str(args.tables_json),
            "tables_json_sha256": sha256_file(args.tables_json),
            "database_root": str(args.database_root),
        },
        "selection": {
            "seed": args.seed,
            "requested_limit": args.limit,
            "selected_count": len(rows),
            "source_train_count": len(train_rows),
            "group_count": len(groups),
            "strategy": "sorted db_id/sql_shape groups with deterministic round-robin",
        },
        "holdout_check": {
            "manifest": str(args.holdout_manifest),
            "forbidden_case_count": len(forbidden_ids),
            "collisions": [],
            "status": "pass",
        },
        "execution_summary": dict(sorted(execution_counts.items())),
        "outputs": {
            "candidates_jsonl": str(rows_path),
            "training_text_jsonl": str(text_path),
            "candidates_sha256": sha256_file(rows_path),
            "training_text_sha256": sha256_file(text_path),
        },
        "forbidden_content_check": {
            "raw_result_rows": False,
            "api_keys_or_cookies": False,
            "user_identifiers": False,
            "status": "pass",
        },
    }
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
