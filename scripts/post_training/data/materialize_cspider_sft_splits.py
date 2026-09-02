#!/usr/bin/env python3
"""Materialize CSpider SFT splits under the frozen token-length contract.

The input is the already audited ``official-splits-v1`` directory.  Train and
validation rows above ``max_seq_length`` are excluded into a metadata-only
manifest.  Final test is never filtered: every test row must fit the contract
or the command fails closed, preserving the official evaluation population.
All output is external to the Git worktree and is written atomically.
"""
from __future__ import annotations

import argparse
import copy
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

from scripts.post_training.training.run_post_training_sft_smoke import (  # noqa: E402
    load_rows,
    split_prompt_and_target,
    validate_split_audit,
)


CONTRACT_VERSION = "cspider-token-length-v1"
DEFAULT_MAX_SEQ_LENGTH = 1536
SOURCE_SPLIT_DIRNAME = "official-splits-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--tokenizer-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-seq-length", type=int, default=DEFAULT_MAX_SEQ_LENGTH)
    parser.add_argument(
        "--generated-at",
        default=None,
        help="UTC ISO-8601 timestamp; set this for byte-reproducible output.",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tokenizer_assets_sha256(tokenizer_dir: Path) -> tuple[list[str], str]:
    names = (
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "added_tokens.json",
        "vocab.json",
        "merges.txt",
        "tokenizer.model",
        "spiece.model",
    )
    files = [tokenizer_dir / name for name in names if (tokenizer_dir / name).is_file()]
    if not files:
        raise ValueError(f"tokenizer directory has no recognized assets: {tokenizer_dir}")
    digest = hashlib.sha256()
    recorded: list[str] = []
    for path in files:
        recorded.append(path.name)
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return recorded, digest.hexdigest()


def load_tokenizer(tokenizer_dir: Path) -> Any:
    if not tokenizer_dir.is_dir():
        raise FileNotFoundError(tokenizer_dir)
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(tokenizer_dir, local_files_only=True)
    if tokenizer.eos_token_id is None:
        raise ValueError("tokenizer must define an EOS token")
    return tokenizer


def row_length(row: Mapping[str, Any], tokenizer: Any) -> tuple[int, int, int]:
    prompt, target = split_prompt_and_target(dict(row))
    prompt_tokens = len(tokenizer(prompt, add_special_tokens=False)["input_ids"])
    target_tokens = len(tokenizer(target, add_special_tokens=False)["input_ids"]) + 1
    return prompt_tokens + target_tokens, prompt_tokens, target_tokens


def read_audited_source(source_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    audit_path = source_dir / "split_audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    train_path = source_dir / "train.jsonl"
    validation_path = source_dir / "validation.jsonl"
    test_path = source_dir / "final_evaluation_only" / "test.jsonl"
    validate_split_audit(audit, train_path, validation_path)
    outputs = audit.get("outputs")
    if not isinstance(outputs, Mapping):
        raise ValueError("source split audit has no output paths")
    if Path(str(outputs.get("test_jsonl"))).resolve() != test_path.resolve():
        raise ValueError("source split audit test path does not match source directory")
    train = load_rows(train_path, "train")
    validation = load_rows(validation_path, "validation")
    test = load_rows(test_path, "test")
    if len(test) != audit["splits"]["test"]["rows"]:
        raise ValueError("source test row count does not match split audit")
    return audit, train, validation, test


def materialize_rows(
    rows: list[dict[str, Any]],
    tokenizer: Any,
    max_seq_length: int,
    *,
    split: str,
    filter_over_budget: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    kept: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    source_max_length = 0
    kept_max_length = 0
    for row in rows:
        sequence_tokens, prompt_tokens, target_tokens = row_length(row, tokenizer)
        source_max_length = max(source_max_length, sequence_tokens)
        if sequence_tokens > max_seq_length:
            if not filter_over_budget:
                raise ValueError(
                    f"{split} contains {row['sample_id']} above max_seq_length={max_seq_length}; "
                    "final test cannot be filtered"
                )
            excluded.append(
                {
                    "sample_id": row["sample_id"],
                    "split": split,
                    "sequence_tokens": sequence_tokens,
                    "prompt_tokens": prompt_tokens,
                    "target_plus_eos_tokens": target_tokens,
                    "reason": "sequence_exceeds_frozen_contract",
                    "eligible_for_sft": False,
                }
            )
            continue
        kept.append(row)
        kept_max_length = max(kept_max_length, sequence_tokens)
    return kept, excluded, {
        "source_rows": len(rows),
        "kept_rows": len(kept),
        "excluded_rows": len(excluded),
        "max_sequence_tokens": kept_max_length,
        "source_max_sequence_tokens": source_max_length,
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def materialize(
    source_dir: Path,
    tokenizer_dir: Path,
    output_dir: Path,
    *,
    max_seq_length: int = DEFAULT_MAX_SEQ_LENGTH,
    generated_at: str | None = None,
) -> dict[str, Any]:
    source_dir = source_dir.resolve()
    tokenizer_dir = tokenizer_dir.resolve()
    output_dir = output_dir.resolve()
    if max_seq_length <= 0:
        raise ValueError("max_seq_length must be positive")
    if output_dir.is_relative_to(ROOT):
        raise ValueError("materialized data must stay outside the Git working tree")
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    if source_dir.name != SOURCE_SPLIT_DIRNAME:
        raise ValueError(f"source directory must be named {SOURCE_SPLIT_DIRNAME}")

    source_audit, train, validation, test = read_audited_source(source_dir)
    tokenizer = load_tokenizer(tokenizer_dir)
    generated_at = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    train_kept, train_excluded, train_stats = materialize_rows(
        train, tokenizer, max_seq_length, split="train", filter_over_budget=True
    )
    validation_kept, validation_excluded, validation_stats = materialize_rows(
        validation, tokenizer, max_seq_length, split="validation", filter_over_budget=True
    )
    test_kept, test_excluded, test_stats = materialize_rows(
        test, tokenizer, max_seq_length, split="test", filter_over_budget=False
    )
    if test_excluded:
        raise AssertionError("final test must not have exclusions")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.parent / f".{output_dir.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        (staging / "final_evaluation_only").mkdir()
        (staging / "exclusions").mkdir()
        train_path = staging / "train.jsonl"
        validation_path = staging / "validation.jsonl"
        test_path = staging / "final_evaluation_only" / "test.jsonl"
        write_jsonl(train_path, train_kept)
        write_jsonl(validation_path, validation_kept)
        write_jsonl(test_path, test_kept)
        exclusion_path = staging / "exclusions" / "train-validation-length.jsonl"
        write_jsonl(exclusion_path, train_excluded + validation_excluded)
        asset_names, asset_hash = tokenizer_assets_sha256(tokenizer_dir)
        # Keep the upstream CSpider audit shape so the existing Trainer gate
        # can consume this artifact without a second, weaker protocol.
        audit = copy.deepcopy(source_audit)
        audit.update({
            "audit_version": "cspider-materialized-splits-v1",
            "generated_at": generated_at,
            "generator": "scripts/post_training/data/materialize_cspider_sft_splits.py",
        })
        audit["source"].update({
                "materialized_from": str(source_dir),
                "source_split_audit_sha256": sha256_file(source_dir / "split_audit.json"),
                "source_train_sha256": sha256_file(source_dir / "train.jsonl"),
                "source_validation_sha256": sha256_file(source_dir / "validation.jsonl"),
                "source_test_sha256": sha256_file(source_dir / "final_evaluation_only" / "test.jsonl"),
            })
        audit["tokenizer"] = {"dir": str(tokenizer_dir), "asset_filenames": asset_names, "assets_sha256": asset_hash, "eos_token_id": tokenizer.eos_token_id}
        audit["prompt"].update({"token_budget_enforced": True, "token_budget_note": f"Materialized under {CONTRACT_VERSION} with max_seq_length={max_seq_length}; no truncation."})
        audit["training_length_contract"] = {"version": CONTRACT_VERSION, "max_seq_length": max_seq_length, "formula": "prompt tokens + candidate SQL tokens + one EOS", "padding_included": False, "silent_truncation": False}
        for name, stats, role, official, path in (
            ("train", train_stats, "parameter_updates", "train", train_path),
            ("validation", validation_stats, "validation_only", "dev", validation_path),
            ("test", test_stats, "final_evaluation_only", "test", test_path),
        ):
            metadata = audit["splits"][name]
            metadata.update(
                stats,
                rows=stats["kept_rows"],
                role=role,
                official_split=official,
                sha256=sha256_file(path),
            )
            if name == "test":
                metadata["forbidden_for_training"] = True
        source_explain = copy.deepcopy(audit["checks"]["sqlite_readonly_explain"])
        audit["checks"]["source_sqlite_readonly_explain"] = source_explain
        audit["checks"]["sqlite_readonly_explain"] = {
            "train": {"pass": train_stats["kept_rows"]},
            "dev": {"pass": validation_stats["kept_rows"]},
            "test": {"pass": test_stats["kept_rows"]},
        }
        audit["checks"]["length_contract"] = {"version": CONTRACT_VERSION, "max_seq_length": max_seq_length, "train_excluded": len(train_excluded), "validation_excluded": len(validation_excluded), "test_excluded": 0}
        audit["exclusions"] = {"path": str(output_dir / "exclusions" / "train-validation-length.jsonl"), "sha256": sha256_file(exclusion_path), "rows": len(train_excluded) + len(validation_excluded), "contains_question_or_sql": False}
        audit["source_audit"] = {"strategy": source_audit["policy"]["split_strategy"], "test_read": True, "test_filtered": False}
        audit["outputs"] = {"train_jsonl": str(output_dir / "train.jsonl"), "validation_jsonl": str(output_dir / "validation.jsonl"), "test_jsonl": str(output_dir / "final_evaluation_only" / "test.jsonl")}
        (staging / "split_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        staging.replace(output_dir)
        return audit
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    args = parse_args()
    result = materialize(args.source_dir, args.tokenizer_dir, args.output_dir, max_seq_length=args.max_seq_length, generated_at=args.generated_at)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
