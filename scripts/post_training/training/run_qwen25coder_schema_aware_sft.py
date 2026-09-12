#!/usr/bin/env python3
"""Train a fresh Qwen2.5-Coder-1.5B Base LoRA on paired Olist events.

Task A keeps the production-shaped SQL target.  Task B is a training-only
SchemaLinkPlan JSON target.  The two events are interleaved per pair, while
validation is reported independently for SQL and program events.  This entry
point is deliberately fail-closed: it consumes only the externally audited
materialization and never reads TheLook or the in-domain final test.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import sys
from typing import Any

import numpy as np
import torch
from peft import LoraConfig, PeftModel, get_peft_model
from torch.utils.data import Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments, set_seed

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.external_artifacts import ensure_path_outside_repository  # noqa: E402
from scripts.post_training.training.run_post_training_sft_smoke import (  # noqa: E402
    sha256_file,
)


CONTRACT_VERSION = "olist-schema-aware-program-sft-v1"
AUDIT_VERSION = "olist-schema-aware-program-sft-audit-v1"
TASK_A = "sql"
TASK_B = "schema_link_plan"
DEFAULT_MODEL_ID = "Qwen/Qwen2.5-Coder-1.5B"
DEFAULT_MODEL_REVISION = "df3ce67c0e24480f20468b6ef2894622d69eb73b"


class SchemaAwareTrainerError(ValueError):
    """Raised when the frozen pair contract or training configuration is invalid."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--expected-model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--expected-model-revision", default=DEFAULT_MODEL_REVISION)
    parser.add_argument("--materialization-dir", type=Path, required=True)
    parser.add_argument("--audit-report", type=Path, required=True)
    parser.add_argument("--review-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-seq-length", type=int, default=3072)
    parser.add_argument("--max-steps", type=int, default=-1)
    parser.add_argument("--num-train-epochs", type=float, default=None)
    parser.add_argument("--seed", type=int, default=20260912)
    parser.add_argument("--learning-rate", type=float, default=1e-4)
    parser.add_argument("--weight-decay", type=float, default=0.01)
    parser.add_argument("--per-device-train-batch-size", type=int, default=1)
    parser.add_argument("--gradient-accumulation-steps", type=int, default=4)
    parser.add_argument("--per-device-eval-batch-size", type=int, default=1)
    parser.add_argument("--evaluation-steps", type=int, default=250)
    parser.add_argument("--save-steps", type=int, default=250)
    parser.add_argument("--logging-steps", type=int, default=10)
    parser.add_argument("--lora-r", type=int, default=16)
    parser.add_argument("--lora-alpha", type=int, default=32)
    parser.add_argument("--lora-dropout", type=float, default=0.05)
    parser.add_argument("--physical-nvidia-smi-device", type=int, required=True)
    parser.add_argument("--expected-gpu-uuid", default=None)
    parser.add_argument("--experiment-label", default="qwen25coder-schema-aware-sft")
    return parser.parse_args(argv)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaAwareTrainerError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise SchemaAwareTrainerError(f"{label} must be a JSON object")
    return value


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SchemaAwareTrainerError(f"cannot read {label}: {path}") from exc
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SchemaAwareTrainerError(f"{label} invalid JSON at line {number}") from exc
        if not isinstance(row, dict):
            raise SchemaAwareTrainerError(f"{label} line {number} is not an object")
        rows.append(row)
    if not rows:
        raise SchemaAwareTrainerError(f"{label} is empty")
    return rows


def _check_external(path: Path, label: str, *, directory: bool = False) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise SchemaAwareTrainerError(f"{label} must stay outside the Git worktree")
    missing = not resolved.is_dir() if directory else not resolved.is_file()
    if missing:
        raise SchemaAwareTrainerError(f"{label} does not exist: {resolved}")
    return resolved


def validate_materialization(
    materialization: Path,
    audit: dict[str, Any],
    review: dict[str, Any],
    *,
    max_seq_length: int,
) -> dict[str, list[dict[str, Any]]]:
    """Recheck the completed external gates before any model allocation."""
    if audit.get("audit_version") != AUDIT_VERSION:
        raise SchemaAwareTrainerError("unsupported schema-aware audit version")
    checks = audit.get("checks")
    if not isinstance(checks, dict) or checks.get("status") != "pass":
        raise SchemaAwareTrainerError("schema-aware materialization audit did not pass")
    for key in (
        "thelook_read", "in_domain_test_used", "model_called", "sql_executed", "gpu_used",
    ):
        if checks.get(key) is not False:
            raise SchemaAwareTrainerError(f"audit does not prove {key}=false")
    counts = audit.get("counts")
    if not isinstance(counts, dict) or counts.get("length_exclusions") != 0:
        raise SchemaAwareTrainerError("audit has missing or non-zero length exclusions")
    if review.get("review_version") != "olist-schema-aware-program-sft-review-v1":
        raise SchemaAwareTrainerError("unsupported schema-aware review version")
    review_checks = review.get("checks")
    if not isinstance(review_checks, dict) or review_checks.get("status") != "pass":
        raise SchemaAwareTrainerError("schema-aware bounded review did not pass")
    source_hashes = audit.get("evidence", {})
    if not isinstance(source_hashes, dict):
        raise SchemaAwareTrainerError("audit has no evidence hashes")
    materialization_evidence = audit.get("materialization")
    if (
        not isinstance(materialization_evidence, dict)
        or Path(str(materialization_evidence.get("dir", ""))).resolve() != materialization.resolve()
    ):
        raise SchemaAwareTrainerError("audit is not bound to the requested materialization directory")
    output: dict[str, list[dict[str, Any]]] = {}
    for split, expected in (("train", 2400), ("validation", 600)):
        events = _read_jsonl(materialization / f"{split}_events.jsonl", f"{split} events")
        if len(events) != expected * 2:
            raise SchemaAwareTrainerError(f"{split} event count is not exactly {expected * 2}")
        if sha256_file(materialization / f"{split}_events.jsonl") != source_hashes.get(f"{split}_events_sha256"):
            raise SchemaAwareTrainerError(f"{split} event hash differs from audit")
        pairs: dict[str, set[str]] = {}
        for row in events:
            task = row.get("task_type")
            pair = row.get("pair_id")
            if task not in (TASK_A, TASK_B) or not isinstance(pair, str) or not pair:
                raise SchemaAwareTrainerError(f"{split} has malformed task/pair identity")
            if row.get("split", {}).get("name") != split:
                raise SchemaAwareTrainerError(f"{split} event has incorrect split marker")
            if row.get("token_length", {}).get("sequence_tokens", max_seq_length + 1) > max_seq_length:
                raise SchemaAwareTrainerError(f"{split} event exceeds max sequence length")
            if row.get("prompt_format_version") not in {
                "olist-candidate-sql-v1", "olist-schema-aware-program-sft-v1",
            }:
                raise SchemaAwareTrainerError(f"{split} has unsupported prompt format")
            if not isinstance(row.get("rendered_prompt"), str) or not isinstance(row.get("target_text"), str):
                raise SchemaAwareTrainerError(f"{split} event lacks prompt or target")
            if row.get("training_text") != row["rendered_prompt"] + "\n" + row["target_text"]:
                raise SchemaAwareTrainerError(f"{split} event training text boundary drift")
            pairs.setdefault(pair, set()).add(task)
        if len(pairs) != expected or any(value != {TASK_A, TASK_B} for value in pairs.values()):
            raise SchemaAwareTrainerError(f"{split} does not contain exactly one A/B pair per query")
        output[split] = events
    return output


class PairEventDataset(Dataset[dict[str, torch.Tensor]]):
    """Causal LM examples with labels only on target text and EOS."""

    def __init__(self, rows: list[dict[str, Any]], tokenizer: Any, max_seq_length: int) -> None:
        self.rows = rows
        self.examples: list[dict[str, torch.Tensor]] = []
        self.stats = {"samples": 0, "task_counts": Counter(), "max_sequence_tokens": 0,
                      "max_target_tokens": 0}
        for row in rows:
            prompt_ids = tokenizer(row["rendered_prompt"] + "\n", add_special_tokens=False)["input_ids"]
            target_ids = tokenizer(row["target_text"], add_special_tokens=False)["input_ids"]
            if not target_ids:
                raise SchemaAwareTrainerError(f"{row['event_id']} has empty target")
            input_ids = prompt_ids + target_ids + [tokenizer.eos_token_id]
            if len(input_ids) > max_seq_length:
                raise SchemaAwareTrainerError(f"{row['event_id']} exceeds max_seq_length")
            labels = [-100] * len(prompt_ids) + target_ids + [tokenizer.eos_token_id]
            self.examples.append({
                "input_ids": torch.tensor(input_ids, dtype=torch.long),
                "attention_mask": torch.ones(len(input_ids), dtype=torch.long),
                "labels": torch.tensor(labels, dtype=torch.long),
            })
            self.stats["samples"] += 1
            self.stats["task_counts"][row["task_type"]] += 1
            self.stats["max_sequence_tokens"] = max(self.stats["max_sequence_tokens"], len(input_ids))
            self.stats["max_target_tokens"] = max(self.stats["max_target_tokens"], len(target_ids) + 1)
        self.stats["task_counts"] = dict(self.stats["task_counts"])

    def __len__(self) -> int:
        return len(self.examples)

    def __getitem__(self, index: int) -> dict[str, torch.Tensor]:
        return self.examples[index]


class RightPaddingCollator:
    def __init__(self, pad_token_id: int) -> None:
        self.pad_token_id = pad_token_id

    def __call__(self, features: list[dict[str, torch.Tensor]]) -> dict[str, torch.Tensor]:
        length = max(feature["input_ids"].size(0) for feature in features)
        result: dict[str, list[torch.Tensor]] = {"input_ids": [], "attention_mask": [], "labels": []}
        for feature in features:
            pad = length - feature["input_ids"].size(0)
            result["input_ids"].append(torch.nn.functional.pad(feature["input_ids"], (0, pad), value=self.pad_token_id))
            result["attention_mask"].append(torch.nn.functional.pad(feature["attention_mask"], (0, pad), value=0))
            result["labels"].append(torch.nn.functional.pad(feature["labels"], (0, pad), value=-100))
        return {name: torch.stack(values) for name, values in result.items()}


def _gpu_identity() -> tuple[Any, str]:
    gpu = torch.cuda.get_device_properties(0)
    uuid = str(gpu.uuid)
    return gpu, uuid if uuid.startswith("GPU-") else "GPU-" + uuid


def _load_model(model_dir: Path) -> Any:
    model = AutoModelForCausalLM.from_pretrained(
        model_dir, local_files_only=True, torch_dtype=torch.bfloat16, device_map={"": 0}
    )
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    return model


def _evaluate_by_task(trainer: Trainer, datasets: dict[str, PairEventDataset]) -> dict[str, float]:
    result: dict[str, float] = {}
    for task, dataset in datasets.items():
        metrics = trainer.evaluate(eval_dataset=dataset, metric_key_prefix=f"eval_{task}")
        result[f"{task}_loss"] = float(metrics[f"eval_{task}_loss"])
    return result


def main() -> int:
    args = parse_args()
    if not torch.cuda.is_available():
        raise SchemaAwareTrainerError("CUDA is required for this Trainer")
    if args.max_seq_length <= 0 or args.learning_rate <= 0 or args.weight_decay < 0:
        raise SchemaAwareTrainerError("invalid sequence, learning-rate, or weight-decay setting")
    if (args.max_steps > 0) == (args.num_train_epochs is not None):
        raise SchemaAwareTrainerError("set exactly one of positive max steps or num train epochs")
    if args.num_train_epochs is not None and args.num_train_epochs <= 0:
        raise SchemaAwareTrainerError("num train epochs must be positive")
    gpu, gpu_uuid = _gpu_identity()
    if args.expected_gpu_uuid and gpu_uuid != args.expected_gpu_uuid:
        raise SchemaAwareTrainerError(f"GPU UUID mismatch: expected {args.expected_gpu_uuid}, got {gpu_uuid}")
    model_dir = _check_external(args.model_dir, "model directory", directory=True)
    materialization = _check_external(args.materialization_dir, "materialization directory", directory=True)
    audit_path = _check_external(args.audit_report, "audit report")
    review_path = _check_external(args.review_report, "review report")
    output_dir = args.output_dir.resolve()
    if output_dir.is_relative_to(ROOT) or output_dir.exists():
        raise SchemaAwareTrainerError("output directory must be a new external path")
    audit = _read_json(audit_path, "audit report")
    review = _read_json(review_path, "review report")
    manifest = _read_json(model_dir / "download_manifest.json", "model manifest")
    if (
        manifest.get("model_id") != args.expected_model_id
        or manifest.get("revision") != args.expected_model_revision
    ):
        raise SchemaAwareTrainerError("model manifest does not match the frozen Base contract")
    events = validate_materialization(materialization, audit, review, max_seq_length=args.max_seq_length)
    tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"
    train_rows = events["train"]
    validation_rows = events["validation"]
    train_dataset = PairEventDataset(train_rows, tokenizer, args.max_seq_length)
    validation_all = PairEventDataset(validation_rows, tokenizer, args.max_seq_length)
    validation_by_task = {
        TASK_A: PairEventDataset([row for row in validation_rows if row["task_type"] == TASK_A], tokenizer, args.max_seq_length),
        TASK_B: PairEventDataset([row for row in validation_rows if row["task_type"] == TASK_B], tokenizer, args.max_seq_length),
    }
    random.seed(args.seed); np.random.seed(args.seed); set_seed(args.seed)
    output_dir.mkdir(parents=True, exist_ok=False)
    model = get_peft_model(
        _load_model(model_dir),
        LoraConfig(r=args.lora_r, lora_alpha=args.lora_alpha, lora_dropout=args.lora_dropout,
                   bias="none", target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
                   task_type="CAUSAL_LM"),
    )
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    if not 0 < trainable < total:
        raise SchemaAwareTrainerError("LoRA injection did not freeze the base model")
    checkpoint_dir = output_dir / "adapter_checkpoints"
    train_args = TrainingArguments(
        output_dir=str(checkpoint_dir), do_train=True, do_eval=True, eval_strategy="steps",
        eval_steps=args.evaluation_steps, per_device_train_batch_size=args.per_device_train_batch_size,
        per_device_eval_batch_size=args.per_device_eval_batch_size, gradient_accumulation_steps=args.gradient_accumulation_steps,
        learning_rate=args.learning_rate, max_steps=args.max_steps,
        num_train_epochs=args.num_train_epochs if args.num_train_epochs is not None else 1.0,
        lr_scheduler_type="constant", logging_strategy="steps", logging_steps=args.logging_steps,
        logging_first_step=True, save_strategy="steps", save_steps=args.save_steps, save_total_limit=2,
        bf16=True, tf32=True, optim="adamw_torch", weight_decay=args.weight_decay,
        gradient_checkpointing=True, gradient_checkpointing_kwargs={"use_reentrant": False},
        report_to=[], remove_unused_columns=False, seed=args.seed, data_seed=args.seed, dataloader_num_workers=0,
    )
    trainer = Trainer(model=model, args=train_args, train_dataset=train_dataset, eval_dataset=validation_all,
                      data_collator=RightPaddingCollator(tokenizer.pad_token_id))
    started = datetime.now(timezone.utc)
    train_result = trainer.train()
    aggregate_eval = trainer.evaluate()
    task_eval = _evaluate_by_task(trainer, validation_by_task)
    adapter_dir = output_dir / "adapter_final"
    trainer.save_model(str(adapter_dir)); trainer.save_state()
    model_manifest = _read_json(model_dir / "download_manifest.json", "model manifest")
    evidence = {
        "report_schema_version": "olist-schema-aware-program-sft-run-v1",
        "experiment_label": args.experiment_label,
        "started_at": started.replace(microsecond=0).isoformat(),
        "model": {"id": model_manifest.get("model_id"), "revision": model_manifest.get("revision"),
                  "download_manifest_sha256": sha256_file(model_dir / "download_manifest.json"), "base_weight_mode": "bf16_frozen"},
        "data": {"contract_version": CONTRACT_VERSION, "materialization_dir": str(materialization),
                 "audit_sha256": sha256_file(audit_path), "review_sha256": sha256_file(review_path),
                 "train_events_sha256": sha256_file(materialization / "train_events.jsonl"),
                 "validation_events_sha256": sha256_file(materialization / "validation_events.jsonl"),
                 "train": train_dataset.stats, "validation": validation_all.stats,
                 "thelook_used": False, "in_domain_test_used": False, "raw_question_or_sql_saved": False},
        "training": {"seed": args.seed, "max_seq_length": args.max_seq_length, "max_steps": args.max_steps,
                     "num_train_epochs": args.num_train_epochs, "per_device_train_batch_size": args.per_device_train_batch_size,
                     "gradient_accumulation_steps": args.gradient_accumulation_steps,
                     "global_batch_size": args.per_device_train_batch_size * args.gradient_accumulation_steps,
                     "learning_rate": args.learning_rate, "optimizer": "adamw_torch", "weight_decay": args.weight_decay,
                     "gradient_checkpointing": True, "compute_dtype": "bfloat16", "lora_r": args.lora_r,
                     "lora_alpha": args.lora_alpha, "lora_dropout": args.lora_dropout,
                     "trainable_parameters": trainable, "total_parameters": total},
        "results": {"global_step": int(trainer.state.global_step), "train_loss": float(train_result.training_loss),
                    "aggregate_validation_loss": float(aggregate_eval["eval_loss"]), **task_eval,
                    "elapsed_seconds": (datetime.now(timezone.utc) - started).total_seconds()},
        "gpu": {"cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"), "process_local_device": 0,
                "physical_nvidia_smi_device": args.physical_nvidia_smi_device, "name": gpu.name, "uuid": gpu_uuid,
                "peak_allocated_bytes": torch.cuda.max_memory_allocated(), "peak_reserved_bytes": torch.cuda.max_memory_reserved()},
        "boundaries": {"production_default_unchanged": True, "thelook_final_test_used": False,
                        "raw_artifacts_outside_repository": True},
    }
    (output_dir / "sft_run.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"results": evidence["results"], "training": evidence["training"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SchemaAwareTrainerError, RuntimeError) as exc:
        print(f"Schema-aware SFT error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
