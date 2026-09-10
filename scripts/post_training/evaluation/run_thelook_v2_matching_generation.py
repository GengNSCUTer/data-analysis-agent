#!/usr/bin/env python3
"""Generate one protected TheLook v2 Base *or* Adapter completion set.

This command is generation-only.  It projects no ``gold_sql`` from the final
test, opens no PostgreSQL connection, and does not execute generated SQL.
The subsequent pair-verifier must accept both external completion files before
the separate execution/Gold-denotation command may run.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import sys
from time import perf_counter
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from data_analysis_agent.external_artifacts import ensure_path_outside_repository
from data_analysis_agent.olist_candidate_sql_evaluation import (
    CandidateEvaluationRecord,
    build_safe_report,
)
from data_analysis_agent.thelook_v2_matching import (
    EXPECTED_BASE_WEIGHT_MODE,
    EXPECTED_CASES,
    EXPECTED_MAX_INPUT_TOKENS,
    EXPECTED_MAX_NEW_TOKENS,
    EXPECTED_MODEL_ID,
    EXPECTED_MODEL_REVISION,
    EXPECTED_PROMPT_BUNDLE_SHA256,
    EXPECTED_PROMPT_TOKEN_MAX,
    EXPECTED_PROMPT_TOKEN_MIN,
    EXPECTED_SEED,
    MATCHING_CONTRACT_VERSION,
    PROMPT_VERSION,
    TheLookV2MatchingError,
    prompt_bundle_sha256,
    read_generation_cases,
    render_catalog_prompt,
    render_generation_prompt,
    sha256_file,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-jsonl", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--run-label", choices=("base", "adapter"), required=True)
    parser.add_argument("--adapter-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--max-input-tokens", type=int, default=EXPECTED_MAX_INPUT_TOKENS
    )
    parser.add_argument("--max-new-tokens", type=int, default=EXPECTED_MAX_NEW_TOKENS)
    parser.add_argument("--seed", type=int, default=EXPECTED_SEED)
    parser.add_argument("--physical-nvidia-smi-device", type=int, required=True)
    parser.add_argument("--expected-gpu-uuid", required=True)
    return parser.parse_args()


def _read_model_manifest(model_dir: Path) -> Mapping[str, Any]:
    try:
        manifest = json.loads(
            (model_dir / "download_manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise TheLookV2MatchingError(
            "base model download manifest is unavailable"
        ) from exc
    if not isinstance(manifest, Mapping):
        raise TheLookV2MatchingError("base model download manifest must be an object")
    if (
        manifest.get("model_id") != EXPECTED_MODEL_ID
        or manifest.get("revision") != EXPECTED_MODEL_REVISION
    ):
        raise TheLookV2MatchingError(
            "base model identity differs from frozen Olist release v2 base"
        )
    return manifest


def load_model(
    model_dir: Path, run_label: str, adapter_dir: Path | None
) -> tuple[Any, Any, dict[str, Any], Mapping[str, Any]]:
    """Load the identical bf16 base, optionally adding the frozen Olist adapter."""

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    manifest = _read_model_manifest(model_dir)
    tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"
    model = AutoModelForCausalLM.from_pretrained(
        model_dir,
        local_files_only=True,
        torch_dtype=torch.bfloat16,
        device_map={"": 0},
    )
    adapter: dict[str, Any] = {"enabled": False}
    if run_label == "adapter":
        if adapter_dir is None:
            raise TheLookV2MatchingError("adapter run requires --adapter-dir")
        model_path = adapter_dir / "adapter_model.safetensors"
        config_path = adapter_dir / "adapter_config.json"
        if not model_path.is_file() or not config_path.is_file():
            raise TheLookV2MatchingError("adapter artifacts are incomplete")
        model = PeftModel.from_pretrained(model, adapter_dir, is_trainable=False)
        adapter = {
            "enabled": True,
            "adapter_model_sha256": sha256_file(model_path),
            "adapter_config_sha256": sha256_file(config_path),
            "adapter_model_bytes": model_path.stat().st_size,
        }
    elif adapter_dir is not None:
        raise TheLookV2MatchingError("base run cannot receive an adapter")
    model.eval()
    return tokenizer, model, adapter, manifest


def generate_completion(
    tokenizer: Any, model: Any, prompt: str, max_input: int, max_new: int
) -> tuple[str, int, int]:
    """Greedily produce one raw completion; presentation cleanup happens later."""

    import torch

    encoded = tokenizer(prompt, add_special_tokens=False, return_tensors="pt")
    input_length = int(encoded["input_ids"].shape[-1])
    if input_length > max_input:
        raise TheLookV2MatchingError(
            f"prompt has {input_length} tokens, exceeding the frozen {max_input}-token limit"
        )
    started = perf_counter()
    with torch.inference_mode():
        output = model.generate(
            **{key: value.to("cuda:0") for key, value in encoded.items()},
            do_sample=False,
            num_beams=1,
            max_new_tokens=max_new,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
            use_cache=True,
        )
    tokens = output[0, input_length:]
    completion = tokenizer.decode(tokens, skip_special_tokens=True).strip()
    if not completion:
        raise TheLookV2MatchingError("model generated an empty completion")
    return completion, int(tokens.shape[-1]), round((perf_counter() - started) * 1000)


def main() -> int:
    args = parse_args()
    import numpy as np
    import torch
    from transformers import set_seed

    if (
        args.max_input_tokens != EXPECTED_MAX_INPUT_TOKENS
        or args.max_new_tokens != EXPECTED_MAX_NEW_TOKENS
        or args.seed != EXPECTED_SEED
    ):
        raise TheLookV2MatchingError(
            "generation parameters differ from the frozen v2 matching contract"
        )
    if not torch.cuda.is_available():
        raise TheLookV2MatchingError("CUDA is required for matching generation")
    for path in (args.cases_jsonl, args.manifest, args.model_dir):
        ensure_path_outside_repository(path, ROOT)
    output_dir = ensure_path_outside_repository(args.output_dir, ROOT)
    if output_dir.exists():
        raise TheLookV2MatchingError("generation output directory must be new")
    adapter_dir = (
        ensure_path_outside_repository(args.adapter_dir, ROOT)
        if args.adapter_dir
        else None
    )

    cases, frozen_manifest = read_generation_cases(args.cases_jsonl, args.manifest)
    catalog_prompt = render_catalog_prompt()
    prompts = [
        render_generation_prompt(case, catalog_prompt=catalog_prompt) for case in cases
    ]
    random.seed(args.seed)
    np.random.seed(args.seed)
    set_seed(args.seed)
    tokenizer, model, adapter, model_manifest = load_model(
        args.model_dir, args.run_label, adapter_dir
    )
    prompt_token_lengths = [
        len(tokenizer(prompt, add_special_tokens=False)["input_ids"])
        for prompt in prompts
    ]
    prompt_preflight = {
        "count": len(prompt_token_lengths),
        "min": min(prompt_token_lengths),
        "max": max(prompt_token_lengths),
        "at_input_limit": sum(
            length == args.max_input_tokens for length in prompt_token_lengths
        ),
    }
    if (
        prompt_bundle_sha256(prompts) != EXPECTED_PROMPT_BUNDLE_SHA256
        or prompt_preflight
        != {
            "count": EXPECTED_CASES,
            "min": EXPECTED_PROMPT_TOKEN_MIN,
            "max": EXPECTED_PROMPT_TOKEN_MAX,
            "at_input_limit": 0,
        }
        or prompt_preflight["max"] > args.max_input_tokens
    ):
        raise TheLookV2MatchingError(
            "v2 prompt token preflight differs from the frozen input limit"
        )

    gpu = torch.cuda.get_device_properties(0)
    gpu_uuid = str(gpu.uuid)
    gpu_uuid = gpu_uuid if gpu_uuid.startswith("GPU-") else f"GPU-{gpu_uuid}"
    if gpu_uuid != args.expected_gpu_uuid:
        raise TheLookV2MatchingError("CUDA UUID differs from the explicit GPU guard")
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()

    output_dir.mkdir(parents=True)
    raw_path = output_dir / "raw-completions.jsonl"
    records: list[CandidateEvaluationRecord] = []
    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for case, prompt in zip(cases, prompts, strict=True):
        try:
            completion, generated_tokens, elapsed_ms = generate_completion(
                tokenizer, model, prompt, args.max_input_tokens, args.max_new_tokens
            )
        except TheLookV2MatchingError:
            records.append(
                CandidateEvaluationRecord(
                    case.case_id,
                    "answerable",
                    "failed",
                    None,
                    None,
                    "not_run",
                    "not_run",
                    None,
                    False,
                    "generation_error",
                )
            )
            continue
        with raw_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"case_id": case.case_id, "completion": completion},
                    ensure_ascii=False,
                )
                + "\n"
            )
        records.append(
            CandidateEvaluationRecord(
                case.case_id,
                "answerable",
                "generated",
                generated_tokens,
                elapsed_ms,
                "not_run",
                "not_run",
                None,
                False,
                None,
            )
        )

    contract = {
        "matching_contract_version": MATCHING_CONTRACT_VERSION,
        "dataset": frozen_manifest["evaluation_version"],
        "case_count": len(cases),
        "cases_jsonl_sha256": sha256_file(args.cases_jsonl),
        "manifest_sha256": sha256_file(args.manifest),
        "workspace": frozen_manifest["workspace"],
        "prompt_version": PROMPT_VERSION,
        "prompt_bundle_sha256": EXPECTED_PROMPT_BUNDLE_SHA256,
        "prompt_token_preflight": prompt_preflight,
        "model_id": model_manifest["model_id"],
        "model_revision": model_manifest["revision"],
        "base_weight_mode": EXPECTED_BASE_WEIGHT_MODE,
        "decode": {
            "do_sample": False,
            "num_beams": 1,
            "max_input_tokens": args.max_input_tokens,
            "max_new_tokens": args.max_new_tokens,
            "seed": args.seed,
        },
        "gold_sql_read_for_generation": False,
        "database_rows_read_for_generation": False,
    }
    report = build_safe_report(
        report_metadata={
            "report_schema_version": "thelook-v2-generation-safe-report-v1",
            "experiment_type": "thelook_v2_cross_schema_matching_generation",
            "run_label": args.run_label,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
            "comparison_contract": contract,
            "model": {
                "id": model_manifest["model_id"],
                "revision": model_manifest["revision"],
                "download_manifest_sha256": sha256_file(
                    args.model_dir / "download_manifest.json"
                ),
                "base_weight_mode": EXPECTED_BASE_WEIGHT_MODE,
                "adapter": adapter,
            },
            "prompt_token_lengths": prompt_preflight,
            "gpu": {
                "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
                "physical_nvidia_smi_device": args.physical_nvidia_smi_device,
                "name": gpu.name,
                "uuid": gpu_uuid,
                "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
            },
            "raw_artifacts": {
                "raw_completions_sha256": sha256_file(raw_path)
                if raw_path.exists()
                else None,
                "raw_completions_outside_repository": True,
            },
            "boundaries": {
                "gold_sql_read_for_generation": False,
                "database_rows_read_for_generation": False,
                "production_default_unchanged": True,
                "raw_completion_in_repository": False,
            },
        },
        records=records,
    )
    (output_dir / "safe-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"run_label": args.run_label, "summary": report["summary"]},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TheLookV2MatchingError, ValueError) as exc:
        print(f"TheLook v2 generation input error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
