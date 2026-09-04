#!/usr/bin/env python3
"""Assemble fully evidenced Olist Pilot v1 Gold admissions outside Git.

This command turns several bounded admission batches into one complete source
for runtime-Prompt materialization. It does not render prompts, tokenize rows,
or train a model. A record with an advisory ``needs_human_review`` verdict can
enter only through an external, explicit manual decision bound to its Gold SQL
hash and QuerySpec ID.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sys
import uuid
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.olist_queryspec import WorkspacePin  # noqa: E402


ASSEMBLY_VERSION = "olist-pilot-v1-admission-assembly-v1"
MANUAL_REVIEW_VERSION = "olist-pilot-v1-manual-review-v1"
EXPECTED_ROWS = 40
_ALLOWED_DECISIONS = frozenset({"approved", "excluded"})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admission-dir", action="append", type=Path, required=True)
    parser.add_argument("--manual-review-json", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _external_existing(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError(f"{label} must stay outside the Git worktree")
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved


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
        raise ValueError("assembly output must stay outside the Git worktree")
    if resolved.exists():
        raise FileExistsError(resolved)
    return resolved


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} has invalid JSON at line {number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{label} line {number} must be an object")
        rows.append(row)
    if not rows:
        raise ValueError(f"{label} is empty")
    return rows


def load_manual_reviews(path: Path) -> dict[str, dict[str, str]]:
    payload = _read_json(_external_existing(path, "manual review"), "manual review")
    if set(payload) != {"review_version", "workspace", "decisions"}:
        raise ValueError("manual review has unsupported fields")
    if payload["review_version"] != MANUAL_REVIEW_VERSION:
        raise ValueError("unsupported manual review version")
    if payload["workspace"] != WorkspacePin.current().as_dict():
        raise ValueError("manual review workspace differs from the current pin")
    decisions = payload["decisions"]
    if not isinstance(decisions, list):
        raise ValueError("manual review decisions must be a list")
    result: dict[str, dict[str, str]] = {}
    fields = {"seed_id", "query_spec_id", "gold_sql_sha256", "decision", "rationale"}
    for item in decisions:
        if not isinstance(item, Mapping) or set(item) != fields:
            raise ValueError("manual review decision has unsupported fields")
        seed_id = item["seed_id"]
        if not isinstance(seed_id, str) or not seed_id or seed_id in result:
            raise ValueError("manual review seed IDs must be non-empty and unique")
        if item["decision"] not in _ALLOWED_DECISIONS:
            raise ValueError("manual review has an unsupported decision")
        if not all(isinstance(item[name], str) and item[name].strip() for name in fields - {"decision"}):
            raise ValueError("manual review fields must be non-empty strings")
        result[seed_id] = dict(item)
    return result


def load_admission_batch(directory: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    directory = _external_existing_dir(directory, "admission directory")
    aggregate_path = directory / "admission_aggregate.json"
    records_path = directory / "admission_records.jsonl"
    aggregate = _read_json(aggregate_path, "admission aggregate")
    records = _read_jsonl(records_path, "admission records")
    if aggregate.get("workspace") != WorkspacePin.current().as_dict():
        raise ValueError("admission workspace differs from the current pin")
    output = aggregate.get("output")
    if not isinstance(output, Mapping):
        raise ValueError("admission aggregate has no output evidence")
    details = output.get("admission_records_jsonl")
    if not isinstance(details, Mapping):
        raise ValueError("admission aggregate has no records hash")
    if details.get("rows") != len(records) or details.get("sha256") != sha256_file(records_path):
        raise ValueError("admission record evidence does not match aggregate")
    selection = aggregate.get("selection")
    if not isinstance(selection, Mapping) or selection.get("source_materialized_rows") != EXPECTED_ROWS:
        raise ValueError("admission batch does not bind to the complete Pilot v1 source")
    selected = selection.get("selected_seed_ids")
    if not isinstance(selected, list) or [str(row.get("seed_id")) for row in records] != selected:
        raise ValueError("admission batch selected IDs do not match its records")
    return aggregate, records


def normalize_records(
    batches: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    reviews: Mapping[str, Mapping[str, str]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for aggregate, batch in batches:
        del aggregate
        for record in batch:
            seed_id = record.get("seed_id")
            if not isinstance(seed_id, str) or seed_id in seen:
                raise ValueError("admission records have duplicate or missing seed IDs")
            seen.add(seed_id)
            status = record.get("admission_status")
            review = reviews.get(seed_id)
            if status == "admitted":
                if review is not None:
                    raise ValueError("manual review may only resolve needs_human_review records")
            elif status == "needs_human_review":
                if review is None:
                    raise ValueError(f"needs_human_review record lacks manual decision: {seed_id}")
                if review["query_spec_id"] != record.get("query_spec", {}).get("query_spec_id"):
                    raise ValueError("manual review QuerySpec ID does not match admission record")
                if review["gold_sql_sha256"] != record.get("gold_sql_sha256"):
                    raise ValueError("manual review Gold SQL hash does not match admission record")
                if review["decision"] != "approved":
                    raise ValueError(f"manual review excluded required Pilot v1 record: {seed_id}")
            else:
                raise ValueError(f"admission record is not eligible: {seed_id} ({status})")
            records.append({**record, "admission_status": "admitted", "manual_review": review})
    unused = sorted(set(reviews) - seen)
    if unused:
        raise ValueError(f"manual review contains unknown seed IDs: {unused}")
    if len(records) != EXPECTED_ROWS:
        raise ValueError(f"Pilot v1 requires exactly {EXPECTED_ROWS} admitted records, got {len(records)}")
    splits = {"train": 0, "validation": 0, "in_domain_test": 0}
    for record in records:
        split = record.get("split")
        if split not in splits:
            raise ValueError("admission record has unsupported split")
        splits[split] += 1
    if splits != {"train": 24, "validation": 8, "in_domain_test": 8}:
        raise ValueError(f"Pilot v1 split counts are invalid: {splits}")
    return records


def assemble(
    admission_dirs: list[Path],
    manual_review_json: Path,
    output_dir: Path,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if not admission_dirs:
        raise ValueError("at least one admission directory is required")
    output_dir = _external_new_dir(output_dir)
    reviews = load_manual_reviews(manual_review_json)
    batches = [load_admission_batch(directory) for directory in admission_dirs]
    records = normalize_records(batches, reviews)
    generated_at = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.parent / f".{output_dir.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        records_path = staging / "admitted_records.jsonl"
        with records_path.open("x", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        manifest = {
            "assembly_version": ASSEMBLY_VERSION,
            "generated_at": generated_at,
            "workspace": WorkspacePin.current().as_dict(),
            "input": {
                "admission_aggregates": [
                    {
                        "path": str(directory.resolve() / "admission_aggregate.json"),
                        "sha256": sha256_file(directory.resolve() / "admission_aggregate.json"),
                    }
                    for directory in admission_dirs
                ],
                "manual_review_sha256": sha256_file(_external_existing(manual_review_json, "manual review")),
            },
            "counts": {
                "admitted_records": len(records),
                "manual_review_approvals": len(reviews),
                "splits": {split: sum(record["split"] == split for record in records) for split in ("train", "validation", "in_domain_test")},
            },
            "checks": {
                "full_source_verified_per_batch": True,
                "all_records_admitted_or_manually_approved": True,
                "prompt_or_question_materialized": False,
                "tokenizer_loaded": False,
                "gpu_used": False,
                "protected_holdout_raw_read": False,
                "status": "pass",
            },
            "output": {"admitted_records_jsonl": {"rows": len(records), "sha256": sha256_file(records_path)}},
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
    result = assemble(
        args.admission_dir,
        args.manual_review_json,
        args.output_dir,
        generated_at=args.generated_at,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
