#!/usr/bin/env python3
"""Generate the Qwen3.5-4B LoRA Adapter side of protected TheLook v2.

Only generation-safe TheLook fields are read.  The runner loads the frozen
Qwen3.5-4B Base plus its final Olist LoRA Adapter and writes raw completions
and hash-only safe evidence outside Git.  It does not read Gold SQL, execute
PostgreSQL, inspect Base completions, or alter the product runtime.
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
for directory in (ROOT, SOURCE_ROOT):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from data_analysis_agent.external_artifacts import ensure_path_outside_repository  # noqa: E402
from data_analysis_agent.olist_candidate_sql_evaluation import (  # noqa: E402
    CandidateEvaluationRecord,
    build_safe_report,
)
from data_analysis_agent.qwen35_4b_thelook_v2_matching import (  # noqa: E402
    DECODE,
    MODEL_ID,
    MODEL_REVISION,
    PROMPT_TOKEN_PREFLIGHT,
    WEIGHT_DTYPE,
    build_comparison_contract,
)
from data_analysis_agent.thelook_v2_matching import (  # noqa: E402
    EXPECTED_CASES,
    TheLookV2MatchingError,
    prompt_bundle_sha256,
    read_generation_cases,
    render_generation_prompt,
    sha256_file,
)
from scripts.post_training.evaluation.run_qwen35_thelook_v2_baseline_generation import (  # noqa: E402
    _build_inputs,
    _read_model_manifest,
)


RUNNER_VERSION = "qwen35-4b-thelook-v2-adapter-generation-v1"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-jsonl", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--adapter-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--physical-nvidia-smi-device", type=int, required=True)
    parser.add_argument("--expected-gpu-uuid", required=True)
    return parser.parse_args(argv)


def load_adapter(model_dir: Path, adapter_dir: Path) -> tuple[Any, Any, dict[str, Any]]:
    """Load exactly the frozen 4B Base revision plus a complete LoRA adapter."""

    import torch
    from peft import PeftModel
    from transformers import AutoModelForImageTextToText, AutoProcessor

    manifest = _read_model_manifest(model_dir)
    if (
        manifest.get("model_id") != MODEL_ID
        or manifest.get("revision") != MODEL_REVISION
    ):
        raise TheLookV2MatchingError(
            "model identity differs from frozen Qwen3.5-4B Base"
        )
    model_path = adapter_dir / "adapter_model.safetensors"
    config_path = adapter_dir / "adapter_config.json"
    if not model_path.is_file() or not config_path.is_file():
        raise TheLookV2MatchingError("4B Adapter artifact is incomplete")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TheLookV2MatchingError(
            "4B Adapter config is unavailable or invalid"
        ) from exc
    if not isinstance(config, Mapping) or config.get("task_type") != "CAUSAL_LM":
        raise TheLookV2MatchingError(
            "4B Adapter task type is incompatible with SQL generation"
        )
    base_path = config.get("base_model_name_or_path")
    if (
        isinstance(base_path, str)
        and base_path
        and Path(base_path).resolve() != model_dir.resolve()
    ):
        raise TheLookV2MatchingError(
            "4B Adapter base model path differs from the frozen model directory"
        )
    processor = AutoProcessor.from_pretrained(model_dir, local_files_only=True)
    base = AutoModelForImageTextToText.from_pretrained(
        model_dir, local_files_only=True, dtype=torch.bfloat16
    ).to("cuda:0")
    model = PeftModel.from_pretrained(base, adapter_dir, is_trainable=False)
    model.eval()
    return (
        processor,
        model,
        {
            "enabled": True,
            "adapter_model_sha256": sha256_file(model_path),
            "adapter_config_sha256": sha256_file(config_path),
            "adapter_model_bytes": model_path.stat().st_size,
        },
    )


def generate_completion(
    processor: Any, model: Any, prompt: str
) -> tuple[str, int, int]:
    """Generate one deterministic completion; normalization is deferred."""

    import torch

    encoded = _build_inputs(processor, prompt)
    input_length = int(encoded["input_ids"].shape[-1])
    if input_length > DECODE["max_input_tokens"]:
        raise TheLookV2MatchingError("Qwen3.5-4B chat input exceeds frozen limit")
    started = perf_counter()
    with torch.inference_mode():
        output = model.generate(
            **{key: value.to("cuda:0") for key, value in encoded.items()},
            do_sample=False,
            num_beams=1,
            max_new_tokens=DECODE["max_new_tokens"],
            pad_token_id=processor.tokenizer.pad_token_id,
            eos_token_id=processor.tokenizer.eos_token_id,
            use_cache=True,
        )
    completion_ids = output[:, input_length:]
    completion = processor.batch_decode(completion_ids, skip_special_tokens=True)[
        0
    ].strip()
    if not completion:
        raise TheLookV2MatchingError("Qwen3.5-4B Adapter produced an empty completion")
    return (
        completion,
        int(completion_ids.shape[-1]),
        round((perf_counter() - started) * 1000),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    import numpy as np
    import torch
    from transformers import set_seed

    if not torch.cuda.is_available():
        raise TheLookV2MatchingError("CUDA is required for Adapter generation")
    for path in (args.cases_jsonl, args.manifest, args.model_dir, args.adapter_dir):
        ensure_path_outside_repository(path, ROOT)
    output_dir = ensure_path_outside_repository(args.output_dir, ROOT)
    if output_dir.exists():
        raise TheLookV2MatchingError("4B Adapter output directory must be new")

    cases, manifest = read_generation_cases(args.cases_jsonl, args.manifest)
    prompts = [render_generation_prompt(case) for case in cases]
    random.seed(DECODE["seed"])
    np.random.seed(DECODE["seed"])
    set_seed(DECODE["seed"])
    processor, model, adapter = load_adapter(args.model_dir, args.adapter_dir)
    lengths = [
        int(_build_inputs(processor, prompt)["input_ids"].shape[-1])
        for prompt in prompts
    ]
    preflight = {
        "count": len(lengths),
        "min": min(lengths),
        "max": max(lengths),
        "at_input_limit": sum(
            length == DECODE["max_input_tokens"] for length in lengths
        ),
    }
    if (
        len(cases) != EXPECTED_CASES
        or prompt_bundle_sha256(prompts)
        != "48bdc6763f8a7a5ead870d1ae9b25ba4dede6c99cf39fea0c5294cd4223ab123"
        or preflight != PROMPT_TOKEN_PREFLIGHT
    ):
        raise TheLookV2MatchingError(
            "protected prompt preflight differs from frozen 4B Base"
        )
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
        completion, tokens, elapsed_ms = generate_completion(processor, model, prompt)
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
                tokens,
                elapsed_ms,
                "not_run",
                "not_run",
                None,
                False,
                None,
            )
        )

    contract = build_comparison_contract(
        cases_sha256=sha256_file(args.cases_jsonl),
        manifest_sha256=sha256_file(args.manifest),
        workspace=manifest["workspace"],
    )
    report = build_safe_report(
        report_metadata={
            "report_schema_version": "qwen35-4b-thelook-v2-adapter-generation-safe-report-v1",
            "runner_version": RUNNER_VERSION,
            "run_label": "adapter",
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
            "comparison_contract": contract,
            "model": {
                "id": MODEL_ID,
                "revision": MODEL_REVISION,
                "weight_dtype": WEIGHT_DTYPE,
                "download_manifest_sha256": sha256_file(
                    args.model_dir / "download_manifest.json"
                ),
                "adapter": adapter,
            },
            "raw_artifacts": {
                "raw_completions_sha256": sha256_file(raw_path),
                "raw_completions_outside_repository": True,
            },
            "boundaries": {
                "gold_sql_read_for_generation": False,
                "database_rows_read_for_generation": False,
                "production_default_unchanged": True,
                "raw_completion_in_repository": False,
            },
            "gpu": {
                "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
                "physical_nvidia_smi_device": args.physical_nvidia_smi_device,
                "name": gpu.name,
                "uuid": gpu_uuid,
                "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
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
            {"run_label": "adapter", "summary": report["summary"]}, ensure_ascii=False
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TheLookV2MatchingError, ValueError) as exc:
        print(f"Qwen3.5-4B TheLook adapter error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
