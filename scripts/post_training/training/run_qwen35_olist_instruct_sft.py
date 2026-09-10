#!/usr/bin/env python3
"""Train a bf16 LoRA adapter for a frozen Qwen3.5 Instruct model on Olist SFT.

The trainer is intentionally separate from the historical Qwen2.5 Coder Base
entry.  It consumes the Qwen3.5 official chat-template layout audit, trains
only LoRA parameters, and records external reproducibility evidence without
copying questions or SQL into Git.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import gc
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
from transformers import AutoModelForImageTextToText, AutoProcessor, Trainer, TrainingArguments, set_seed

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
from data_analysis_agent.qwen35_sft_training import (
    QWEN35_LORA_TARGET_SUFFIXES,
    select_qwen35_language_lora_targets,
    select_bounded_rows,
)
from scripts.post_training.training.run_post_training_sft_smoke import (
    load_rows,
    sha256_file,
    validate_split_audit,
)


DEFAULT_EXPECTED_MODEL_ID = "Qwen/Qwen3.5-4B"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument(
        "--expected-model-id",
        default=DEFAULT_EXPECTED_MODEL_ID,
        help="Frozen model identity required in the model manifest and layout audit.",
    )
    parser.add_argument("--train-jsonl", type=Path, required=True)
    parser.add_argument("--validation-jsonl", type=Path, required=True)
    parser.add_argument("--split-audit", type=Path, required=True)
    parser.add_argument("--layout-audit", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-seq-length", type=int, required=True)
    parser.add_argument("--max-steps", type=int, default=-1)
    parser.add_argument("--num-train-epochs", type=float, default=None)
    parser.add_argument("--max-train-samples", type=int, default=None)
    parser.add_argument("--max-validation-samples", type=int, default=None)
    parser.add_argument(
        "--sample-selection",
        choices=("first", "shortest_sequence", "longest_sequence"),
        default="first",
        help=(
            "How a deliberately bounded smoke subset is selected. Formal runs "
            "must use the default because they consume every frozen row."
        ),
    )
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--learning-rate", type=float, required=True)
    parser.add_argument("--weight-decay", type=float, required=True)
    parser.add_argument("--per-device-train-batch-size", type=int, required=True)
    parser.add_argument("--gradient-accumulation-steps", type=int, required=True)
    parser.add_argument("--per-device-eval-batch-size", type=int, required=True)
    parser.add_argument("--evaluation-steps", type=int, required=True)
    parser.add_argument("--save-steps", type=int, required=True)
    parser.add_argument("--logging-steps", type=int, required=True)
    parser.add_argument("--lora-r", type=int, required=True)
    parser.add_argument("--lora-alpha", type=int, required=True)
    parser.add_argument("--lora-dropout", type=float, required=True)
    parser.add_argument("--physical-nvidia-smi-device", type=int, required=True)
    parser.add_argument("--expected-gpu-uuid", required=True)
    parser.add_argument("--experiment-label", required=True)
    return parser.parse_args()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Qwen35SftFormatError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise Qwen35SftFormatError(f"{label} must be an object")
    return value


def _positive(value: int | float, label: str) -> None:
    if value <= 0:
        raise Qwen35SftFormatError(f"{label} must be positive")


def validate_layout_audit(
    layout: dict[str, Any],
    *,
    model_dir: Path,
    train_jsonl: Path,
    validation_jsonl: Path,
    split_audit: Path,
    max_seq_length: int,
    expected_model_id: str,
) -> None:
    """Bind parameter-update inputs to the completed Qwen3.5 CPU preflight."""

    template = layout.get("template")
    model = layout.get("model")
    source = layout.get("source")
    splits = layout.get("splits")
    manifest = _read_json(model_dir / "download_manifest.json", "Qwen3.5 model manifest")
    if not all(isinstance(value, dict) for value in (template, model, source, splits)):
        raise Qwen35SftFormatError("layout audit lacks template, model, source, or split evidence")
    if (
        layout.get("report_schema_version") != "qwen35-olist-sft-layout-audit-v1"
        or template.get("version") != QWEN35_OLIST_SFT_TEMPLATE_VERSION
        or template.get("official_chat_template") is not True
        or template.get("enable_thinking") is not False
        or template.get("silent_truncation") is not False
        or model.get("id") != expected_model_id
        or model.get("id") != manifest.get("model_id")
        or model.get("revision") != manifest.get("revision")
        or model.get("download_manifest_sha256") != sha256_file(model_dir / "download_manifest.json")
        or source.get("train_jsonl_sha256") != sha256_file(train_jsonl)
        or source.get("validation_jsonl_sha256") != sha256_file(validation_jsonl)
        or source.get("split_audit_sha256") != sha256_file(split_audit)
        or source.get("thelook_used") is not False
        or source.get("in_domain_test_forbidden_for_training") is not True
        or layout.get("max_seq_length") != max_seq_length
    ):
        raise Qwen35SftFormatError("layout audit differs from frozen Qwen3.5 training inputs")
    for split_name, expected_rows in (("train", 2400), ("validation", 600)):
        stats = splits.get(split_name)
        if (
            not isinstance(stats, dict)
            or stats.get("rows") != expected_rows
            or not isinstance(stats.get("max_sequence_tokens"), int)
            or stats["max_sequence_tokens"] > max_seq_length
            or stats.get("at_max_seq_length") != 0
        ):
            raise Qwen35SftFormatError(f"layout audit lacks valid {split_name} length evidence")


class Qwen35SqlDataset(Dataset[dict[str, torch.Tensor]]):
    """Tokenize the frozen Qwen3.5 template and refuse every silent change."""

    def __init__(self, rows: list[dict[str, Any]], processor: Any, max_seq_length: int) -> None:
        self.examples: list[dict[str, torch.Tensor]] = []
        self.stats = {"samples": 0, "max_sequence_tokens": 0, "max_target_tokens": 0}
        for row in rows:
            example = build_qwen35_sql_sft_example(
                processor,
                str(row["rendered_prompt"]),
                str(row["candidate_sql"]),
                max_seq_length=max_seq_length,
            )
            self.examples.append(
                {
                    "input_ids": torch.tensor(example.input_ids, dtype=torch.long),
                    "attention_mask": torch.tensor(example.attention_mask, dtype=torch.long),
                    "labels": torch.tensor(example.labels, dtype=torch.long),
                }
            )
            self.stats["samples"] += 1
            self.stats["max_sequence_tokens"] = max(
                self.stats["max_sequence_tokens"], len(example.input_ids)
            )
            self.stats["max_target_tokens"] = max(
                self.stats["max_target_tokens"], example.supervised_tokens
            )

    def __len__(self) -> int:
        return len(self.examples)

    def __getitem__(self, index: int) -> dict[str, torch.Tensor]:
        return self.examples[index]


class RightPaddingCollator:
    def __init__(self, pad_token_id: int) -> None:
        self.pad_token_id = pad_token_id

    def __call__(self, features: list[dict[str, torch.Tensor]]) -> dict[str, torch.Tensor]:
        max_length = max(item["input_ids"].size(0) for item in features)
        batch: dict[str, list[torch.Tensor]] = {"input_ids": [], "attention_mask": [], "labels": []}
        for item in features:
            padding = max_length - item["input_ids"].size(0)
            batch["input_ids"].append(
                torch.nn.functional.pad(item["input_ids"], (0, padding), value=self.pad_token_id)
            )
            batch["attention_mask"].append(
                torch.nn.functional.pad(item["attention_mask"], (0, padding), value=0)
            )
            batch["labels"].append(
                torch.nn.functional.pad(item["labels"], (0, padding), value=-100)
            )
        return {name: torch.stack(items) for name, items in batch.items()}


def current_gpu_identity() -> tuple[Any, str]:
    properties = torch.cuda.get_device_properties(0)
    uuid = str(properties.uuid)
    return properties, uuid if uuid.startswith("GPU-") else f"GPU-{uuid}"


def count_parameters(model: torch.nn.Module) -> tuple[int, int]:
    total = sum(parameter.numel() for parameter in model.parameters())
    trainable = sum(parameter.numel() for parameter in model.parameters() if parameter.requires_grad)
    return trainable, total


def latest_metric(history: list[dict[str, Any]], key: str) -> float | None:
    values = [float(item[key]) for item in history if key in item]
    return values[-1] if values else None


def _load_base_model(model_dir: Path) -> Any:
    model = AutoModelForImageTextToText.from_pretrained(
        model_dir,
        local_files_only=True,
        dtype=torch.bfloat16,
    ).to("cuda:0")
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    return model


def _to_cuda(batch: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
    return {name: value.to("cuda:0") for name, value in batch.items()}


def main() -> int:
    args = parse_args()
    for value, label in (
        (args.max_seq_length, "max sequence length"),
        (args.learning_rate, "learning rate"),
        (args.per_device_train_batch_size, "train batch size"),
        (args.per_device_eval_batch_size, "eval batch size"),
        (args.gradient_accumulation_steps, "gradient accumulation steps"),
        (args.evaluation_steps, "evaluation steps"),
        (args.save_steps, "save steps"),
        (args.logging_steps, "logging steps"),
        (args.lora_r, "LoRA rank"),
        (args.lora_alpha, "LoRA alpha"),
    ):
        _positive(value, label)
    if args.weight_decay < 0 or not 0 <= args.lora_dropout < 1:
        raise Qwen35SftFormatError("weight decay or LoRA dropout is outside its valid range")
    if (args.max_steps > 0) == (args.num_train_epochs is not None):
        raise Qwen35SftFormatError("set exactly one of positive max steps or num train epochs")
    if args.num_train_epochs is not None:
        _positive(args.num_train_epochs, "num train epochs")
    if not torch.cuda.is_available():
        raise Qwen35SftFormatError("CUDA is required for Qwen3.5 SFT")
    gpu, gpu_uuid = current_gpu_identity()
    if gpu_uuid != args.expected_gpu_uuid:
        raise Qwen35SftFormatError(
            f"CUDA UUID guard failed: expected {args.expected_gpu_uuid}, got {gpu_uuid}"
        )
    for path in (args.model_dir, args.train_jsonl, args.validation_jsonl, args.split_audit, args.layout_audit):
        ensure_path_outside_repository(path, ROOT)
    output_dir = ensure_path_outside_repository(args.output_dir, ROOT)
    if output_dir.exists():
        raise Qwen35SftFormatError("output directory must be a new external path")
    # Create the immutable external evidence root before Trainer sees its
    # checkpoint child.  Creating it after save_model is too late because
    # Trainer creates `adapter_checkpoints` itself.
    output_dir.mkdir(parents=True, exist_ok=False)

    layout_audit = _read_json(args.layout_audit, "Qwen3.5 layout audit")
    validate_layout_audit(
        layout_audit,
        model_dir=args.model_dir,
        train_jsonl=args.train_jsonl,
        validation_jsonl=args.validation_jsonl,
        split_audit=args.split_audit,
        max_seq_length=args.max_seq_length,
        expected_model_id=args.expected_model_id,
    )
    split_audit = _read_json(args.split_audit, "Olist split audit")
    validate_split_audit(split_audit, args.train_jsonl, args.validation_jsonl)
    all_train_rows = load_rows(args.train_jsonl, "train")
    all_validation_rows = load_rows(args.validation_jsonl, "validation")
    random.seed(args.seed)
    np.random.seed(args.seed)
    set_seed(args.seed)
    processor = AutoProcessor.from_pretrained(args.model_dir, local_files_only=True)
    if processor.tokenizer.pad_token_id is None:
        processor.tokenizer.pad_token = processor.tokenizer.eos_token
    processor.tokenizer.padding_side = "right"
    def sequence_length(row: dict[str, Any]) -> int:
        return len(
            build_qwen35_sql_sft_example(
                processor,
                str(row["rendered_prompt"]),
                str(row["candidate_sql"]),
                max_seq_length=args.max_seq_length,
            ).input_ids
        )

    train_rows = select_bounded_rows(
        all_train_rows,
        args.max_train_samples,
        "train",
        args.sample_selection,
        sequence_length,
    )
    validation_rows = select_bounded_rows(
        all_validation_rows,
        args.max_validation_samples,
        "validation",
        args.sample_selection,
        sequence_length,
    )
    train_dataset = Qwen35SqlDataset(train_rows, processor, args.max_seq_length)
    validation_dataset = Qwen35SqlDataset(validation_rows, processor, args.max_seq_length)

    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    base_model = _load_base_model(args.model_dir)
    target_names = select_qwen35_language_lora_targets(name for name, _ in base_model.named_modules())
    target_counts = dict(sorted(Counter(name.rsplit(".", 1)[-1] for name in target_names).items()))
    model = get_peft_model(
        base_model,
        LoraConfig(
            r=args.lora_r,
            lora_alpha=args.lora_alpha,
            lora_dropout=args.lora_dropout,
            bias="none",
            # Use the explicitly enumerated text-module paths rather than
            # suffix matching.  The selector has already rejected ambiguous
            # same-suffix modules outside `model.language_model.layers.*`.
            target_modules=list(target_names),
            task_type="CAUSAL_LM",
        ),
    )
    trainable, total = count_parameters(model)
    if trainable <= 0 or trainable >= total:
        raise Qwen35SftFormatError("LoRA injection did not leave a frozen base with trainable adapters")

    checkpoint_dir = output_dir / "adapter_checkpoints"
    training_args = TrainingArguments(
        output_dir=str(checkpoint_dir),
        do_train=True,
        do_eval=True,
        eval_strategy="steps",
        eval_steps=args.evaluation_steps,
        per_device_train_batch_size=args.per_device_train_batch_size,
        per_device_eval_batch_size=args.per_device_eval_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        learning_rate=args.learning_rate,
        max_steps=args.max_steps,
        num_train_epochs=args.num_train_epochs if args.num_train_epochs is not None else 1.0,
        lr_scheduler_type="constant",
        logging_strategy="steps",
        logging_steps=args.logging_steps,
        logging_first_step=True,
        save_strategy="steps",
        save_steps=args.save_steps,
        save_total_limit=2,
        bf16=True,
        tf32=True,
        optim="adamw_torch",
        weight_decay=args.weight_decay,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        report_to=[],
        remove_unused_columns=False,
        seed=args.seed,
        data_seed=args.seed,
        dataloader_num_workers=0,
    )
    collator = RightPaddingCollator(processor.tokenizer.pad_token_id)
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=validation_dataset,
        data_collator=collator,
    )
    started_at = datetime.now(timezone.utc)
    train_result = trainer.train()
    evaluation = trainer.evaluate()
    adapter_dir = output_dir / "adapter_final"
    trainer.save_model(str(adapter_dir))
    trainer.save_state()
    global_step = int(trainer.state.global_step)
    last_logged_train_loss = latest_metric(trainer.state.log_history, "loss")

    # A successful Trainer save is insufficient evidence: reload the adapter
    # into a fresh bf16 base and prove one masked validation forward is finite.
    reload_batch = _to_cuda(collator([validation_dataset[0]]))
    del trainer, model, base_model
    gc.collect()
    torch.cuda.empty_cache()
    reload_base = _load_base_model(args.model_dir)
    reloaded = PeftModel.from_pretrained(reload_base, adapter_dir, is_trainable=False)
    reloaded.eval()
    with torch.no_grad():
        reloaded_output = reloaded(**reload_batch)
    reload_loss = float(reloaded_output.loss.detach().float().cpu())
    if not torch.isfinite(torch.tensor(reload_loss)):
        raise Qwen35SftFormatError("fresh Qwen3.5 adapter reload produced non-finite loss")

    manifest = _read_json(args.model_dir / "download_manifest.json", "Qwen3.5 model manifest")
    elapsed_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
    evidence = {
        "report_schema_version": "qwen35-olist-instruct-sft-run-v1",
        "experiment_label": args.experiment_label,
        "started_at": started_at.replace(microsecond=0).isoformat(),
        "model": {
            "id": manifest["model_id"],
            "revision": manifest["revision"],
            "download_manifest_sha256": sha256_file(args.model_dir / "download_manifest.json"),
            "base_weight_mode": "bf16_frozen",
        },
        "template": {
            "version": QWEN35_OLIST_SFT_TEMPLATE_VERSION,
            "official_chat_template": True,
            "enable_thinking": False,
            "layout_audit_sha256": sha256_file(args.layout_audit),
        },
        "data": {
            "train_jsonl_sha256": sha256_file(args.train_jsonl),
            "validation_jsonl_sha256": sha256_file(args.validation_jsonl),
            "split_audit_sha256": sha256_file(args.split_audit),
            "train": train_dataset.stats,
            "validation": validation_dataset.stats,
            "selected_train_samples": len(train_rows),
            "selected_validation_samples": len(validation_rows),
            "bounded_sample_selection": args.sample_selection,
            "thelook_used": False,
            "raw_question_or_sql_saved": False,
        },
        "training": {
            "seed": args.seed,
            "max_seq_length": args.max_seq_length,
            "max_steps": args.max_steps,
            "num_train_epochs": args.num_train_epochs,
            "per_device_train_batch_size": args.per_device_train_batch_size,
            "per_device_eval_batch_size": args.per_device_eval_batch_size,
            "gradient_accumulation_steps": args.gradient_accumulation_steps,
            "global_batch_size": args.per_device_train_batch_size * args.gradient_accumulation_steps,
            "learning_rate": args.learning_rate,
            "optimizer": "adamw_torch",
            "optimizer_state_quantized": False,
            "weight_decay": args.weight_decay,
            "gradient_checkpointing": True,
            "compute_dtype": "bfloat16",
            "lora_r": args.lora_r,
            "lora_alpha": args.lora_alpha,
            "lora_dropout": args.lora_dropout,
            "lora_target_module_count": len(target_names),
            "lora_target_counts": target_counts,
            "trainable_parameters": trainable,
            "total_parameters": total,
            "trainable_percent": round(trainable / total * 100, 6),
        },
        "results": {
            "global_step": global_step,
            "train_loss": float(train_result.training_loss),
            "last_logged_train_loss": last_logged_train_loss,
            "evaluation_loss": float(evaluation["eval_loss"]),
            "fresh_reload_finite_loss": reload_loss,
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
        "boundaries": {
            "production_default_unchanged": True,
            "thelook_final_test_used": False,
            "raw_artifacts_outside_repository": True,
        },
    }
    (output_dir / "sft_run.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"results": evidence["results"], "training": evidence["training"], "gpu": evidence["gpu"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Qwen35SftFormatError, ValueError, RuntimeError) as exc:
        print(f"Qwen3.5 Olist SFT error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
