#!/usr/bin/env python3
"""CPU-only Qwen3.5 layout audit for the frozen multi-schema SQL-only release."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src"
for path in (ROOT, SOURCE_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from data_analysis_agent.external_artifacts import ensure_path_outside_repository
from data_analysis_agent.qwen35_sft_format import (
    QWEN35_OLIST_SFT_TEMPLATE_VERSION,
    Qwen35SftFormatError,
    build_qwen35_sql_sft_example,
)
from scripts.post_training.training.run_post_training_sft_smoke import (
    sha256_file,
    validate_split_audit,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--expected-model-id", required=True)
    parser.add_argument("--train-jsonl", type=Path, required=True)
    parser.add_argument("--validation-jsonl", type=Path, required=True)
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--max-seq-length", type=int, default=3072)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _rows(path: Path, split: str) -> list[dict[str, Any]]:
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, json.JSONDecodeError) as exc:
        raise Qwen35SftFormatError(f"cannot read {split} split") from exc
    if not rows or any(row.get("split", {}).get("name") != split for row in rows):
        raise Qwen35SftFormatError(f"invalid {split} split")
    return rows


def _stats(rows: list[dict[str, Any]], processor: Any, max_seq_length: int) -> dict[str, int]:
    lengths: list[int] = []
    targets: list[int] = []
    for row in rows:
        example = build_qwen35_sql_sft_example(
            processor, str(row["rendered_prompt"]), str(row["candidate_sql"]),
            max_seq_length=max_seq_length,
        )
        lengths.append(len(example.input_ids))
        targets.append(example.supervised_tokens)
    return {
        "rows": len(rows), "min_sequence_tokens": min(lengths),
        "max_sequence_tokens": max(lengths), "min_supervised_tokens": min(targets),
        "max_supervised_tokens": max(targets),
        "at_max_seq_length": sum(length == max_seq_length for length in lengths),
    }


def main() -> int:
    args = parse_args()
    if args.max_seq_length <= 0:
        raise Qwen35SftFormatError("max sequence length must be positive")
    for path in (args.model_dir, args.train_jsonl, args.validation_jsonl, args.release_manifest):
        ensure_path_outside_repository(path, ROOT)
    output = ensure_path_outside_repository(args.output, ROOT)
    if output.exists():
        raise Qwen35SftFormatError("layout audit output must be new")
    manifest = json.loads(args.release_manifest.read_text(encoding="utf-8"))
    validate_split_audit(manifest, args.train_jsonl, args.validation_jsonl)
    model_manifest = json.loads((args.model_dir / "download_manifest.json").read_text(encoding="utf-8"))
    if model_manifest.get("model_id") != args.expected_model_id:
        raise Qwen35SftFormatError("unexpected Qwen3.5 model identity")
    from transformers import AutoProcessor
    processor = AutoProcessor.from_pretrained(args.model_dir, local_files_only=True)
    report = {
        "report_schema_version": "qwen35-multi-schema-sql-only-layout-audit-v1",
        "template": {
            "version": QWEN35_OLIST_SFT_TEMPLATE_VERSION,
            "official_chat_template": True, "enable_thinking": False,
            "prompt_labels": "ignore_index", "silent_truncation": False,
        },
        "model": {
            "id": model_manifest["model_id"], "revision": model_manifest["revision"],
            "download_manifest_sha256": sha256_file(args.model_dir / "download_manifest.json"),
        },
        "source": {
            "release_manifest_sha256": sha256_file(args.release_manifest),
            "train_jsonl_sha256": sha256_file(args.train_jsonl),
            "validation_jsonl_sha256": sha256_file(args.validation_jsonl),
            "workspace_balance": {"olist": 0.5, "thelook": 0.5},
            "protected_tests_forbidden_for_training": True,
        },
        "max_seq_length": args.max_seq_length,
        "splits": {
            "train": _stats(_rows(args.train_jsonl, "train"), processor, args.max_seq_length),
            "validation": _stats(_rows(args.validation_jsonl, "validation"), processor, args.max_seq_length),
        },
        "raw_question_or_sql_saved": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"splits": report["splits"], "template": report["template"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Qwen35SftFormatError, ValueError) as exc:
        print(f"Qwen3.5 multi-schema layout audit error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
