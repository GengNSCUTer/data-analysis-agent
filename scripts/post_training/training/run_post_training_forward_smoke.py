#!/usr/bin/env python3
"""Run one train-only QLoRA forward pass and write non-sensitive evidence.

This is a loader, tokenizer and label-mask smoke test. It does not call
``backward()``, update parameters, save an adapter or touch the product runtime.
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
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig


SQL_MARKER = "\n\n### SQL\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--candidates-jsonl", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sample-index", type=int, default=0)
    parser.add_argument("--max-seq-length", type=int, default=1536)
    parser.add_argument("--lora-r", type=int, default=16)
    parser.add_argument("--lora-alpha", type=int, default=32)
    parser.add_argument("--lora-dropout", type=float, default=0.05)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_candidate(path: Path, index: int) -> dict[str, Any]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not 0 <= index < len(rows):
        raise ValueError(f"sample index {index} outside [0, {len(rows)})")
    candidate = rows[index]
    required = {"sample_id", "training_text", "candidate_sql", "split", "execution_outcome"}
    missing = required - candidate.keys()
    if missing:
        raise ValueError(f"candidate lacks required fields: {sorted(missing)}")
    if candidate["split"].get("name") != "train":
        raise ValueError("forward smoke accepts train split only")
    if candidate["execution_outcome"].get("sqlite_readonly_explain") != "pass":
        raise ValueError("forward smoke requires an execution-checked candidate")
    return candidate


def split_prompt_and_target(training_text: str, candidate_sql: str) -> tuple[str, str]:
    if SQL_MARKER not in training_text:
        raise ValueError("training text has no SQL marker")
    prompt, embedded_sql = training_text.rsplit(SQL_MARKER, 1)
    if embedded_sql.strip() != candidate_sql.strip():
        raise ValueError("training text target does not match candidate_sql")
    return prompt + SQL_MARKER, candidate_sql.strip()


def count_parameters(model: torch.nn.Module) -> tuple[int, int]:
    total = sum(parameter.numel() for parameter in model.parameters())
    trainable = sum(parameter.numel() for parameter in model.parameters() if parameter.requires_grad)
    return trainable, total


def main() -> int:
    args = parse_args()
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for the QLoRA forward smoke")
    if args.max_seq_length <= 0:
        raise ValueError("--max-seq-length must be positive")
    candidate = load_candidate(args.candidates_jsonl, args.sample_index)
    prompt, target = split_prompt_and_target(candidate["training_text"], candidate["candidate_sql"])

    tokenizer = AutoTokenizer.from_pretrained(args.model_dir, local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    prompt_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
    target_ids = tokenizer(target, add_special_tokens=False)["input_ids"]
    if not prompt_ids or not target_ids:
        raise ValueError("prompt or target tokenized to an empty sequence")
    input_ids = prompt_ids + target_ids + [tokenizer.eos_token_id]
    if len(input_ids) > args.max_seq_length:
        raise ValueError(
            f"sample uses {len(input_ids)} tokens, above max_seq_length={args.max_seq_length}; "
            "do not silently truncate SQL targets"
        )
    labels = [-100] * len(prompt_ids) + target_ids + [tokenizer.eos_token_id]
    if len(labels) != len(input_ids):
        raise AssertionError("labels/input_ids length mismatch")
    if all(label == -100 for label in labels):
        raise AssertionError("all labels are masked")

    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    quantization = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
    )
    model = AutoModelForCausalLM.from_pretrained(
        args.model_dir,
        local_files_only=True,
        quantization_config=quantization,
        torch_dtype=torch.bfloat16,
        device_map={"": 0},
    )
    model.config.use_cache = False
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
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
    # This is a loader/label-mask smoke only; avoid training-mode checkpoint work.
    model.eval()
    device = torch.device("cuda:0")
    batch = {
        "input_ids": torch.tensor([input_ids], dtype=torch.long, device=device),
        "attention_mask": torch.ones((1, len(input_ids)), dtype=torch.long, device=device),
        "labels": torch.tensor([labels], dtype=torch.long, device=device),
    }
    started = datetime.now(timezone.utc)
    with torch.no_grad():
        output = model(**batch)
    elapsed_seconds = (datetime.now(timezone.utc) - started).total_seconds()
    loss = float(output.loss.detach().float().cpu())
    if not torch.isfinite(torch.tensor(loss)):
        raise RuntimeError(f"non-finite forward loss: {loss}")
    trainable, total = count_parameters(model)
    device_properties = torch.cuda.get_device_properties(0)
    gpu_uuid = str(device_properties.uuid)
    if not gpu_uuid.startswith("GPU-"):
        gpu_uuid = "GPU-" + gpu_uuid
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    model_manifest_path = args.model_dir / "download_manifest.json"
    model_manifest = json.loads(model_manifest_path.read_text(encoding="utf-8"))
    evidence = {
        "experiment_type": "qlora_forward_smoke",
        "started_at": started.replace(microsecond=0).isoformat(),
        "model": {
            "model_id": model_manifest["model_id"],
            "revision": model_manifest["revision"],
            "model_dir": str(args.model_dir),
            "model_download_manifest_sha256": sha256_file(model_manifest_path),
        },
        "dataset": {
            "candidates_jsonl": str(args.candidates_jsonl),
            "candidates_sha256": sha256_file(args.candidates_jsonl),
            "sample_id": candidate["sample_id"],
            "sample_index": args.sample_index,
            "split": candidate["split"],
            "raw_question_or_sql_saved": False,
        },
        "tokenizer": {
            "prompt_tokens": len(prompt_ids),
            "target_tokens": len(target_ids) + 1,
            "sequence_tokens": len(input_ids),
            "masked_prompt_tokens": len(prompt_ids),
            "supervised_tokens": len(target_ids) + 1,
            "max_seq_length": args.max_seq_length,
            "padding_side": tokenizer.padding_side,
            "pad_token_id": tokenizer.pad_token_id,
        },
        "qlora": {
            "load_in_4bit": True,
            "quant_type": "nf4",
            "double_quant": True,
            "compute_dtype": "bfloat16",
            "gradient_checkpointing": True,
            "lora_r": args.lora_r,
            "lora_alpha": args.lora_alpha,
            "lora_dropout": args.lora_dropout,
            "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
            "trainable_parameters": trainable,
            "total_parameters": total,
            "trainable_percent": round(trainable / total * 100, 6),
        },
        "forward": {
            "batch_shape": list(batch["input_ids"].shape),
            "finite_loss": True,
            "loss": loss,
            "elapsed_seconds": elapsed_seconds,
            "backward_or_optimizer_step_run": False,
        },
        "gpu": {
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
            "process_local_device": 0,
            "name": device_properties.name,
            "uuid": gpu_uuid,
            "physical_nvidia_smi_device": 3,
            "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
            "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
        },
        "versions": {
            "torch": torch.__version__,
            "transformers": __import__("transformers").__version__,
            "peft": __import__("peft").__version__,
            "bitsandbytes": __import__("bitsandbytes").__version__,
        },
    }
    evidence_path = output_dir / "forward_smoke.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
