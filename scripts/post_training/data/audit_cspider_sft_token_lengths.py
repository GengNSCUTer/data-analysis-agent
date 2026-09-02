#!/usr/bin/env python3
"""Audit CSpider train/validation SFT token lengths without reading final test.

The count deliberately mirrors ``CausalSqlDataset``: tokenize the prompt and
SQL target independently with ``add_special_tokens=False`` and append exactly
one EOS token.  No model weights, GPU, truncation, or filtered dataset are
created by this command.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

# Allow direct execution by file path while retaining the canonical script imports.
ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.post_training.training.run_post_training_sft_smoke import (
    load_rows,
    sha256_file,
    split_prompt_and_target,
    validate_split_audit,
)


AUDIT_VERSION = "cspider-token-length-v1"
DEFAULT_MAX_SEQ_LENGTH = 1536
REPORT_PERCENTILES = (50, 90, 95, 99, 100)
TOKENIZER_ASSET_FILENAMES = (
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "added_tokens.json",
    "vocab.json",
    "merges.txt",
    "tokenizer.model",
    "spiece.model",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-jsonl", type=Path, required=True)
    parser.add_argument("--validation-jsonl", type=Path, required=True)
    parser.add_argument("--split-audit", type=Path, required=True)
    parser.add_argument("--tokenizer-dir", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--max-seq-length", type=int, default=DEFAULT_MAX_SEQ_LENGTH)
    parser.add_argument(
        "--comparison-lengths",
        type=int,
        nargs="+",
        default=[1024, 1536, 2048, 3072],
        help="Additional sequence limits reported for capacity planning.",
    )
    parser.add_argument(
        "--generated-at",
        default=None,
        help="UTC ISO-8601 timestamp; set for byte-reproducible report content.",
    )
    return parser.parse_args()


def tokenizer_assets_sha256(tokenizer_dir: Path) -> tuple[list[str], str]:
    asset_paths = [
        tokenizer_dir / filename
        for filename in TOKENIZER_ASSET_FILENAMES
        if (tokenizer_dir / filename).is_file()
    ]
    if not asset_paths:
        raise ValueError(f"tokenizer directory has no recognized tokenizer assets: {tokenizer_dir}")
    digest = hashlib.sha256()
    filenames: list[str] = []
    for path in asset_paths:
        filename = path.name
        filenames.append(filename)
        digest.update(filename.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return filenames, digest.hexdigest()


def require_cspider_audit(audit: Mapping[str, Any]) -> None:
    policy = audit.get("policy")
    if not isinstance(policy, Mapping) or policy.get("split_strategy") != "official_cspider_train_dev_test":
        raise ValueError("token audit requires the official CSpider three-split audit")


def nearest_rank(values: list[int], percentile: int) -> int:
    if not values:
        raise ValueError("cannot calculate a percentile for no values")
    if percentile <= 0 or percentile > 100:
        raise ValueError("percentile must be in 1..100")
    index = (len(values) * percentile + 99) // 100 - 1
    return sorted(values)[index]


def distribution(values: list[int]) -> dict[str, int]:
    if not values:
        raise ValueError("cannot describe no values")
    return {
        "min": min(values),
        **{f"p{percentile}": nearest_rank(values, percentile) for percentile in REPORT_PERCENTILES},
        "max": max(values),
    }


def token_ids(tokenizer: Any, text: str) -> list[int]:
    result = tokenizer(text, add_special_tokens=False)
    ids = result.get("input_ids") if isinstance(result, Mapping) else None
    if not isinstance(ids, list) or not all(isinstance(item, int) for item in ids):
        raise ValueError("tokenizer did not return integer input_ids")
    return ids


def analyze_rows(
    rows: Iterable[dict[str, Any]], tokenizer: Any, max_seq_length: int, comparison_lengths: list[int]
) -> dict[str, Any]:
    if max_seq_length <= 0 or any(length <= 0 for length in comparison_lengths):
        raise ValueError("sequence lengths must be positive")
    if tokenizer.eos_token_id is None:
        raise ValueError("tokenizer must define an EOS token")

    prompt_lengths: list[int] = []
    target_lengths: list[int] = []
    sequence_lengths: list[int] = []
    for row in rows:
        prompt, target = split_prompt_and_target(row)
        prompt_lengths.append(len(token_ids(tokenizer, prompt)))
        target_lengths.append(len(token_ids(tokenizer, target)) + 1)
        sequence_lengths.append(prompt_lengths[-1] + target_lengths[-1])

    if not sequence_lengths:
        raise ValueError("token audit input is empty")
    limits = sorted({max_seq_length, *comparison_lengths})
    over_budget = {str(limit): sum(length > limit for length in sequence_lengths) for limit in limits}
    return {
        "rows": len(sequence_lengths),
        "counting_contract": {
            "prompt": "tokenize prompt through SQL marker with add_special_tokens=false",
            "target": "tokenize candidate SQL with add_special_tokens=false, then append one EOS",
            "padding_tokens_included": False,
            "silent_truncation": False,
        },
        "prompt_tokens": distribution(prompt_lengths),
        "target_plus_eos_tokens": distribution(target_lengths),
        "sequence_tokens": distribution(sequence_lengths),
        "over_budget_rows": over_budget,
        "eligible_rows_at_contract_limit": len(sequence_lengths) - over_budget[str(max_seq_length)],
    }


def load_tokenizer(tokenizer_dir: Path) -> Any:
    if not tokenizer_dir.is_dir():
        raise FileNotFoundError(tokenizer_dir)
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(tokenizer_dir, local_files_only=True)


def tokenizer_metadata(tokenizer: Any, tokenizer_dir: Path) -> dict[str, Any]:
    config_path = tokenizer_dir / "config.json"
    model_config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    asset_filenames, assets_sha256 = tokenizer_assets_sha256(tokenizer_dir)
    return {
        "local_dir": str(tokenizer_dir.resolve()),
        "asset_filenames": asset_filenames,
        "assets_sha256": assets_sha256,
        "class": type(tokenizer).__name__,
        "eos_token_id": tokenizer.eos_token_id,
        "pad_token_id": tokenizer.pad_token_id,
        "model_max_length": tokenizer.model_max_length,
        "model_max_position_embeddings": model_config.get("max_position_embeddings"),
    }


def write_json_atomically(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite token audit: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        temporary_path.replace(path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def main() -> int:
    args = parse_args()
    if args.max_seq_length <= 0:
        raise ValueError("--max-seq-length must be positive")
    if args.output_json.resolve().is_relative_to(Path(__file__).resolve().parents[3]):
        raise ValueError("token audit output must stay outside the Git working tree")

    audit = json.loads(args.split_audit.read_text(encoding="utf-8"))
    require_cspider_audit(audit)
    validate_split_audit(audit, args.train_jsonl, args.validation_jsonl)
    tokenizer = load_tokenizer(args.tokenizer_dir)
    train_rows = load_rows(args.train_jsonl, "train")
    validation_rows = load_rows(args.validation_jsonl, "validation")
    generated_at = args.generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    report = {
        "audit_version": AUDIT_VERSION,
        "generated_at": generated_at,
        "generator": "scripts/post_training/data/audit_cspider_sft_token_lengths.py",
        "scope": {
            "included_splits": ["train", "validation"],
            "final_test_read": False,
            "model_weights_loaded": False,
            "gpu_used": False,
            "truncation_performed": False,
        },
        "source": {
            "split_audit": str(args.split_audit.resolve()),
            "split_audit_sha256": sha256_file(args.split_audit),
            "train_jsonl": str(args.train_jsonl.resolve()),
            "train_jsonl_sha256": sha256_file(args.train_jsonl),
            "validation_jsonl": str(args.validation_jsonl.resolve()),
            "validation_jsonl_sha256": sha256_file(args.validation_jsonl),
        },
        "tokenizer": tokenizer_metadata(tokenizer, args.tokenizer_dir),
        "training_length_contract": {
            "version": AUDIT_VERSION,
            "max_seq_length": args.max_seq_length,
            "policy": "no_truncation; rows above the limit are ineligible for a later materialized train/validation artifact",
            "required_future_gate": "each later materialized split must preserve official role and record excluded row counts and source hashes",
        },
        "splits": {
            "train": analyze_rows(train_rows, tokenizer, args.max_seq_length, args.comparison_lengths),
            "validation": analyze_rows(
                validation_rows, tokenizer, args.max_seq_length, args.comparison_lengths
            ),
        },
    }
    write_json_atomically(args.output_json, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
