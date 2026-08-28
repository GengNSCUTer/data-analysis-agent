#!/usr/bin/env python3
"""Build external, schema-stratified Spider smoke inputs without exposing gold SQL.

The model-facing file contains only ``db_id`` and ``question``. A separate,
external audit file keeps the matching gold query for post-generation analysis.
Never pass the audit file to a generation command.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import random
import sys
from typing import Any, Mapping, Sequence

from data_analysis_agent.external_artifacts import ensure_path_outside_repository


ROOT = Path(__file__).resolve().parents[3]


class SpiderSmokeSubsetError(ValueError):
    """Raised when a Spider smoke subset cannot preserve its evaluation boundary."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_case_list(path: Path) -> list[Mapping[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SpiderSmokeSubsetError(f"Spider cases do not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SpiderSmokeSubsetError(f"Spider cases are not valid JSON: {path}") from exc
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        raise SpiderSmokeSubsetError("Spider cases must be a JSON list of objects")
    return raw


def select_schema_stratified_indices(
    cases: Sequence[Mapping[str, Any]],
    *,
    per_schema: int,
    seed: int,
    exclude_prefix: int,
    exclude_prefix_schemas: bool,
) -> list[int]:
    """Select up to ``per_schema`` unseen rows from each schema without reading gold SQL."""

    if per_schema <= 0:
        raise SpiderSmokeSubsetError("per_schema must be positive")
    if exclude_prefix < 0:
        raise SpiderSmokeSubsetError("exclude_prefix cannot be negative")
    prefix_schemas: set[str] = set()
    groups: dict[str, list[int]] = defaultdict(list)
    for index, case in enumerate(cases):
        database_id = case.get("db_id")
        question = case.get("question")
        if not isinstance(database_id, str) or not database_id.strip():
            raise SpiderSmokeSubsetError(f"case {index} has no db_id")
        if not isinstance(question, str) or not question.strip():
            raise SpiderSmokeSubsetError(f"case {index} has no question")
        normalized_database_id = database_id.strip()
        if index < exclude_prefix:
            prefix_schemas.add(normalized_database_id)
        elif not exclude_prefix_schemas or normalized_database_id not in prefix_schemas:
            groups[normalized_database_id].append(index)
    if not groups:
        raise SpiderSmokeSubsetError("no eligible cases remain after exclude_prefix")

    selected: list[int] = []
    for database_id in sorted(groups):
        candidates = list(groups[database_id])
        random.Random(f"{seed}:{database_id}").shuffle(candidates)
        selected.extend(candidates[:per_schema])
    return sorted(selected)


def model_facing_case(case: Mapping[str, Any]) -> dict[str, str]:
    """Copy only the fields needed by generation; never write a dev gold query here."""

    database_id = case.get("db_id")
    question = case.get("question")
    if not isinstance(database_id, str) or not database_id.strip():
        raise SpiderSmokeSubsetError("selected case has no db_id")
    if not isinstance(question, str) or not question.strip():
        raise SpiderSmokeSubsetError("selected case has no question")
    return {"db_id": database_id.strip(), "question": question}


def audit_case(case: Mapping[str, Any], *, source_index: int) -> dict[str, Any]:
    """Build an external post-generation record that retains the gold query only for audit."""

    database_id = case.get("db_id")
    query = case.get("query")
    if not isinstance(database_id, str) or not database_id.strip():
        raise SpiderSmokeSubsetError("selected audit case has no db_id")
    if not isinstance(query, str) or not query.strip():
        raise SpiderSmokeSubsetError("selected audit case has no Spider gold query")
    return {"source_index": source_index, "db_id": database_id.strip(), "query": query}


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--per-schema", type=int, default=10)
    parser.add_argument("--exclude-prefix", type=int, default=0)
    parser.add_argument(
        "--exclude-prefix-schemas",
        action="store_true",
        help="Exclude every schema observed in the source prefix, not only its rows.",
    )
    parser.add_argument("--seed", type=int, default=20260826)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cases_path = ensure_path_outside_repository(args.cases, ROOT)
        output_dir = ensure_path_outside_repository(args.output_dir, ROOT)
        if output_dir.exists() and any(output_dir.iterdir()):
            raise SpiderSmokeSubsetError("output directory must be absent or empty")
        cases = load_case_list(cases_path)
        selected_indices = select_schema_stratified_indices(
            cases,
            per_schema=args.per_schema,
            seed=args.seed,
            exclude_prefix=args.exclude_prefix,
            exclude_prefix_schemas=args.exclude_prefix_schemas,
        )
        model_cases = [model_facing_case(cases[index]) for index in selected_indices]
        audit_cases = [audit_case(cases[index], source_index=index) for index in selected_indices]
        output_dir.mkdir(parents=True, exist_ok=True)
        generation_path = output_dir / "generation_cases.json"
        audit_path = output_dir / "audit_cases.json"
        write_json(generation_path, model_cases)
        write_json(audit_path, audit_cases)
        schema_counts = {
            database_id: sum(case["db_id"] == database_id for case in model_cases)
            for database_id in sorted({case["db_id"] for case in model_cases})
        }
        manifest = {
            "manifest_version": "1",
            "dataset": "spider_dev",
            "dataset_version": args.dataset_version,
            "source_cases_sha256": sha256_file(cases_path),
            "generation_cases_sha256": sha256_file(generation_path),
            "audit_cases_sha256": sha256_file(audit_path),
            "selection": {
                "strategy": "schema_stratified_independent_smoke",
                "seed": args.seed,
                "per_schema_requested": args.per_schema,
                "exclude_prefix": args.exclude_prefix,
                "exclude_prefix_schemas": args.exclude_prefix_schemas,
                "source_case_count": len(cases),
                "selected_case_count": len(selected_indices),
                "selected_schema_count": len(schema_counts),
                "schema_counts": schema_counts,
            },
            "boundaries": {
                "model_facing_file": generation_path.name,
                "model_facing_file_contains_gold_sql": False,
                "gold_audit_file": audit_path.name,
                "gold_audit_file_for_post_generation_only": True,
                "raw_assets_outside_git": True,
            },
        }
        manifest_path = output_dir / "subset_manifest.json"
        write_json(manifest_path, manifest)
    except (SpiderSmokeSubsetError, ValueError) as exc:
        print(f"Spider smoke subset error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
