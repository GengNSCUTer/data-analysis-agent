#!/usr/bin/env python3
"""Run a small, resumable LoRA or QLoRA SFT experiment on external candidates.

This script deliberately trains only the LoRA adapter. Source rows, model
weights, checkpoints and logs must all live outside the Git working tree.
Evidence records counts, hashes and metrics but never copies questions or SQL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import torch
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from torch.utils.data import Dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    Trainer,
    TrainingArguments,
    set_seed,
)

from data_analysis_agent.spider_sft_format import PROMPT_FORMAT_VERSION


SQL_MARKER = "\n\n### SQL\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--train-jsonl", type=Path, required=True)
    parser.add_argument("--validation-jsonl", type=Path, required=True)
    parser.add_argument("--split-audit", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--resume-from-checkpoint",
        type=Path,
        default=None,
        help="External Trainer checkpoint directory; never resume from Git.",
    )
    parser.add_argument("--max-seq-length", type=int, default=1536)
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument(
        "--num-train-epochs",
        type=float,
        default=None,
        help="Use complete dataset passes instead of --max-steps for a scaled experiment.",
    )
    parser.add_argument("--seed", type=int, default=20260825)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--gradient-accumulation-steps", type=int, default=4)
    parser.add_argument("--per-device-eval-batch-size", type=int, default=1)
    parser.add_argument("--evaluation-steps", type=int, default=4)
    parser.add_argument("--save-steps", type=int, default=4)
    parser.add_argument("--logging-steps", type=int, default=1)
    parser.add_argument("--lora-r", type=int, default=16)
    parser.add_argument("--lora-alpha", type=int, default=32)
    parser.add_argument("--lora-dropout", type=float, default=0.05)
    parser.add_argument(
        "--base-weight-mode",
        choices=("qlora_4bit", "bf16_lora"),
        default="qlora_4bit",
        help=(
            "qlora_4bit stores the frozen base in 4-bit NF4; bf16_lora keeps "
            "the frozen base in bf16. Both modes train the same LoRA adapter."
        ),
    )
    parser.add_argument(
        "--physical-nvidia-smi-device",
        type=int,
        required=True,
        help="Physical nvidia-smi device ID recorded by the launcher.",
    )
    parser.add_argument(
        "--expected-gpu-uuid",
        default=None,
        help="Optional physical GPU UUID guard; rejects an unexpected CUDA mapping.",
    )
    parser.add_argument(
        "--experiment-label",
        default="unnamed",
        help="Stable external experiment label recorded in the evidence manifest.",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_rows(path: Path, expected_split: str) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not rows:
        raise ValueError(f"{expected_split} split is empty")
    sample_ids = {row.get("sample_id") for row in rows}
    if len(sample_ids) != len(rows) or None in sample_ids:
        raise ValueError(f"{expected_split} split has duplicate or missing sample IDs")
    for row in rows:
        if row.get("split", {}).get("name") != expected_split:
            raise ValueError(f"{row['sample_id']} is not in expected {expected_split} split")
        if row.get("execution_outcome", {}).get("sqlite_readonly_explain") != "pass":
            raise ValueError(f"{row['sample_id']} lacks execution evidence")
    return rows


def prompt_format_version(rows: list[dict[str, Any]]) -> str:
    """Require one explicit prompt contract across a training split."""

    versions = {str(row.get("prompt_format_version", PROMPT_FORMAT_VERSION)) for row in rows}
    if len(versions) != 1:
        raise ValueError("training split mixes prompt format versions")
    return versions.pop()


def validate_split_audit(
    audit: dict[str, Any], train_path: Path, validation_path: Path
) -> None:
    """Verify split artifacts still match the audit used to create them."""

    checks = audit.get("checks")
    if not isinstance(checks, dict) or checks.get("status") != "pass":
        raise ValueError("split audit did not pass")
    if checks.get("v2_holdout_used") is not False:
        raise ValueError("split audit does not prove v2 holdout isolation")

    policy = audit.get("policy")
    if not isinstance(policy, dict) or policy.get("primary_group") != "spider_db_id":
        raise ValueError("split audit does not prove database-grouped validation")

    split_metadata = audit.get("splits")
    if not isinstance(split_metadata, dict):
        raise ValueError("split audit has no split metadata")
    for split_name, path in (("train", train_path), ("validation", validation_path)):
        metadata = split_metadata.get(split_name)
        if not isinstance(metadata, dict):
            raise ValueError(f"split audit has no {split_name} metadata")
        expected_hash = metadata.get("sha256")
        if not isinstance(expected_hash, str) or not expected_hash:
            raise ValueError(f"split audit has no {split_name} hash")
        actual_hash = sha256_file(path)
        if actual_hash != expected_hash:
            raise ValueError(
                f"{split_name} file does not match split audit: "
                f"expected {expected_hash}, got {actual_hash}"
            )
        expected_rows = metadata.get("rows")
        actual_rows = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line)
        if expected_rows != actual_rows:
            raise ValueError(
                f"{split_name} row count does not match split audit: "
                f"expected {expected_rows}, got {actual_rows}"
            )


def split_prompt_and_target(row: dict[str, Any]) -> tuple[str, str]:
    training_text = row.get("training_text")
    candidate_sql = row.get("candidate_sql")
    if not isinstance(training_text, str) or not isinstance(candidate_sql, str):
        raise ValueError(f"{row['sample_id']} lacks training text or target SQL")
    if SQL_MARKER not in training_text:
        raise ValueError(f"{row['sample_id']} has no SQL target marker")
    prompt, embedded_sql = training_text.rsplit(SQL_MARKER, 1)
    if embedded_sql.strip() != candidate_sql.strip():
        raise ValueError(f"{row['sample_id']} target SQL mismatch")
    return prompt + SQL_MARKER, candidate_sql.strip()


class CausalSqlDataset(Dataset[dict[str, torch.Tensor]]):
    def __init__(
        self,
        rows: list[dict[str, Any]],
        tokenizer: Any,
        max_seq_length: int,
    ) -> None:
        self.examples: list[dict[str, torch.Tensor]] = []
        self.stats = {"samples": 0, "max_sequence_tokens": 0, "max_target_tokens": 0}
        for row in rows:
            prompt, target = split_prompt_and_target(row)
            prompt_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
            target_ids = tokenizer(target, add_special_tokens=False)["input_ids"]
            input_ids = prompt_ids + target_ids + [tokenizer.eos_token_id]
            labels = [-100] * len(prompt_ids) + target_ids + [tokenizer.eos_token_id]
            if len(input_ids) != len(labels) or not target_ids:
                raise ValueError(f"invalid label layout for {row['sample_id']}")
            if len(input_ids) > max_seq_length:
                raise ValueError(
                    f"{row['sample_id']} has {len(input_ids)} tokens above max_seq_length={max_seq_length}; "
                    "refuse to truncate target SQL"
                )
            self.examples.append(
                {
                    "input_ids": torch.tensor(input_ids, dtype=torch.long),
                    "attention_mask": torch.ones(len(input_ids), dtype=torch.long),
                    "labels": torch.tensor(labels, dtype=torch.long),
                }
            )
            self.stats["samples"] += 1
            self.stats["max_sequence_tokens"] = max(
                self.stats["max_sequence_tokens"], len(input_ids)
            )
            self.stats["max_target_tokens"] = max(
                self.stats["max_target_tokens"], len(target_ids) + 1
            )

    def __len__(self) -> int:
        return len(self.examples)

    def __getitem__(self, index: int) -> dict[str, torch.Tensor]:
        return self.examples[index]


@dataclass
class CausalSqlCollator:
    pad_token_id: int

    def __call__(self, features: list[dict[str, torch.Tensor]]) -> dict[str, torch.Tensor]:
        max_length = max(feature["input_ids"].size(0) for feature in features)
        batch: dict[str, list[torch.Tensor]] = {"input_ids": [], "attention_mask": [], "labels": []}
        for feature in features:
            padding = max_length - feature["input_ids"].size(0)
            batch["input_ids"].append(
                torch.nn.functional.pad(feature["input_ids"], (0, padding), value=self.pad_token_id)
            )
            batch["attention_mask"].append(
                torch.nn.functional.pad(feature["attention_mask"], (0, padding), value=0)
            )
            batch["labels"].append(
                torch.nn.functional.pad(feature["labels"], (0, padding), value=-100)
            )
        return {name: torch.stack(values) for name, values in batch.items()}


def count_parameters(model: torch.nn.Module) -> tuple[int, int]:
    total = sum(parameter.numel() for parameter in model.parameters())
    trainable = sum(parameter.numel() for parameter in model.parameters() if parameter.requires_grad)
    return trainable, total


def latest_metric(log_history: list[dict[str, Any]], key: str) -> float | None:
    values = [item[key] for item in log_history if key in item]
    return float(values[-1]) if values else None


def main() -> int:
    args = parse_args()
    # Accelerate validates host-wide RTX 40-series P2P even for this single-GPU
    # run. Disable unavailable NCCL paths explicitly instead of relying on a
    # launcher-specific environment.
    os.environ.setdefault("NCCL_P2P_DISABLE", "1")
    os.environ.setdefault("NCCL_IB_DISABLE", "1")
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for SFT smoke")
    if args.max_seq_length <= 0:
        raise ValueError("max sequence length must be positive")
    if args.num_train_epochs is None and args.max_steps <= 0:
        raise ValueError("max steps must be positive when epochs are not configured")
    if args.num_train_epochs is not None and args.num_train_epochs <= 0:
        raise ValueError("num train epochs must be positive")
    if min(
        args.gradient_accumulation_steps,
        args.per_device_eval_batch_size,
        args.evaluation_steps,
        args.save_steps,
        args.logging_steps,
    ) <= 0:
        raise ValueError("batch and step interval arguments must be positive")
    repository_root = Path(__file__).resolve().parents[3]
    if args.output_dir.resolve().is_relative_to(repository_root):
        raise ValueError("output directory must be outside the Git working tree")
    if args.resume_from_checkpoint is not None:
        if args.resume_from_checkpoint.resolve().is_relative_to(repository_root):
            raise ValueError("resume checkpoint must be outside the Git working tree")
        if not args.resume_from_checkpoint.is_dir():
            raise FileNotFoundError(args.resume_from_checkpoint)

    train_rows = load_rows(args.train_jsonl, "train")
    validation_rows = load_rows(args.validation_jsonl, "validation")
    train_prompt_format = prompt_format_version(train_rows)
    validation_prompt_format = prompt_format_version(validation_rows)
    if train_prompt_format != validation_prompt_format:
        raise ValueError("train and validation splits use different prompt formats")
    split_audit = json.loads(args.split_audit.read_text(encoding="utf-8"))
    validate_split_audit(split_audit, args.train_jsonl, args.validation_jsonl)

    random.seed(args.seed)
    np.random.seed(args.seed)
    set_seed(args.seed)
    tokenizer = AutoTokenizer.from_pretrained(args.model_dir, local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"
    train_dataset = CausalSqlDataset(train_rows, tokenizer, args.max_seq_length)
    validation_dataset = CausalSqlDataset(validation_rows, tokenizer, args.max_seq_length)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir = args.output_dir / "adapter_checkpoints"
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    model_load_kwargs: dict[str, Any] = {
        "local_files_only": True,
        "torch_dtype": torch.bfloat16,
        "device_map": {"": 0},
    }
    if args.base_weight_mode == "qlora_4bit":
        model_load_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
        )
    model = AutoModelForCausalLM.from_pretrained(args.model_dir, **model_load_kwargs)
    model.config.use_cache = False
    if args.base_weight_mode == "qlora_4bit":
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    elif hasattr(model, "enable_input_require_grads"):
        # Non-reentrant checkpointing still needs the first adapter input to be
        # differentiable on some Transformers model implementations.
        model.enable_input_require_grads()
    model = get_peft_model(
        model,
        LoraConfig(
            r=args.lora_r,
            lora_alpha=args.lora_alpha,
            lora_dropout=args.lora_dropout,
            bias="none",
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
            task_type="CAUSAL_LM",
        ),
    )
    trainable, total = count_parameters(model)
    planned_max_steps = -1 if args.num_train_epochs is not None else args.max_steps
    training_args = TrainingArguments(
        output_dir=str(checkpoint_dir),
        overwrite_output_dir=args.resume_from_checkpoint is None,
        do_train=True,
        do_eval=True,
        eval_strategy="steps",
        eval_steps=args.evaluation_steps,
        per_device_train_batch_size=1,
        per_device_eval_batch_size=args.per_device_eval_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        learning_rate=args.learning_rate,
        max_steps=planned_max_steps,
        num_train_epochs=args.num_train_epochs if args.num_train_epochs is not None else 3.0,
        lr_scheduler_type="constant",
        logging_strategy="steps",
        logging_steps=args.logging_steps,
        logging_first_step=True,
        save_strategy="steps",
        save_steps=args.save_steps,
        save_total_limit=2,
        bf16=True,
        tf32=True,
        optim="paged_adamw_8bit",
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        report_to=[],
        remove_unused_columns=False,
        seed=args.seed,
        data_seed=args.seed,
        dataloader_num_workers=0,
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=validation_dataset,
        data_collator=CausalSqlCollator(tokenizer.pad_token_id),
    )
    started = datetime.now(timezone.utc)
    train_result = trainer.train(
        resume_from_checkpoint=(str(args.resume_from_checkpoint) if args.resume_from_checkpoint else None)
    )
    evaluation = trainer.evaluate()
    adapter_dir = args.output_dir / "adapter_final"
    trainer.save_model(str(adapter_dir))
    trainer.save_state()
    elapsed_seconds = (datetime.now(timezone.utc) - started).total_seconds()
    model_manifest = json.loads((args.model_dir / "download_manifest.json").read_text(encoding="utf-8"))
    gpu = torch.cuda.get_device_properties(0)
    gpu_uuid = str(gpu.uuid)
    if not gpu_uuid.startswith("GPU-"):
        gpu_uuid = "GPU-" + gpu_uuid
    if args.expected_gpu_uuid is not None and gpu_uuid != args.expected_gpu_uuid:
        raise RuntimeError(
            "CUDA device UUID does not match launcher guard: "
            f"expected {args.expected_gpu_uuid}, got {gpu_uuid}"
        )
    evidence = {
        "experiment_type": "adapter_sft_coverage_ablation",
        "experiment_label": args.experiment_label,
        "started_at": started.replace(microsecond=0).isoformat(),
        "model": {
            "id": model_manifest["model_id"],
            "revision": model_manifest["revision"],
            "download_manifest_sha256": sha256_file(args.model_dir / "download_manifest.json"),
        },
        "data": {
            "train_jsonl_sha256": sha256_file(args.train_jsonl),
            "validation_jsonl_sha256": sha256_file(args.validation_jsonl),
            "split_audit_sha256": sha256_file(args.split_audit),
            "train": train_dataset.stats,
            "validation": validation_dataset.stats,
            "prompt_format_version": train_prompt_format,
            "raw_question_or_sql_saved": False,
            "v2_holdout_used": False,
        },
        "training": {
            "seed": args.seed,
            "max_seq_length": args.max_seq_length,
            "per_device_train_batch_size": 1,
            "per_device_eval_batch_size": args.per_device_eval_batch_size,
            "gradient_accumulation_steps": args.gradient_accumulation_steps,
            "global_batch_size": args.gradient_accumulation_steps,
            "max_steps": planned_max_steps,
            "num_train_epochs": args.num_train_epochs,
            "evaluation_steps": args.evaluation_steps,
            "save_steps": args.save_steps,
            "logging_steps": args.logging_steps,
            "resumed_from_checkpoint": (
                str(args.resume_from_checkpoint) if args.resume_from_checkpoint else None
            ),
            "learning_rate": args.learning_rate,
            "optimizer": "paged_adamw_8bit",
            "gradient_checkpointing": True,
            "base_weight_mode": args.base_weight_mode,
            "load_in_4bit": args.base_weight_mode == "qlora_4bit",
            "quant_type": "nf4" if args.base_weight_mode == "qlora_4bit" else None,
            "double_quant": args.base_weight_mode == "qlora_4bit",
            "compute_dtype": "bfloat16",
            "lora_r": args.lora_r,
            "lora_alpha": args.lora_alpha,
            "lora_dropout": args.lora_dropout,
            "trainable_parameters": trainable,
            "total_parameters": total,
            "trainable_percent": round(trainable / total * 100, 6),
            "nccl_p2p_disable": os.environ["NCCL_P2P_DISABLE"],
            "nccl_ib_disable": os.environ["NCCL_IB_DISABLE"],
        },
        "results": {
            "global_step": int(trainer.state.global_step),
            "train_loss": float(train_result.training_loss),
            "last_logged_train_loss": latest_metric(trainer.state.log_history, "loss"),
            "evaluation_loss": float(evaluation["eval_loss"]),
            "elapsed_seconds": elapsed_seconds,
        },
        "gpu": {
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
            "process_local_device": 0,
            "physical_nvidia_smi_device": args.physical_nvidia_smi_device,
            "name": gpu.name,
            "uuid": gpu_uuid,
            "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
            "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
        },
        "versions": {
            "torch": torch.__version__,
            "transformers": __import__("transformers").__version__,
            "peft": __import__("peft").__version__,
            "bitsandbytes": __import__("bitsandbytes").__version__,
        },
        "outputs": {
            "adapter_dir": str(adapter_dir),
            "checkpoint_dir": str(checkpoint_dir),
            "adapter_is_lora_only": True,
            "production_postgres_or_vanna_modified": False,
        },
    }
    evidence_path = args.output_dir / "sft_smoke.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
