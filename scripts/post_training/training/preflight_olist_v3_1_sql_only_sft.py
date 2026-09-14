#!/usr/bin/env python3
"""Verify frozen Olist v3.1 SQL-only LoRA inputs without loading a model.

The preflight reads only the parameter-update and validation SFT files.  It
does not open the protected final-test JSONL, allocate CUDA memory, instantiate
``AutoModelForCausalLM``, execute SQL, or call a remote model.
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

from data_analysis_agent.external_artifacts import ensure_path_outside_repository  # noqa: E402
from scripts.post_training.training.run_post_training_sft_smoke import (  # noqa: E402
    CausalSqlCollator,
    CausalSqlDataset,
    load_rows,
    prompt_format_version,
    sha256_file,
    split_prompt_and_target,
    validate_checkpoint_intervals,
    validate_split_audit,
)


PREFLIGHT_VERSION = "olist-v3-1-sql-only-lora-preflight-v1"
EXPECTED_MODEL_ID = "Qwen/Qwen2.5-Coder-1.5B"
EXPECTED_MODEL_REVISION = "df3ce67c0e24480f20468b6ef2894622d69eb73b"
EXPECTED_MODEL_MANIFEST_SHA256 = (
    "484d1e01b35f19c6c764d2801239c20ca17da4270a76869825bffc08022829c1"
)
EXPECTED_RELEASE_AUDIT_SHA256 = (
    "58cc0aea5eb26e07bf7ef1bb751eb43ab15360b9f8f028bfb7c0f8313b88384a"
)
EXPECTED_SPLIT_SHA256 = {
    "train": "be3a9276abb3690c223aa68e064e4fb8e1ab3055f282c56c727dadd46aa7c7c8",
    "validation": "b2544aa8cef3f98134944df3a957dc046938bb351fdc64735b3139f6deefde38",
    "in_domain_test": "67c1db3f9a3d7a74160306cd1680ea6f3c6be00035d2c95d860bb3d6e1266750",
}
FROZEN_TRAINING = {
    "max_seq_length": 3072,
    "base_weight_mode": "bf16_lora",
    "learning_rate": 1e-4,
    "weight_decay": 0.01,
    "per_device_train_batch_size": 1,
    "gradient_accumulation_steps": 4,
    "per_device_eval_batch_size": 1,
    "num_train_epochs": 2.0,
    "evaluation_steps": 150,
    "save_steps": 150,
    "lora_r": 16,
    "lora_alpha": 32,
    "lora_dropout": 0.05,
    "seed": 20260914,
}


class OlistV31PreflightError(ValueError):
    """Raised when a frozen v3.1 input, layout, or training setting drifts."""


def parse_args() -> argparse.Namespace:
    """Parse the CPU-only v3.1 preflight inputs and frozen training settings."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--train-jsonl", type=Path, required=True)
    parser.add_argument("--validation-jsonl", type=Path, required=True)
    parser.add_argument("--split-audit", type=Path, required=True)
    parser.add_argument("--release-contract-audit", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--max-seq-length", type=int, default=FROZEN_TRAINING["max_seq_length"]
    )
    parser.add_argument(
        "--base-weight-mode", default=FROZEN_TRAINING["base_weight_mode"]
    )
    parser.add_argument(
        "--learning-rate", type=float, default=FROZEN_TRAINING["learning_rate"]
    )
    parser.add_argument(
        "--weight-decay", type=float, default=FROZEN_TRAINING["weight_decay"]
    )
    parser.add_argument(
        "--per-device-train-batch-size",
        type=int,
        default=FROZEN_TRAINING["per_device_train_batch_size"],
    )
    parser.add_argument(
        "--gradient-accumulation-steps",
        type=int,
        default=FROZEN_TRAINING["gradient_accumulation_steps"],
    )
    parser.add_argument(
        "--per-device-eval-batch-size",
        type=int,
        default=FROZEN_TRAINING["per_device_eval_batch_size"],
    )
    parser.add_argument(
        "--num-train-epochs", type=float, default=FROZEN_TRAINING["num_train_epochs"]
    )
    parser.add_argument(
        "--evaluation-steps", type=int, default=FROZEN_TRAINING["evaluation_steps"]
    )
    parser.add_argument("--save-steps", type=int, default=FROZEN_TRAINING["save_steps"])
    parser.add_argument("--lora-r", type=int, default=FROZEN_TRAINING["lora_r"])
    parser.add_argument("--lora-alpha", type=int, default=FROZEN_TRAINING["lora_alpha"])
    parser.add_argument(
        "--lora-dropout", type=float, default=FROZEN_TRAINING["lora_dropout"]
    )
    parser.add_argument("--seed", type=int, default=FROZEN_TRAINING["seed"])
    return parser.parse_args()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OlistV31PreflightError(f"cannot read {label}") from exc
    if not isinstance(payload, dict):
        raise OlistV31PreflightError(f"{label} must be a JSON object")
    return payload


def frozen_training_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """Return and validate the only v3.1 SQL-only Trainer configuration."""

    actual = {key: getattr(args, key) for key in FROZEN_TRAINING}
    if actual != FROZEN_TRAINING:
        drifted = sorted(
            key for key, expected in FROZEN_TRAINING.items() if actual[key] != expected
        )
        raise OlistV31PreflightError(
            "training configuration differs from the frozen v3.1 contract: "
            + ", ".join(drifted)
        )
    validate_checkpoint_intervals(
        int(actual["evaluation_steps"]), int(actual["save_steps"])
    )
    return actual


def verify_release_audit(path: Path, split_audit: Path) -> dict[str, Any]:
    """Bind the preflight to the final, hash-recomputed v3.1 release audit."""

    if sha256_file(path) != EXPECTED_RELEASE_AUDIT_SHA256:
        raise OlistV31PreflightError("release-contract audit SHA-256 drifted")
    report = _read_json(path, "release-contract audit")
    checks = report.get("checks")
    counts = report.get("counts")
    source = report.get("source")
    expected_flags = {
        "status": "pass",
        "sft_split_files_hash_bound": True,
        "sft_split_identity_recomputed": True,
        "model_called": False,
        "gpu_used": False,
        "sql_executed": False,
    }
    if (
        not isinstance(checks, Mapping)
        or not isinstance(counts, Mapping)
        or not isinstance(source, Mapping)
        or any(checks.get(key) != value for key, value in expected_flags.items())
        or counts.get("sft_rows")
        != {"train": 3000, "validation": 750, "in_domain_test": 750}
        or source.get("sft_split_audit_sha256") != sha256_file(split_audit)
    ):
        raise OlistV31PreflightError(
            "release-contract audit does not bind frozen SFT inputs"
        )
    return report


def verify_model_manifest(model_dir: Path) -> dict[str, Any]:
    """Require the frozen Qwen2.5-Coder Base revision and manifest fingerprint."""

    manifest_path = model_dir / "download_manifest.json"
    if sha256_file(manifest_path) != EXPECTED_MODEL_MANIFEST_SHA256:
        raise OlistV31PreflightError("Base download-manifest SHA-256 drifted")
    manifest = _read_json(manifest_path, "Base download manifest")
    if (
        manifest.get("model_id") != EXPECTED_MODEL_ID
        or manifest.get("revision") != EXPECTED_MODEL_REVISION
    ):
        raise OlistV31PreflightError(
            "Base model identity/revision differs from contract"
        )
    return manifest


def verify_split_fingerprints(split_audit: Mapping[str, Any]) -> None:
    """Require all three release hashes, while retaining final-test non-reading."""

    splits = split_audit.get("splits")
    if not isinstance(splits, Mapping):
        raise OlistV31PreflightError("split audit has no split metadata")
    expected_roles = {
        "train": "parameter_updates",
        "validation": "validation_only",
        "in_domain_test": "final_evaluation_only",
    }
    for split, expected_hash in EXPECTED_SPLIT_SHA256.items():
        metadata = splits.get(split)
        if (
            not isinstance(metadata, Mapping)
            or metadata.get("sha256") != expected_hash
            or metadata.get("role") != expected_roles[split]
        ):
            raise OlistV31PreflightError(f"frozen {split} split metadata drifted")


def dataset_layout_stats(
    rows: list[dict[str, Any]], tokenizer: Any, max_seq_length: int
) -> dict[str, int]:
    """Verify exact Prompt/SQL/EOS labels and return question-free aggregates."""

    dataset = CausalSqlDataset(rows, tokenizer, max_seq_length)
    prompt_lengths: list[int] = []
    sequence_lengths: list[int] = []
    target_lengths: list[int] = []
    for row, example in zip(rows, dataset.examples, strict=True):
        prompt, target = split_prompt_and_target(row)
        prompt_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
        target_ids = tokenizer(target, add_special_tokens=False)["input_ids"]
        input_ids = example["input_ids"].tolist()
        labels = example["labels"].tolist()
        attention_mask = example["attention_mask"].tolist()
        expected_labels = (
            [-100] * len(prompt_ids) + target_ids + [tokenizer.eos_token_id]
        )
        if (
            labels != expected_labels
            or input_ids != prompt_ids + target_ids + [tokenizer.eos_token_id]
            or attention_mask != [1] * len(input_ids)
        ):
            raise OlistV31PreflightError("runtime Prompt/SQL/EOS label layout drifted")
        prompt_lengths.append(len(prompt_ids))
        sequence_lengths.append(len(input_ids))
        target_lengths.append(len(target_ids) + 1)
    if len(dataset) < 2:
        raise OlistV31PreflightError("preflight requires at least two rows per split")
    shortest = min(dataset.examples, key=lambda item: item["input_ids"].size(0))
    longest = max(dataset.examples, key=lambda item: item["input_ids"].size(0))
    padded = CausalSqlCollator(tokenizer.pad_token_id)([shortest, longest])
    short_length = shortest["input_ids"].size(0)
    if short_length < longest["input_ids"].size(0) and (
        padded["attention_mask"][0, short_length:].tolist()
        != [0] * (longest["input_ids"].size(0) - short_length)
        or padded["labels"][0, short_length:].tolist()
        != [-100] * (longest["input_ids"].size(0) - short_length)
    ):
        raise OlistV31PreflightError("dynamic right-padding layout drifted")
    return {
        "rows": len(rows),
        "min_sequence_tokens": min(sequence_lengths),
        "max_sequence_tokens": max(sequence_lengths),
        "min_prompt_tokens": min(prompt_lengths),
        "max_prompt_tokens": max(prompt_lengths),
        "min_sql_plus_eos_tokens": min(target_lengths),
        "max_sql_plus_eos_tokens": max(target_lengths),
        "silent_truncation": 0,
    }


def main() -> int:
    """Run the v3.1 CPU-only preflight and write safe external evidence."""

    args = parse_args()
    config = frozen_training_from_args(args)
    for path in (
        args.model_dir,
        args.train_jsonl,
        args.validation_jsonl,
        args.split_audit,
        args.release_contract_audit,
    ):
        ensure_path_outside_repository(path, ROOT)
    output_dir = ensure_path_outside_repository(args.output_dir, ROOT)
    if output_dir.exists():
        raise OlistV31PreflightError("preflight output directory must be new")

    release_audit = verify_release_audit(args.release_contract_audit, args.split_audit)
    model_manifest = verify_model_manifest(args.model_dir)
    split_audit = _read_json(args.split_audit, "SFT split audit")
    verify_split_fingerprints(split_audit)
    validate_split_audit(split_audit, args.train_jsonl, args.validation_jsonl)
    train_rows = load_rows(args.train_jsonl, "train")
    validation_rows = load_rows(args.validation_jsonl, "validation")
    train_prompt_version = prompt_format_version(train_rows)
    validation_prompt_version = prompt_format_version(validation_rows)
    if (
        train_prompt_version != "olist-candidate-sql-v1"
        or validation_prompt_version != train_prompt_version
    ):
        raise OlistV31PreflightError("runtime Candidate SQL Prompt version drifted")

    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(args.model_dir, local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"
    if tokenizer.eos_token_id is None or tokenizer.pad_token_id is None:
        raise OlistV31PreflightError("frozen tokenizer lacks EOS or padding token")
    report = {
        "preflight_version": PREFLIGHT_VERSION,
        "checks": {
            "status": "pass",
            "model_loaded": False,
            "gpu_used": False,
            "sql_executed": False,
            "remote_model_called": False,
            "final_test_jsonl_read": False,
            "runtime_prompt_sql_eos_layout": True,
            "dynamic_right_padding": True,
            "silent_truncation": False,
            "validation_best_checkpoint_configured": True,
        },
        "model": {
            "id": model_manifest["model_id"],
            "revision": model_manifest["revision"],
            "download_manifest_sha256": sha256_file(
                args.model_dir / "download_manifest.json"
            ),
        },
        "release": {
            "release_contract_audit_sha256": sha256_file(args.release_contract_audit),
            "split_audit_sha256": sha256_file(args.split_audit),
            "release_audit_status": release_audit["checks"]["status"],
            "final_test_role": split_audit["splits"]["in_domain_test"]["role"],
        },
        "data": {
            "train_jsonl_sha256": sha256_file(args.train_jsonl),
            "validation_jsonl_sha256": sha256_file(args.validation_jsonl),
            "prompt_format_version": train_prompt_version,
            "train": dataset_layout_stats(train_rows, tokenizer, args.max_seq_length),
            "validation": dataset_layout_stats(
                validation_rows, tokenizer, args.max_seq_length
            ),
            "raw_question_or_sql_saved": False,
        },
        "training": {
            **config,
            "effective_batch_size": config["per_device_train_batch_size"]
            * config["gradient_accumulation_steps"],
            "expected_optimizer_steps": 1500,
            "checkpoint_selection": {
                "metric": "eval_loss",
                "greater_is_better": False,
                "load_best_model_at_end": True,
                "adapter_final_before_best_reload": True,
                "headline_adapter": "adapter_best",
            },
        },
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    report_path = output_dir / "preflight.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OlistV31PreflightError, ValueError) as exc:
        print(f"Olist v3.1 SQL-only preflight error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
