#!/usr/bin/env python3
"""Audit frozen Olist SFT rows under Qwen3.5 Instruct's official template.

This is a CPU/tokenizer-only preflight.  It reads only the existing Olist
parameter-update and validation splits, writes aggregate evidence outside Git,
and never reads the protected in-domain or TheLook final tests.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from data_analysis_agent.external_artifacts import ensure_path_outside_repository
from data_analysis_agent.qwen35_sft_format import (
    QWEN35_OLIST_SFT_TEMPLATE_VERSION,
    Qwen35SftFormatError,
    build_qwen35_sql_sft_example,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--train-jsonl", type=Path, required=True)
    parser.add_argument("--validation-jsonl", type=Path, required=True)
    parser.add_argument("--split-audit", type=Path, required=True)
    parser.add_argument("--max-seq-length", type=int, required=True)
    parser.add_argument(
        "--expected-model-id",
        default="Qwen/Qwen3.5-4B",
        help="Frozen Qwen3.5 Instruct model identity bound into the audit.",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_split(path: Path, expected_split: str) -> list[Mapping[str, Any]]:
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, json.JSONDecodeError) as exc:
        raise Qwen35SftFormatError(f"cannot read {expected_split} split") from exc
    if not rows or any(not isinstance(row, Mapping) for row in rows):
        raise Qwen35SftFormatError(f"{expected_split} split must contain JSON objects")
    identifiers = set()
    for row in rows:
        sample_id = row.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id or sample_id in identifiers:
            raise Qwen35SftFormatError(f"{expected_split} split has invalid sample IDs")
        identifiers.add(sample_id)
        split = row.get("split")
        if not isinstance(split, Mapping) or split.get("name") != expected_split:
            raise Qwen35SftFormatError(f"{sample_id} is not a {expected_split} row")
        prompt, sql, training_text = (
            row.get("rendered_prompt"),
            row.get("candidate_sql"),
            row.get("training_text"),
        )
        if not isinstance(prompt, str) or not isinstance(sql, str) or not isinstance(training_text, str):
            raise Qwen35SftFormatError(f"{sample_id} lacks runtime prompt or canonical SQL")
        if training_text != prompt.rstrip() + "\n" + sql.strip():
            raise Qwen35SftFormatError(f"{sample_id} does not preserve the runtime SQL target")
    return rows


def verify_source_audit(audit_path: Path, train_path: Path, validation_path: Path) -> Mapping[str, Any]:
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Qwen35SftFormatError("cannot read Olist split audit") from exc
    if not isinstance(audit, Mapping):
        raise Qwen35SftFormatError("Olist split audit must be an object")
    policy, checks, splits, contract = (
        audit.get("policy"),
        audit.get("checks"),
        audit.get("splits"),
        audit.get("training_length_contract"),
    )
    if (
        not isinstance(policy, Mapping)
        or not isinstance(checks, Mapping)
        or not isinstance(splits, Mapping)
        or not isinstance(contract, Mapping)
        or policy.get("split_strategy") != "olist_family_isolated_v1"
        or policy.get("test_forbidden_for_training") is not True
        or checks.get("status") != "pass"
        or checks.get("family_split_overlap") != []
        or checks.get("query_spec_split_overlap") != []
        or contract.get("silent_truncation") is not False
    ):
        raise Qwen35SftFormatError("source split audit does not prove Olist isolation")
    for name, path in (("train", train_path), ("validation", validation_path)):
        metadata = splits.get(name)
        if not isinstance(metadata, Mapping) or metadata.get("sha256") != sha256_file(path):
            raise Qwen35SftFormatError(f"source audit hash mismatch for {name}")
    return audit


def split_stats(rows: list[Mapping[str, Any]], processor: Any, max_seq_length: int) -> Mapping[str, int]:
    sequences: list[int] = []
    prompts: list[int] = []
    targets: list[int] = []
    for row in rows:
        example = build_qwen35_sql_sft_example(
            processor,
            str(row["rendered_prompt"]),
            str(row["candidate_sql"]),
            max_seq_length=max_seq_length,
        )
        sequences.append(len(example.input_ids))
        prompts.append(example.prompt_tokens)
        targets.append(example.supervised_tokens)
    return {
        "rows": len(rows),
        "min_sequence_tokens": min(sequences),
        "max_sequence_tokens": max(sequences),
        "min_prompt_tokens": min(prompts),
        "max_prompt_tokens": max(prompts),
        "min_supervised_tokens": min(targets),
        "max_supervised_tokens": max(targets),
        "at_max_seq_length": sum(length == max_seq_length for length in sequences),
    }


def main() -> int:
    args = parse_args()
    if args.max_seq_length <= 0:
        raise Qwen35SftFormatError("max sequence length must be positive")
    for path in (args.model_dir, args.train_jsonl, args.validation_jsonl, args.split_audit):
        ensure_path_outside_repository(path, ROOT)
    output = ensure_path_outside_repository(args.output, ROOT)
    if output.exists():
        raise Qwen35SftFormatError("output audit must be a new external file")
    audit = verify_source_audit(args.split_audit, args.train_jsonl, args.validation_jsonl)
    train_rows = read_split(args.train_jsonl, "train")
    validation_rows = read_split(args.validation_jsonl, "validation")

    from transformers import AutoProcessor

    processor = AutoProcessor.from_pretrained(args.model_dir, local_files_only=True)
    manifest = json.loads((args.model_dir / "download_manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping) or manifest.get("model_id") != args.expected_model_id:
        raise Qwen35SftFormatError("audit model identity differs from expected Qwen3.5 Instruct model")
    report = {
        "report_schema_version": "qwen35-olist-sft-layout-audit-v1",
        "template": {
            "version": QWEN35_OLIST_SFT_TEMPLATE_VERSION,
            "official_chat_template": True,
            "enable_thinking": False,
            "user_content": "exact_rendered_runtime_prompt",
            "assistant_target": "canonical_sql_plus_assistant_eot",
            "prompt_labels": "ignore_index",
            "post_eot_template_tokens_supervised": False,
            "silent_truncation": False,
        },
        "model": {
            "id": manifest["model_id"],
            "revision": manifest["revision"],
            "download_manifest_sha256": sha256_file(args.model_dir / "download_manifest.json"),
        },
        "source": {
            "train_jsonl_sha256": sha256_file(args.train_jsonl),
            "validation_jsonl_sha256": sha256_file(args.validation_jsonl),
            "split_audit_sha256": sha256_file(args.split_audit),
            "split_strategy": audit["policy"]["split_strategy"],
            "in_domain_test_forbidden_for_training": audit["policy"]["test_forbidden_for_training"],
            "thelook_used": False,
        },
        "max_seq_length": args.max_seq_length,
        "splits": {
            "train": split_stats(train_rows, processor, args.max_seq_length),
            "validation": split_stats(validation_rows, processor, args.max_seq_length),
        },
        "raw_question_or_sql_saved": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"template": report["template"], "splits": report["splits"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Qwen35SftFormatError, ValueError) as exc:
        print(f"Qwen3.5 Olist SFT layout audit error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
