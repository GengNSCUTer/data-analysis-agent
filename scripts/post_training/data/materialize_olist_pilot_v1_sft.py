#!/usr/bin/env python3
"""Materialize the admitted Olist Pilot v1 runtime Prompt -> Gold SQL SFT splits.

All inputs and outputs remain external. The command binds the admitted Gold
assembly and the rebuilt production runtime prompts by seed ID, refuses split
or family drift, audits exact causal-LM length using a local tokenizer, and
writes train/validation/in-domain-test JSONL atomically. It does not train.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.candidate_sql_generator import OLIST_CANDIDATE_SQL_PROMPT_VERSION  # noqa: E402
from data_analysis_agent.olist_queryspec import WorkspacePin  # noqa: E402


CONTRACT_VERSION = "olist-pilot-v1-sft-v1"
EXPECTED_SPLITS = {"train": 24, "validation": 8, "in_domain_test": 8}
# Olist's actual production Catalog + QueryPlan prompt is materially longer
# than the historical SQLite benchmark prompt. The measured Pilot v1 maximum
# is 2,076 tokens, so 2,304 is the smallest practical 256-token-aligned cap.
DEFAULT_MAX_SEQ_LENGTH = 2304


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admission-assembly-dir", type=Path, required=True)
    parser.add_argument("--runtime-prompt-dir", type=Path, required=True)
    parser.add_argument("--tokenizer-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-seq-length", type=int, default=DEFAULT_MAX_SEQ_LENGTH)
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
        raise ValueError("SFT output must stay outside the Git worktree")
    if resolved.exists():
        raise FileExistsError(resolved)
    return resolved


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is invalid JSON") from exc
    if not isinstance(result, dict):
        raise ValueError(f"{label} must be an object")
    return result


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} has invalid JSON at line {line_no}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{label} line {line_no} must be an object")
        rows.append(row)
    return rows


def load_tokenizer(path: Path) -> Any:
    from transformers import AutoTokenizer

    path = _external_existing_dir(path, "tokenizer directory")
    tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
    if tokenizer.eos_token_id is None:
        raise ValueError("tokenizer must define an EOS token")
    return tokenizer


def _load_assembly(directory: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    directory = _external_existing_dir(directory, "admission assembly directory")
    manifest = _read_json(directory / "admission_assembly_manifest.json", "admission assembly manifest")
    records_path = _external_existing(directory / "admitted_records.jsonl", "admitted records")
    evidence = manifest.get("output", {}).get("admitted_records_jsonl", {})
    if manifest.get("workspace") != WorkspacePin.current().as_dict() or manifest.get("checks", {}).get("status") != "pass":
        raise ValueError("admission assembly does not match the current passing workspace")
    if evidence.get("rows") != 40 or evidence.get("sha256") != sha256_file(records_path):
        raise ValueError("admission assembly records do not match manifest")
    return manifest, _read_jsonl(records_path, "admitted records")


def _load_runtime(directory: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    directory = _external_existing_dir(directory, "runtime prompt directory")
    manifest = _read_json(directory / "runtime_prompt_manifest.json", "runtime prompt manifest")
    records_path = _external_existing(directory / "runtime_candidates.jsonl", "runtime candidates")
    evidence = manifest.get("output", {}).get("runtime_candidates_jsonl", {})
    if manifest.get("workspace", {}).get("prompt_version") != OLIST_CANDIDATE_SQL_PROMPT_VERSION:
        raise ValueError("runtime prompts use an unexpected production prompt version")
    if manifest.get("checks", {}).get("router_rebuilt") is not True or evidence.get("sha256") != sha256_file(records_path):
        raise ValueError("runtime prompt evidence is incomplete or mismatched")
    return manifest, _read_jsonl(records_path, "runtime candidates")


def _query_spec_id(row: Mapping[str, Any]) -> str | None:
    """Read the stable ID from runtime rows or from admission's nested QuerySpec."""
    direct = row.get("query_spec_id")
    if isinstance(direct, str) and direct:
        return direct
    spec = row.get("query_spec")
    value = spec.get("query_spec_id") if isinstance(spec, Mapping) else None
    return value if isinstance(value, str) and value else None


def build_rows(admitted: list[dict[str, Any]], runtime: list[dict[str, Any]], tokenizer: Any, max_seq_length: int) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    if max_seq_length <= 0:
        raise ValueError("max_seq_length must be positive")
    admitted_by_seed = {str(row.get("seed_id")): row for row in admitted}
    runtime_by_seed = {str(row.get("seed_id")): row for row in runtime}
    if len(admitted_by_seed) != 40 or set(admitted_by_seed) != set(runtime_by_seed):
        raise ValueError("admission/runtime seed sets must be identical 40-row sets")
    splits = {name: [] for name in EXPECTED_SPLITS}
    exclusions: list[dict[str, Any]] = []
    families_by_split: dict[str, set[str]] = {name: set() for name in EXPECTED_SPLITS}
    for index, seed_id in enumerate(admitted_by_seed, 1):
        admitted_row = admitted_by_seed[seed_id]
        runtime_row = runtime_by_seed[seed_id]
        split = admitted_row.get("split")
        if split not in splits:
            raise ValueError("admitted row has unsupported split")
        identity_fields = ("split", "family_id", "sql_program_id")
        if any(admitted_row.get(field) != runtime_row.get(field) for field in identity_fields):
            raise ValueError(f"runtime identity drift for {seed_id}")
        query_spec_id = _query_spec_id(admitted_row)
        if query_spec_id is None or query_spec_id != _query_spec_id(runtime_row):
            raise ValueError(f"runtime QuerySpec identity drift for {seed_id}")
        family_id = str(admitted_row.get("family_id"))
        if family_id in families_by_split[split]:
            raise ValueError(f"duplicate family within {split}")
        families_by_split[split].add(family_id)
        prompt = runtime_row.get("prompt")
        sql = admitted_row.get("gold_sql")
        if not isinstance(prompt, str) or not isinstance(sql, str) or not prompt.endswith("### SQL"):
            raise ValueError(f"invalid runtime prompt or Gold SQL for {seed_id}")
        training_text = prompt + "\n" + sql.strip()
        prompt_tokens = len(tokenizer(prompt + "\n", add_special_tokens=False)["input_ids"])
        target_tokens = len(tokenizer(sql.strip(), add_special_tokens=False)["input_ids"]) + 1
        sequence_tokens = prompt_tokens + target_tokens
        sample_id = f"olist-pilot-v1-{index:03d}"
        if sequence_tokens > max_seq_length:
            exclusions.append({
                "sample_id": sample_id,
                "seed_id": seed_id,
                "split": split,
                "family_id": family_id,
                "sequence_tokens": sequence_tokens,
                "prompt_tokens": prompt_tokens,
                "target_plus_eos_tokens": target_tokens,
                "reason": "sequence_exceeds_frozen_contract",
                "eligible_for_sft": False,
            })
            continue
        splits[split].append({
            "sample_id": sample_id,
            "split": {"name": split},
            "prompt_format_version": OLIST_CANDIDATE_SQL_PROMPT_VERSION,
            "seed_id": seed_id,
            "query_spec_id": query_spec_id,
            "family_id": family_id,
            "sql_program_id": admitted_row["sql_program_id"],
            "language_variant_id": runtime_row["variant_id"],
            "rendered_prompt": prompt,
            "candidate_sql": sql.strip(),
            "training_text": training_text,
            "admission_status": "admitted",
            "execution_outcome": {"postgres_reader_result_contract": "pass"},
            "token_length": {
                "sequence_tokens": sequence_tokens,
                "prompt_tokens": prompt_tokens,
                "target_plus_eos_tokens": target_tokens,
            },
        })
    if exclusions:
        raise ValueError("Pilot v1 does not permit length exclusions; inspect external exclusion evidence")
    if {name: len(rows) for name, rows in splits.items()} != EXPECTED_SPLITS:
        raise ValueError("materialized split counts differ from Pilot v1 contract")
    all_families = [row["family_id"] for rows in splits.values() for row in rows]
    if len(all_families) != len(set(all_families)):
        raise ValueError("family IDs cross splits")
    return splits, exclusions


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def materialize(assembly_dir: Path, runtime_dir: Path, tokenizer_dir: Path, output_dir: Path, *, max_seq_length: int, generated_at: str | None = None) -> dict[str, Any]:
    output_dir = _external_new_dir(output_dir)
    assembly_manifest, admitted = _load_assembly(assembly_dir)
    runtime_manifest, runtime = _load_runtime(runtime_dir)
    tokenizer = load_tokenizer(tokenizer_dir)
    splits, exclusions = build_rows(admitted, runtime, tokenizer, max_seq_length)
    generated_at = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.parent / f".{output_dir.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        (staging / "final_evaluation_only").mkdir()
        (staging / "exclusions").mkdir()
        staging_paths = {
            "train": staging / "train.jsonl",
            "validation": staging / "validation.jsonl",
            "in_domain_test": staging / "final_evaluation_only" / "in_domain_test.jsonl",
        }
        final_paths = {
            "train": output_dir / "train.jsonl",
            "validation": output_dir / "validation.jsonl",
            "in_domain_test": output_dir / "final_evaluation_only" / "in_domain_test.jsonl",
        }
        for split, path in staging_paths.items():
            write_jsonl(path, splits[split])
        exclusion_path = staging / "exclusions" / "length.jsonl"
        write_jsonl(exclusion_path, exclusions)
        split_metadata = {}
        for split, rows in splits.items():
            lengths = [row["token_length"]["sequence_tokens"] for row in rows]
            split_metadata[split] = {
                "rows": len(rows),
                "families": len({row["family_id"] for row in rows}),
                "query_specs": len({row["query_spec_id"] for row in rows}),
                "sha256": sha256_file(staging_paths[split]),
                "max_sequence_tokens": max(lengths),
                "min_sequence_tokens": min(lengths),
                "role": "parameter_updates" if split == "train" else "validation_only" if split == "validation" else "final_evaluation_only",
            }
        audit = {
            "audit_version": CONTRACT_VERSION,
            "generated_at": generated_at,
            "workspace": WorkspacePin.current().as_dict(),
            "prompt_version": OLIST_CANDIDATE_SQL_PROMPT_VERSION,
            "source": {
                "admission_assembly_manifest_sha256": sha256_file(_external_existing(Path(assembly_dir) / "admission_assembly_manifest.json", "assembly manifest")),
                "runtime_prompt_manifest_sha256": sha256_file(_external_existing(Path(runtime_dir) / "runtime_prompt_manifest.json", "runtime prompt manifest")),
            },
            "tokenizer": {"dir": str(Path(tokenizer_dir).resolve()), "eos_token_id": tokenizer.eos_token_id},
            "training_length_contract": {"max_seq_length": max_seq_length, "formula": "exact rendered runtime prompt + canonical SQL + EOS", "silent_truncation": False},
            "policy": {
                "split_strategy": "olist_pilot_v1_family_isolated",
                "primary_group": "family_id",
                "test_storage": "final_evaluation_only",
                "test_forbidden_for_training": True,
            },
            "splits": split_metadata,
            "outputs": {
                # The audit remains valid after the atomic staging-directory rename.
                "train_jsonl": str(final_paths["train"]),
                "validation_jsonl": str(final_paths["validation"]),
                "in_domain_test_jsonl": str(final_paths["in_domain_test"]),
            },
            "exclusions": {"path": str(output_dir / "exclusions" / "length.jsonl"), "rows": 0, "sha256": sha256_file(exclusion_path), "contains_question_or_sql": False},
            "checks": {"status": "pass", "family_split_overlap": [], "query_spec_split_overlap": [], "sql_program_split_overlap_allowed": True, "all_gold_admitted": True, "runtime_contract_rebuilt": True, "protected_holdout_raw_read": False, "in_domain_test_forbidden_for_training": True, "gpu_used": False},
        }
        (staging / "split_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        staging.replace(output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return audit


def main() -> int:
    args = parse_args()
    result = materialize(args.admission_assembly_dir, args.runtime_prompt_dir, args.tokenizer_dir, args.output_dir, max_seq_length=args.max_seq_length, generated_at=args.generated_at)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
