"""Generate Base or Adapter SQL candidates for the aligned runtime release."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from time import perf_counter
from typing import Any

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, set_seed


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODEL = Path(
    "/disk2/gengnan/data-analysis-agent-data/models/"
    "qwen2.5-coder-1.5b-base-df3ce67c0e24480f20468b6ef2894622d69eb73b"
)
DEFAULT_ADAPTER = Path(
    "/disk2/gengnan/data-analysis-agent-data/experiments/"
    "runtime-candidate-qwen25coder15b-bf16-lora-2epoch-20261011-r1/adapter_best"
)
DEFAULT_INPUT = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/"
    "runtime-queryplan-aligned-v1-20261012/inputs/inputs.jsonl"
)
VERSION = "runtime-queryplan-aligned-generation-v1"
MAX_INPUT_TOKENS = 4096
MAX_NEW_TOKENS = 1024
SEED = 20261012


class GenerationError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_rows(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise GenerationError("aligned inputs must be non-empty JSONL objects")
    for row in rows:
        forbidden = {"query_spec", "query_spec_id", "candidate_sql", "gold_sql", "training_text"}
        if forbidden & set(row):
            raise GenerationError(f"offline/Gold field leaked into input: {row.get('source_id')}")
        if sha256_text(row["prompt"]) != row["prompt_sha256"]:
            raise GenerationError(f"prompt hash drift: {row.get('source_id')}")
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--adapter-dir", type=Path, default=DEFAULT_ADAPTER)
    parser.add_argument("--inputs", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-label", choices=("base", "adapter"), required=True)
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--expected-gpu-uuid")
    parser.add_argument("--physical-nvidia-smi-device", type=int)
    return parser.parse_args()


def load_model(args: argparse.Namespace):
    if not torch.cuda.is_available():
        raise GenerationError("CUDA is required")
    tokenizer = AutoTokenizer.from_pretrained(args.model_dir, local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"
    model = AutoModelForCausalLM.from_pretrained(
        args.model_dir,
        local_files_only=True,
        torch_dtype=torch.bfloat16,
        device_map={"": 0},
        attn_implementation="sdpa",
    )
    adapter_metadata: dict[str, Any] = {"enabled": False}
    if args.run_label == "adapter":
        if not (args.adapter_dir / "adapter_model.safetensors").is_file():
            raise GenerationError("adapter_model.safetensors is missing")
        model = PeftModel.from_pretrained(model, args.adapter_dir, is_trainable=False)
        adapter_metadata = {
            "enabled": True,
            "adapter_model_sha256": sha256_file(args.adapter_dir / "adapter_model.safetensors"),
            "adapter_config_sha256": sha256_file(args.adapter_dir / "adapter_config.json"),
        }
    model.eval()
    return tokenizer, model, adapter_metadata


def main() -> int:
    args = parse_args()
    if args.max_cases is not None and args.max_cases < 1:
        raise GenerationError("--max-cases must be positive")
    if args.batch_size < 1:
        raise GenerationError("--batch-size must be positive")
    if args.output.exists():
        raise GenerationError(f"refusing to overwrite {args.output}")
    args.output.mkdir(parents=True)
    rows = read_rows(args.inputs)
    selected = rows[: args.max_cases] if args.max_cases is not None else rows
    set_seed(SEED)
    tokenizer, model, adapter_metadata = load_model(args)
    props = torch.cuda.get_device_properties(0)
    gpu_uuid = str(props.uuid)
    if not gpu_uuid.startswith("GPU-"):
        gpu_uuid = "GPU-" + gpu_uuid
    if args.expected_gpu_uuid and gpu_uuid != args.expected_gpu_uuid:
        raise GenerationError(f"GPU UUID mismatch: {gpu_uuid}")
    raw_path = args.output / "raw-completions.jsonl"
    count = 0
    total_tokens = 0
    started = datetime.now(timezone.utc)
    with raw_path.open("x", encoding="utf-8") as stream:
        for start in range(0, len(selected), args.batch_size):
            batch_rows = selected[start : start + args.batch_size]
            encoded = tokenizer(
                [row["prompt"] for row in batch_rows],
                add_special_tokens=False,
                return_tensors="pt",
                padding=True,
            )
            prompt_lengths = encoded["attention_mask"].sum(dim=1).tolist()
            for row, prompt_tokens in zip(batch_rows, prompt_lengths, strict=True):
                if prompt_tokens > MAX_INPUT_TOKENS:
                    raise GenerationError(f"input exceeds {MAX_INPUT_TOKENS}: {row['source_id']}")
            input_width = int(encoded["input_ids"].shape[-1])
            batch = {name: value.to("cuda:0") for name, value in encoded.items()}
            if torch.cuda.is_available():
                torch.cuda.synchronize()
            before = perf_counter()
            with torch.inference_mode():
                output_ids = model.generate(
                    **batch,
                    do_sample=False,
                    num_beams=1,
                    max_new_tokens=MAX_NEW_TOKENS,
                    pad_token_id=tokenizer.pad_token_id,
                    eos_token_id=tokenizer.eos_token_id,
                    use_cache=True,
                )
            if torch.cuda.is_available():
                torch.cuda.synchronize()
            elapsed_ms = round((perf_counter() - before) * 1000)
            for index, row in enumerate(batch_rows):
                completion_ids = output_ids[index, input_width:].detach().cpu().tolist()
                eos_id = tokenizer.eos_token_id
                eos_observed = eos_id is not None and eos_id in completion_ids
                if eos_observed:
                    completion_ids = completion_ids[: completion_ids.index(eos_id) + 1]
                completion = tokenizer.decode(completion_ids, skip_special_tokens=True).strip()
                item = {
                    "source_id": row["source_id"],
                    "workspace": row["workspace"],
                    "prompt_sha256": row["prompt_sha256"],
                    "raw_completion": completion,
                    "raw_completion_sha256": sha256_text(completion),
                    "generated_tokens": len(completion_ids),
                    "eos_observed": eos_observed,
                    "elapsed_ms": elapsed_ms,
                    "batch_index": start // args.batch_size,
                }
                stream.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
                count += 1
                total_tokens += item["generated_tokens"]
            stream.flush()
            os.fsync(stream.fileno())
    evidence = {
        "version": VERSION,
        "run_label": args.run_label,
        "started_at": started.replace(microsecond=0).isoformat(),
        "model_dir": str(args.model_dir),
        "model_manifest_sha256": sha256_file(args.model_dir / "download_manifest.json"),
        "adapter": adapter_metadata,
        "inputs": {"path": str(args.inputs), "sha256": sha256_file(args.inputs), "rows": len(rows)},
        "generation": {
            "generated_rows": count,
            "max_cases": args.max_cases,
            "raw_sha256": sha256_file(raw_path),
            "total_generated_tokens": total_tokens,
            "max_input_tokens": MAX_INPUT_TOKENS,
            "max_new_tokens": MAX_NEW_TOKENS,
            "seed": SEED,
            "do_sample": False,
            "num_beams": 1,
            "batch_size": args.batch_size,
        },
        "gpu": {
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
            "process_local_device": 0,
            "physical_nvidia_smi_device": args.physical_nvidia_smi_device,
            "name": props.name,
            "uuid": gpu_uuid,
        },
        "gold_read_for_generation": False,
        "database_rows_read": False,
    }
    (args.output / "generation-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GenerationError as exc:
        print(f"generation error: {exc}", file=os.sys.stderr)
        raise SystemExit(2) from exc
