#!/usr/bin/env python3
"""Build isolated CSpider official-split SFT records outside the repository.

The CSpider full release already provides schema-disjoint train, dev, and test
splits.  This command preserves that assignment: train is the only output
eligible for parameter updates, dev becomes validation, and test is physically
separated as final-evaluation-only data.  It never starts a tokenizer or model.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from data_analysis_agent.spider_sft_format import (
    PROMPT_FORMAT_VERSION_V2,
    normalize_question,
    render_sft_training_text,
    serialize_spider_schema_for_version,
)
from scripts.post_training.data.build_spider_sft_candidates import (
    normalized_sql_shape,
    read_only_explain,
    sql_feature_flags,
)


SOURCE_SPLITS = (
    ("train", "train.json", "tables.json", "database", "train"),
    ("dev", "dev.json", "tables.json", "database", "validation"),
    ("test", "test_data/test.json", "test_data/tables_test.json", "test_database", "test"),
)
EXPECTED_ROLES = {
    "train": "parameter_updates",
    "dev": "validation_only",
    "test": "final_evaluation_only",
}


class CSpiderBuildError(ValueError):
    """Raised when CSpider source evidence cannot produce trusted SFT records."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extracted-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--prompt-format-version",
        default=PROMPT_FORMAT_VERSION_V2,
        help="Shared schema/question/SQL format used by later SFT and inference.",
    )
    parser.add_argument(
        "--generated-at",
        default=None,
        help="UTC ISO-8601 timestamp; set this for byte-reproducible JSONL output.",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "acquisition-manifest.json":
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def load_json_list(path: Path, name: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CSpiderBuildError(f"{name} is invalid JSON: {path}") from exc
    if not isinstance(value, list) or not value:
        raise CSpiderBuildError(f"{name} must be a non-empty JSON list")
    if not all(isinstance(item, dict) for item in value):
        raise CSpiderBuildError(f"{name} contains a non-object item")
    return value


def source_manifest(root: Path) -> dict[str, Any]:
    path = root / "acquisition-manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise CSpiderBuildError(f"CSpider acquisition manifest is unavailable: {path}") from exc
    if manifest.get("dataset", {}).get("id") != "cspider":
        raise CSpiderBuildError("acquisition manifest is not for CSpider")
    if manifest.get("dataset", {}).get("release") != "full-2024-03-01":
        raise CSpiderBuildError("unexpected CSpider release")
    if manifest.get("extraction", {}).get("tree_sha256") != tree_sha256(root):
        raise CSpiderBuildError("extracted CSpider tree does not match acquisition manifest")
    source_files = manifest.get("source_files")
    if not isinstance(source_files, dict):
        raise CSpiderBuildError("acquisition manifest lacks source file hashes")
    for relative, expected_hash in source_files.items():
        path = root / str(relative)
        if not path.is_file() or sha256_file(path) != expected_hash:
            raise CSpiderBuildError(f"source file hash mismatch: {relative}")
    return manifest


def source_split_contract(manifest: Mapping[str, Any], source_split: str) -> Mapping[str, Any]:
    splits = manifest.get("splits")
    if not isinstance(splits, Mapping):
        raise CSpiderBuildError("acquisition manifest lacks split contracts")
    contract = splits.get(source_split)
    if not isinstance(contract, Mapping):
        raise CSpiderBuildError(f"acquisition manifest lacks {source_split} contract")
    if contract.get("role") != EXPECTED_ROLES[source_split]:
        raise CSpiderBuildError(f"unexpected {source_split} role in acquisition manifest")
    if contract.get("forbidden_for_training") is not (source_split != "train"):
        raise CSpiderBuildError(f"unexpected {source_split} training boundary")
    return contract


def database_ids(rows: list[Mapping[str, Any]], split_name: str) -> set[str]:
    ids: set[str] = set()
    for index, row in enumerate(rows):
        required = {"db_id", "question", "query"} - row.keys()
        if required:
            raise CSpiderBuildError(f"{split_name}[{index}] misses fields: {sorted(required)}")
        db_id = row["db_id"]
        if not isinstance(db_id, str) or not db_id:
            raise CSpiderBuildError(f"{split_name}[{index}] has invalid db_id")
        if not isinstance(row["question"], str) or not isinstance(row["query"], str):
            raise CSpiderBuildError(f"{split_name}[{index}] has non-string question or query")
        ids.add(db_id)
    return ids


def table_index(path: Path, split_name: str) -> dict[str, dict[str, Any]]:
    tables = load_json_list(path, f"{split_name} table metadata")
    indexed: dict[str, dict[str, Any]] = {}
    for item in tables:
        db_id = item.get("db_id")
        if not isinstance(db_id, str) or not db_id or db_id in indexed:
            raise CSpiderBuildError(f"{split_name} table metadata has an invalid or duplicate db_id")
        indexed[db_id] = item
    return indexed


def split_group(db_id: str) -> str:
    return f"{db_id}:{hashlib.sha256(db_id.encode('utf-8')).hexdigest()[:12]}"


def build_rows(
    *,
    source_split: str,
    trainer_split: str,
    rows: list[dict[str, Any]],
    tables: Mapping[str, dict[str, Any]],
    database_root: Path,
    prompt_format_version: str,
    generated_at: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
    built: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    execution_summary: Counter[str] = Counter()
    for index, item in enumerate(rows):
        db_id = str(item["db_id"])
        table = tables.get(db_id)
        if table is None:
            raise CSpiderBuildError(f"{source_split}[{index}] db_id lacks table metadata: {db_id}")
        database_path = database_root / db_id / f"{db_id}.sqlite"
        if not database_path.is_file():
            raise CSpiderBuildError(f"{source_split}[{index}] database is missing: {database_path}")

        schema = serialize_spider_schema_for_version(table, prompt_format_version)
        question = normalize_question(str(item["question"]))
        sql = str(item["query"]).strip()
        if not sql:
            raise CSpiderBuildError(f"{source_split}[{index}] has an empty SQL target")
        execution = read_only_explain(database_path, sql)
        execution_status = str(execution.get("sqlite_readonly_explain"))
        execution_summary[execution_status] += 1
        if execution_status != "pass":
            # Keep flawed public gold targets out of parameter updates while
            # retaining enough source evidence to audit the exclusion.
            exclusions.append(
                {
                    "sample_id": f"cspider_{source_split}:{index:05d}",
                    "source": "cspider_full_2024",
                    "source_split": source_split,
                    "db_id": db_id,
                    "question_redacted": question,
                    "candidate_sql": sql,
                    "execution_outcome": execution,
                    "exclusion": {
                        "reason": "sqlite_readonly_explain_failed",
                        "eligible_for_sft": False,
                    },
                }
            )
            continue
        sql_shape = normalized_sql_shape(sql)
        features = sql_feature_flags(sql)
        built.append(
            {
                "sample_id": f"cspider_{source_split}:{index:05d}",
                "source": "cspider_full_2024",
                "license": "CC BY-SA 4.0 (CSpider task site)",
                "timestamp": generated_at,
                "workspace_id": "cspider_research",
                "catalog_snapshot": "cspider-schema-v2",
                "role_scope": "sqlite-readonly-research",
                "question_redacted": question,
                "working_memory": {},
                "target_route": {"intent": "data_query", "requires_database": True},
                "query_plan": {"sql_shape": sql_shape, "sql_features": features},
                "candidate_sql": sql,
                "execution_outcome": execution,
                "review": {
                    "semantic_correct": True,
                    "review_type": "public_dataset_gold",
                    "reviewer": "cspider_official_gold",
                },
                "label_provenance": "public_dataset_gold",
                "split": {
                    "name": trainer_split,
                    "official_name": source_split,
                    "group": split_group(db_id),
                },
                "prompt_format_version": prompt_format_version,
                "schema_text": schema,
                "training_text": render_sft_training_text(
                    question, schema, sql, prompt_format_version
                ),
            }
        )
    return built, exclusions, execution_summary


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def build_splits(
    extracted_dir: Path,
    output_dir: Path,
    *,
    prompt_format_version: str = PROMPT_FORMAT_VERSION_V2,
    generated_at: str | None = None,
) -> dict[str, Any]:
    extracted_dir = extracted_dir.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    manifest = source_manifest(extracted_dir)
    generated_at = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    source_rows: dict[str, list[dict[str, Any]]] = {}
    source_ids: dict[str, set[str]] = {}
    source_tables: dict[str, dict[str, dict[str, Any]]] = {}
    for source_split, records_path, tables_path, _, _ in SOURCE_SPLITS:
        contract = source_split_contract(manifest, source_split)
        records = load_json_list(extracted_dir / records_path, source_split)
        ids = database_ids(records, source_split)
        if len(records) != contract.get("record_count") or len(ids) != contract.get("database_count"):
            raise CSpiderBuildError(f"{source_split} count differs from acquisition manifest")
        source_rows[source_split] = records
        source_ids[source_split] = ids
        source_tables[source_split] = table_index(extracted_dir / tables_path, source_split)

    for left, right in (("train", "dev"), ("train", "test"), ("dev", "test")):
        overlap = sorted(source_ids[left].intersection(source_ids[right]))
        if overlap:
            raise CSpiderBuildError(f"official schema overlap between {left} and {right}: {overlap[:5]}")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = output_dir.parent / f".{output_dir.name}.staging-{uuid.uuid4().hex}"
    try:
        staging_dir.mkdir()
        built_splits: dict[str, list[dict[str, Any]]] = {}
        source_quality_exclusions: dict[str, list[dict[str, Any]]] = {}
        execution_summaries: dict[str, Counter[str]] = {}
        for source_split, _, _, database_relative, trainer_split in SOURCE_SPLITS:
            built, exclusions, execution = build_rows(
                source_split=source_split,
                trainer_split=trainer_split,
                rows=source_rows[source_split],
                tables=source_tables[source_split],
                database_root=extracted_dir / database_relative,
                prompt_format_version=prompt_format_version,
                generated_at=generated_at,
            )
            built_splits[source_split] = built
            source_quality_exclusions[source_split] = exclusions
            execution_summaries[source_split] = execution

        train_path = staging_dir / "train.jsonl"
        validation_path = staging_dir / "validation.jsonl"
        test_path = staging_dir / "final_evaluation_only/test.jsonl"
        exclusions_dir = staging_dir / "source_quality_exclusions"
        test_path.parent.mkdir()
        exclusions_dir.mkdir()
        write_jsonl(train_path, built_splits["train"])
        write_jsonl(validation_path, built_splits["dev"])
        write_jsonl(test_path, built_splits["test"])
        exclusion_paths: dict[str, Path] = {}
        for source_split, exclusions in source_quality_exclusions.items():
            if exclusions:
                path = exclusions_dir / f"{source_split}.jsonl"
                write_jsonl(path, exclusions)
                exclusion_paths[source_split] = path
        audit = {
            "audit_version": "1",
            "generated_at": generated_at,
            "generator": "scripts/post_training/data/build_cspider_sft_splits.py",
            "source": {
                "extracted_dir": str(extracted_dir),
                "acquisition_manifest_sha256": sha256_file(
                    extracted_dir / "acquisition-manifest.json"
                ),
                "extracted_tree_sha256": tree_sha256(extracted_dir),
                "dataset": manifest["dataset"],
            },
            "prompt": {
                "format_version": prompt_format_version,
                "database_rows_or_values_included": False,
                "token_budget_enforced": False,
                "token_budget_note": "No tokenizer was loaded during data construction.",
            },
            "policy": {
                "split_strategy": "official_cspider_train_dev_test",
                "primary_group": "cspider_db_id",
                "test_storage": "final_evaluation_only",
                "test_forbidden_for_training": True,
            },
            "splits": {
                "train": {
                    "rows": len(built_splits["train"]),
                    "source_rows": len(source_rows["train"]),
                    "database_groups": len(source_ids["train"]),
                    "sha256": sha256_file(train_path),
                    "official_split": "train",
                    "role": "parameter_updates",
                },
                "validation": {
                    "rows": len(built_splits["dev"]),
                    "source_rows": len(source_rows["dev"]),
                    "database_groups": len(source_ids["dev"]),
                    "sha256": sha256_file(validation_path),
                    "official_split": "dev",
                    "role": "validation_only",
                },
                "test": {
                    "rows": len(built_splits["test"]),
                    "source_rows": len(source_rows["test"]),
                    "database_groups": len(source_ids["test"]),
                    "sha256": sha256_file(test_path),
                    "official_split": "test",
                    "role": "final_evaluation_only",
                    "forbidden_for_training": True,
                },
            },
            "checks": {
                "train_validation_database_overlap": [],
                "train_test_database_overlap": [],
                "validation_test_database_overlap": [],
                "sqlite_readonly_explain": {
                    split: dict(sorted(summary.items())) for split, summary in execution_summaries.items()
                },
                "source_quality_exclusions": {
                    split: {
                        "rows": len(exclusions),
                        "sha256": sha256_file(exclusion_paths[split]),
                        "reason": "sqlite_readonly_explain_failed",
                    }
                    for split, exclusions in source_quality_exclusions.items()
                    if exclusions
                },
                "raw_data_in_git": False,
                "status": "pass",
            },
            "outputs": {
                "train_jsonl": str(output_dir / "train.jsonl"),
                "validation_jsonl": str(output_dir / "validation.jsonl"),
                "test_jsonl": str(output_dir / "final_evaluation_only/test.jsonl"),
            },
        }
        (staging_dir / "split_audit.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging_dir.replace(output_dir)
        return audit
    except Exception:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        raise


def main() -> int:
    args = parse_args()
    audit = build_splits(
        args.extracted_dir,
        args.output_dir,
        prompt_format_version=args.prompt_format_version,
        generated_at=args.generated_at,
    )
    print(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
