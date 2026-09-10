#!/usr/bin/env python3
"""Generate Qwen3.5 text-only baseline completions for protected TheLook v2.

This intentionally measures a *new base model*, not the frozen Qwen2.5
Base/Adapter matching pair.  It reads only the generation-safe projection
(question, QuerySpec, and ResultContract), never Gold SQL or database rows.
The corresponding post-evaluator is the only later stage permitted to read
Gold SQL.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
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
    EXPECTED_CASES,
    EXPECTED_MAX_INPUT_TOKENS,
    EXPECTED_MAX_NEW_TOKENS,
    EXPECTED_SEED,
    TheLookV2MatchingError,
    prompt_bundle_sha256,
    read_generation_cases,
    render_generation_prompt,
    sha256_file,
)


RUNNER_VERSION = "qwen35-thelook-v2-baseline-generation-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-jsonl", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--max-input-tokens", type=int, default=EXPECTED_MAX_INPUT_TOKENS)
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
        raise TheLookV2MatchingError("Qwen3.5 model manifest is unavailable") from exc
    if (
        not isinstance(manifest, Mapping)
        or not isinstance(manifest.get("model_id"), str)
        or not isinstance(manifest.get("revision"), str)
        or not str(manifest["model_id"]).startswith("Qwen/Qwen3.5-")
    ):
        raise TheLookV2MatchingError("model directory is not a frozen Qwen3.5 release")
    return manifest


def _messages_for_sql_prompt(prompt: str) -> list[dict[str, list[dict[str, str]]]]:
    """Place the unchanged server prompt into Qwen3.5's official chat template."""

    return [{"role": "user", "content": [{"type": "text", "text": prompt}]}]


def _build_inputs(processor: Any, prompt: str) -> Any:
    return processor.apply_chat_template(
        _messages_for_sql_prompt(prompt),
        tokenize=True,
        add_generation_prompt=True,
        return_dict=True,
        return_tensors="pt",
        # The task contract requests SQL only; hidden reasoning would make
        # output shape and token budget incomparable with the existing greedy
        # candidate generator.
        enable_thinking=False,
    )


def load_model(model_dir: Path) -> tuple[Any, Any, Mapping[str, Any]]:
    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor

    manifest = _read_model_manifest(model_dir)
    processor = AutoProcessor.from_pretrained(model_dir, local_files_only=True)
    model = AutoModelForImageTextToText.from_pretrained(
        model_dir,
        local_files_only=True,
        dtype=torch.bfloat16,
    ).to("cuda:0")
    model.eval()
    return processor, model, manifest


def generate_completion(
    processor: Any, model: Any, prompt: str, *, max_input: int, max_new: int
) -> tuple[str, int, int]:
    """Greedily generate raw text; SQL cleanup remains a later audit stage."""

    import torch

    encoded = _build_inputs(processor, prompt)
    input_length = int(encoded["input_ids"].shape[-1])
    if input_length > max_input:
        raise TheLookV2MatchingError(
            f"Qwen3.5 chat prompt has {input_length} tokens, exceeding {max_input}"
        )
    started = perf_counter()
    with torch.inference_mode():
        output = model.generate(
            **{key: value.to("cuda:0") for key, value in encoded.items()},
            do_sample=False,
            num_beams=1,
            max_new_tokens=max_new,
            pad_token_id=processor.tokenizer.pad_token_id,
            eos_token_id=processor.tokenizer.eos_token_id,
            use_cache=True,
        )
    completion_ids = output[:, input_length:]
    completion = processor.batch_decode(completion_ids, skip_special_tokens=True)[0].strip()
    if not completion:
        raise TheLookV2MatchingError("Qwen3.5 produced an empty completion")
    return completion, int(completion_ids.shape[-1]), round(
        (perf_counter() - started) * 1000
    )


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
        raise TheLookV2MatchingError("generation parameters differ from baseline contract")
    if not torch.cuda.is_available():
        raise TheLookV2MatchingError("CUDA is required for Qwen3.5 generation")
    for path in (args.cases_jsonl, args.manifest, args.model_dir):
        ensure_path_outside_repository(path, ROOT)
    output_dir = ensure_path_outside_repository(args.output_dir, ROOT)
    if output_dir.exists():
        raise TheLookV2MatchingError("baseline output directory must be new")

    cases, frozen_manifest = read_generation_cases(args.cases_jsonl, args.manifest)
    prompts = [render_generation_prompt(case) for case in cases]
    random.seed(args.seed)
    np.random.seed(args.seed)
    set_seed(args.seed)
    processor, model, model_manifest = load_model(args.model_dir)
    prompt_token_lengths = [
        int(_build_inputs(processor, prompt)["input_ids"].shape[-1])
        for prompt in prompts
    ]
    if len(cases) != EXPECTED_CASES or max(prompt_token_lengths) > args.max_input_tokens:
        raise TheLookV2MatchingError("protected case count or prompt capacity drifted")

    gpu = torch.cuda.get_device_properties(0)
    gpu_uuid = str(gpu.uuid)
    gpu_uuid = gpu_uuid if gpu_uuid.startswith("GPU-") else f"GPU-{gpu_uuid}"
    if gpu_uuid != args.expected_gpu_uuid:
        raise TheLookV2MatchingError("CUDA UUID differs from explicit GPU guard")
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    output_dir.mkdir(parents=True)
    raw_path = output_dir / "raw-completions.jsonl"
    records: list[CandidateEvaluationRecord] = []
    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for case, prompt in zip(cases, prompts, strict=True):
        try:
            completion, generated_tokens, elapsed_ms = generate_completion(
                processor,
                model,
                prompt,
                max_input=args.max_input_tokens,
                max_new=args.max_new_tokens,
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
                json.dumps({"case_id": case.case_id, "completion": completion}, ensure_ascii=False)
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

    prompt_preflight = {
        "count": len(prompt_token_lengths),
        "min": min(prompt_token_lengths),
        "max": max(prompt_token_lengths),
        "at_input_limit": sum(length == args.max_input_tokens for length in prompt_token_lengths),
    }
    report = build_safe_report(
        report_metadata={
            "report_schema_version": "qwen35-thelook-v2-generation-safe-report-v1",
            "runner_version": RUNNER_VERSION,
            "run_label": args.run_label,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "evaluation": {
                "dataset": frozen_manifest["evaluation_version"],
                "case_count": len(cases),
                "cases_jsonl_sha256": sha256_file(args.cases_jsonl),
                "manifest_sha256": sha256_file(args.manifest),
                "server_prompt_bundle_sha256": prompt_bundle_sha256(prompts),
                "qwen35_chat_template": True,
                "thinking_enabled": False,
            },
            "model": {
                "id": model_manifest["model_id"],
                "revision": model_manifest["revision"],
                "download_manifest_sha256": sha256_file(args.model_dir / "download_manifest.json"),
                "weight_dtype": "bf16",
                "adapter": {"enabled": False},
            },
            "decode": {
                "do_sample": False,
                "num_beams": 1,
                "max_input_tokens": args.max_input_tokens,
                "max_new_tokens": args.max_new_tokens,
                "seed": args.seed,
            },
            "prompt_token_preflight": prompt_preflight,
            "gpu": {
                "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
                "physical_nvidia_smi_device": args.physical_nvidia_smi_device,
                "name": gpu.name,
                "uuid": gpu_uuid,
                "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
            },
            "raw_artifacts": {
                "raw_completions_sha256": sha256_file(raw_path) if raw_path.exists() else None,
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
    print(json.dumps({"run_label": args.run_label, "summary": report["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TheLookV2MatchingError, ValueError) as exc:
        print(f"Qwen3.5 TheLook baseline error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
