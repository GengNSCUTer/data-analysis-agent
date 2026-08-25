#!/usr/bin/env python3
"""Generate deterministic Spider dev SQL with a Qwen base model or LoRA adapter.

This offline research runner uses the same schema/question serialization as the
SFT corpus, but never reads Spider dev gold SQL or database rows for inference.
It emits only external candidate JSONL and a non-sensitive evidence summary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping

import numpy as np
import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, set_seed

from data_analysis_agent.external_artifacts import ensure_path_outside_repository
from data_analysis_agent.spider_sft_format import (
    PROMPT_FORMAT_VERSION,
    render_sft_prompt,
    serialize_spider_schema,
)


ROOT = Path(__file__).resolve().parents[1]


class GenerationInputError(ValueError):
    """A prediction run would violate the frozen comparison contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--adapter-dir", type=Path)
    parser.add_argument("--run-label", choices=("base", "adapter"), required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--tables-json", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-input-tokens", type=int, default=1536)
    parser.add_argument("--max-new-tokens", type=int, default=256)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--base-weight-mode",
        choices=("qlora_4bit", "bf16_lora"),
        default="qlora_4bit",
        help="Frozen-base storage precision; both modes generate with a LoRA adapter when loaded.",
    )
    parser.add_argument(
        "--physical-nvidia-smi-device",
        type=int,
        default=None,
        help="Physical nvidia-smi device ID recorded by a guarded launcher.",
    )
    parser.add_argument(
        "--expected-gpu-uuid",
        default=None,
        help="Optional physical GPU UUID guard; rejects an unexpected CUDA mapping.",
    )
    return parser.parse_args()


def load_json_list(path: Path, label: str) -> list[Mapping[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GenerationInputError(f"{label} does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GenerationInputError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise GenerationInputError(f"{label} must be a JSON list of objects")
    return value


def require_dev_cases_without_gold(cases: list[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """Return only in-memory db_id/question values; do not inspect ``query``."""

    normalized: list[tuple[str, str]] = []
    for index, case in enumerate(cases):
        database_id = case.get("db_id")
        question = case.get("question")
        if not isinstance(database_id, str) or not database_id.strip():
            raise GenerationInputError(f"case {index} has no db_id")
        if not isinstance(question, str) or not question.strip():
            raise GenerationInputError(f"case {index} has no question")
        normalized.append((database_id.strip(), question))
    if not normalized:
        raise GenerationInputError("case list is empty")
    return normalized


def table_mapping(rows: list[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    mapping: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        database_id = row.get("db_id")
        if not isinstance(database_id, str) or not database_id.strip():
            raise GenerationInputError("table metadata has no db_id")
        if database_id in mapping:
            raise GenerationInputError(f"duplicate table metadata db_id: {database_id}")
        mapping[database_id] = row
    return mapping


def load_existing_case_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    existing: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GenerationInputError(f"existing prediction JSONL is invalid on line {line_number}") from exc
        if not isinstance(row, Mapping):
            raise GenerationInputError(f"existing prediction JSONL line {line_number} is not an object")
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or not case_id.startswith("spider_dev:"):
            raise GenerationInputError("existing prediction JSONL has an invalid case ID")
        if row.get("candidate_index", 0) != 0:
            raise GenerationInputError("only candidate_index=0 is supported")
        if case_id in existing:
            raise GenerationInputError("existing prediction JSONL has duplicate case IDs")
        existing.add(case_id)
    return existing


def append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def decode_sql(tokenizer: Any, token_ids: torch.Tensor) -> str:
    text = tokenizer.decode(token_ids, skip_special_tokens=True).strip()
    if not text:
        raise GenerationInputError("model generated an empty completion")
    return text


def load_model(args: argparse.Namespace) -> tuple[Any, Any, dict[str, Any]]:
    tokenizer = AutoTokenizer.from_pretrained(args.model_dir, local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"
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
    adapter_metadata: dict[str, Any] = {"enabled": False}
    if args.run_label == "adapter":
        if args.adapter_dir is None:
            raise GenerationInputError("adapter run requires --adapter-dir")
        config_path = args.adapter_dir / "adapter_config.json"
        model_path = args.adapter_dir / "adapter_model.safetensors"
        if not config_path.is_file() or not model_path.is_file():
            raise GenerationInputError("adapter directory lacks PEFT adapter files")
        model = PeftModel.from_pretrained(model, args.adapter_dir, is_trainable=False)
        adapter_metadata = {
            "enabled": True,
            "adapter_config_sha256": sha256_file(config_path),
            "adapter_model_sha256": sha256_file(model_path),
            "adapter_model_bytes": model_path.stat().st_size,
        }
    elif args.adapter_dir is not None:
        raise GenerationInputError("base run must not receive --adapter-dir")
    model.eval()
    return tokenizer, model, adapter_metadata


def main() -> int:
    args = parse_args()
    # Accelerate validates host-wide RTX 40-series P2P even for a single visible GPU.
    os.environ.setdefault("NCCL_P2P_DISABLE", "1")
    os.environ.setdefault("NCCL_IB_DISABLE", "1")
    if not torch.cuda.is_available():
        raise GenerationInputError("CUDA is required for deterministic Qwen generation")
    if args.max_input_tokens <= 0 or args.max_new_tokens <= 0:
        raise GenerationInputError("token budgets must be positive")
    if args.max_cases is not None and args.max_cases <= 0:
        raise GenerationInputError("--max-cases must be positive")
    output_dir = ensure_path_outside_repository(args.output_dir, ROOT)
    if args.cases.resolve().is_relative_to(ROOT) or args.tables_json.resolve().is_relative_to(ROOT):
        raise GenerationInputError("Spider benchmark inputs must remain outside the repository")
    if args.adapter_dir is not None:
        ensure_path_outside_repository(args.adapter_dir, ROOT)
    model_manifest_path = args.model_dir / "download_manifest.json"
    ensure_path_outside_repository(args.model_dir, ROOT)
    if not model_manifest_path.is_file():
        raise GenerationInputError("model directory lacks download_manifest.json")
    model_manifest = json.loads(model_manifest_path.read_text(encoding="utf-8"))
    if model_manifest.get("model_id") != "Qwen/Qwen2.5-Coder-1.5B":
        raise GenerationInputError("comparison runner requires the frozen Qwen 1.5B base model")

    output_dir.mkdir(parents=True, exist_ok=True)
    predictions_path = output_dir / "predictions.jsonl"
    evidence_path = output_dir / "generation_evidence.json"
    if evidence_path.exists():
        raise GenerationInputError("generation evidence already exists; choose a new output directory")
    existing_case_ids = load_existing_case_ids(predictions_path)
    if predictions_path.exists() and not args.resume:
        raise GenerationInputError("prediction output already exists; use --resume or choose a new directory")

    cases = require_dev_cases_without_gold(load_json_list(args.cases, "Spider dev cases"))
    tables = table_mapping(load_json_list(args.tables_json, "Spider tables metadata"))
    expected_case_ids = [f"spider_dev:{index:05d}" for index in range(len(cases))]
    selected = [
        (case_id, database_id, question)
        for case_id, (database_id, question) in zip(expected_case_ids, cases, strict=True)
        if case_id not in existing_case_ids
    ]
    if args.max_cases is not None:
        selected = selected[: args.max_cases]

    random.seed(args.seed)
    np.random.seed(args.seed)
    set_seed(args.seed)
    tokenizer, model, adapter_metadata = load_model(args)
    gpu = torch.cuda.get_device_properties(0)
    gpu_uuid = str(gpu.uuid)
    if not gpu_uuid.startswith("GPU-"):
        gpu_uuid = "GPU-" + gpu_uuid
    if args.expected_gpu_uuid is not None and gpu_uuid != args.expected_gpu_uuid:
        raise GenerationInputError(
            "CUDA device UUID does not match launcher guard: "
            f"expected {args.expected_gpu_uuid}, got {gpu_uuid}"
        )
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    started = datetime.now(timezone.utc)
    generated_count = 0
    generated_tokens = 0
    unknown_generated_tokens = 0
    max_prompt_tokens = 0
    max_observed_total_tokens = 0
    total_generation_elapsed_ms = 0
    for case_id, database_id, question in selected:
        table = tables.get(database_id)
        if table is None:
            raise GenerationInputError(f"case {case_id} refers to missing table metadata: {database_id}")
        prompt = render_sft_prompt(question, serialize_spider_schema(table))
        encoded = tokenizer(prompt, add_special_tokens=False, return_tensors="pt")
        input_ids = encoded["input_ids"]
        if input_ids.shape[-1] > args.max_input_tokens:
            raise GenerationInputError(
                f"case {case_id} exceeds max input tokens; refuse to truncate the schema prompt"
            )
        max_prompt_tokens = max(max_prompt_tokens, int(input_ids.shape[-1]))
        batch = {name: value.to("cuda:0") for name, value in encoded.items()}
        started_case = perf_counter()
        with torch.inference_mode():
            output_ids = model.generate(
                **batch,
                do_sample=False,
                num_beams=1,
                max_new_tokens=args.max_new_tokens,
                pad_token_id=tokenizer.pad_token_id,
                eos_token_id=tokenizer.eos_token_id,
                use_cache=True,
            )
        elapsed_ms = round((perf_counter() - started_case) * 1_000)
        completion_ids = output_ids[0, input_ids.shape[-1] :]
        candidate_sql = decode_sql(tokenizer, completion_ids)
        token_count = int(completion_ids.shape[-1])
        append_jsonl(
            predictions_path,
            {
                "case_id": case_id,
                "candidate_sql": candidate_sql,
                "candidate_index": 0,
                "generated_tokens": token_count,
                "generation_elapsed_ms": elapsed_ms,
            },
        )
        generated_count += 1
        generated_tokens += token_count
        total_generation_elapsed_ms += elapsed_ms
        max_observed_total_tokens = max(max_observed_total_tokens, int(output_ids.shape[-1]))

    evidence = {
        "experiment_type": "qwen25coder15b_base_adapter_generation",
        "run_label": args.run_label,
        "started_at": started.replace(microsecond=0).isoformat(),
        "model": {
            "id": model_manifest["model_id"],
            "revision": model_manifest["revision"],
            "download_manifest_sha256": sha256_file(model_manifest_path),
            "base_weight_mode": args.base_weight_mode,
            "load_in_4bit": args.base_weight_mode == "qlora_4bit",
            "quant_type": "nf4" if args.base_weight_mode == "qlora_4bit" else None,
            "double_quant": args.base_weight_mode == "qlora_4bit",
            "compute_dtype": "bfloat16",
        },
        "adapter": adapter_metadata,
        "comparison_contract": {
            "prompt_format_version": PROMPT_FORMAT_VERSION,
            "dataset": "spider_dev",
            "cases_sha256": sha256_file(args.cases),
            "tables_sha256": sha256_file(args.tables_json),
            "decode": {
                "do_sample": False,
                "num_beams": 1,
                "max_new_tokens": args.max_new_tokens,
                "seed": args.seed,
            },
            "spider_dev_gold_sql_read_for_generation": False,
            "raw_questions_or_prompts_written": False,
            "raw_database_rows_read": False,
        },
        "generation": {
            "native_case_count": len(cases),
            "generated_this_invocation": generated_count,
            "existing_prediction_case_count": len(existing_case_ids),
            "max_cases": args.max_cases,
            "prediction_jsonl_sha256": sha256_file(predictions_path) if predictions_path.exists() else None,
            "total_generated_tokens_this_invocation": generated_tokens,
            "unknown_generated_tokens": unknown_generated_tokens,
            "total_generation_elapsed_ms_this_invocation": total_generation_elapsed_ms,
            "max_prompt_tokens": max_prompt_tokens,
            "max_observed_total_tokens": max_observed_total_tokens,
        },
        "gpu": {
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
            "process_local_device": 0,
            "physical_nvidia_smi_device": args.physical_nvidia_smi_device,
            "name": gpu.name,
            "uuid": gpu_uuid,
            "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
            "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
            "nccl_p2p_disable": os.environ["NCCL_P2P_DISABLE"],
            "nccl_ib_disable": os.environ["NCCL_IB_DISABLE"],
        },
        "boundaries": {
            "prediction_output_outside_git": True,
            "production_postgres_or_vanna_modified": False,
            "official_leaderboard_claim": False,
        },
    }
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GenerationInputError as exc:
        print(f"generation input error: {exc}", file=os.sys.stderr)
        raise SystemExit(2) from exc
