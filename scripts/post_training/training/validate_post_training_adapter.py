#!/usr/bin/env python3
"""Reload a saved LoRA/QLoRA adapter and validate one held-out forward pass.

This is the canonical post-training adapter validation implementation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig


SQL_MARKER = "\n\n### SQL\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--adapter-dir", type=Path, required=True)
    parser.add_argument("--validation-jsonl", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sample-index", type=int, default=0)
    parser.add_argument("--max-seq-length", type=int, default=1536)
    parser.add_argument(
        "--base-weight-mode",
        choices=("qlora_4bit", "bf16_lora"),
        default="qlora_4bit",
        help="The frozen-base storage precision used to create this adapter.",
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
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_row(path: Path, index: int) -> dict[str, Any]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not 0 <= index < len(rows):
        raise ValueError(f"sample index {index} outside [0, {len(rows)})")
    row = rows[index]
    if row.get("split", {}).get("name") != "validation":
        raise ValueError("adapter validation must use validation split")
    return row


def encode(row: dict[str, Any], tokenizer: Any, max_seq_length: int) -> dict[str, torch.Tensor]:
    text, sql = row["training_text"], row["candidate_sql"]
    if SQL_MARKER not in text:
        raise ValueError("training text lacks SQL marker")
    prompt, embedded_sql = text.rsplit(SQL_MARKER, 1)
    if embedded_sql.strip() != sql.strip():
        raise ValueError("candidate SQL differs from encoded target")
    prompt_ids = tokenizer(prompt + SQL_MARKER, add_special_tokens=False)["input_ids"]
    target_ids = tokenizer(sql.strip(), add_special_tokens=False)["input_ids"]
    input_ids = prompt_ids + target_ids + [tokenizer.eos_token_id]
    if len(input_ids) > max_seq_length:
        raise ValueError("validation sample exceeds configured length")
    labels = [-100] * len(prompt_ids) + target_ids + [tokenizer.eos_token_id]
    return {
        "input_ids": torch.tensor([input_ids], dtype=torch.long, device="cuda:0"),
        "attention_mask": torch.ones((1, len(input_ids)), dtype=torch.long, device="cuda:0"),
        "labels": torch.tensor([labels], dtype=torch.long, device="cuda:0"),
    }


def main() -> int:
    args = parse_args()
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required to validate an adapter")
    if not (args.adapter_dir / "adapter_config.json").is_file():
        raise FileNotFoundError(args.adapter_dir / "adapter_config.json")
    if not (args.adapter_dir / "adapter_model.safetensors").is_file():
        raise FileNotFoundError(args.adapter_dir / "adapter_model.safetensors")
    row = load_row(args.validation_jsonl, args.sample_index)
    tokenizer = AutoTokenizer.from_pretrained(args.model_dir, local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    batch = encode(row, tokenizer, args.max_seq_length)
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
    base = AutoModelForCausalLM.from_pretrained(args.model_dir, **model_load_kwargs)
    model = PeftModel.from_pretrained(base, args.adapter_dir, is_trainable=False)
    model.eval()
    started = datetime.now(timezone.utc)
    with torch.no_grad():
        output = model(**batch)
    loss = float(output.loss.detach().float().cpu())
    if not torch.isfinite(torch.tensor(loss)):
        raise RuntimeError(f"adapter validation loss is non-finite: {loss}")
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
        "experiment_type": "adapter_reload_validation",
        "started_at": started.replace(microsecond=0).isoformat(),
        "adapter": {
            "adapter_config_sha256": sha256_file(args.adapter_dir / "adapter_config.json"),
            "adapter_model_sha256": sha256_file(args.adapter_dir / "adapter_model.safetensors"),
            "adapter_model_bytes": (args.adapter_dir / "adapter_model.safetensors").stat().st_size,
            "loaded_with_peft": True,
        },
        "validation": {
            "validation_jsonl_sha256": sha256_file(args.validation_jsonl),
            "sample_id": row["sample_id"],
            "sequence_tokens": int(batch["input_ids"].shape[-1]),
            "raw_question_or_sql_saved": False,
        },
        "result": {"finite_loss": True, "loss": loss},
        "base_weight_mode": args.base_weight_mode,
        "gpu": {
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
            "name": gpu.name,
            "uuid": gpu_uuid,
            "physical_nvidia_smi_device": args.physical_nvidia_smi_device,
            "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
        },
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / "adapter_validation.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
