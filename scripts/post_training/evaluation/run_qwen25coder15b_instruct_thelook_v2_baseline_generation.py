#!/usr/bin/env python3
"""Generate a protected TheLook v2 SQL baseline with Qwen2.5-Coder Instruct.

This is deliberately a *single-model baseline*, not a Base/Adapter matching
pair.  It keeps the frozen server prompt content, cases, decode parameters and
post-evaluation chain fixed, while using Qwen2.5-Coder-Instruct's official chat
template.  Generation can read only the narrow final-test projection and never
opens PostgreSQL or accesses Gold SQL.
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
from typing import Any, Mapping, Sequence

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


MODEL_ID = "Qwen/Qwen2.5-Coder-1.5B-Instruct"
MODEL_REVISION = "2e1fd397ee46e1388853d2af2c993145b0f1098a"
RUNNER_VERSION = "qwen25coder15b-instruct-thelook-v2-baseline-generation-v1"
REPORT_SCHEMA_VERSION = "qwen25coder15b-instruct-thelook-v2-generation-safe-report-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-jsonl", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
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
            "Qwen2.5-Coder-Instruct download manifest is unavailable"
        ) from exc
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("model_id") != MODEL_ID
        or manifest.get("revision") != MODEL_REVISION
    ):
        raise TheLookV2MatchingError(
            "model directory is not the frozen Qwen2.5-Coder-1.5B-Instruct revision"
        )
    return manifest


def _messages_for_server_prompt(prompt: str) -> list[dict[str, str]]:
    """Put the unchanged server prompt in exactly one official user turn.

    No synthetic schema-only prompt, demonstration SQL, Gold SQL, database row,
    or separate planning turn is added.  Qwen's tokenizer may add its own
    documented default system message when rendering this one-user conversation;
    the rendered chat text and template hash are bound in the safe report.
    """

    if not isinstance(prompt, str) or not prompt.strip():
        raise TheLookV2MatchingError("server prompt must be a non-empty string")
    return [{"role": "user", "content": prompt}]


def _chat_text(tokenizer: Any, prompt: str) -> str:
    rendered = tokenizer.apply_chat_template(
        _messages_for_server_prompt(prompt),
        tokenize=False,
        add_generation_prompt=True,
    )
    if not isinstance(rendered, str) or not rendered:
        raise TheLookV2MatchingError("official Qwen chat template rendered no text")
    return rendered


def _chat_inputs(tokenizer: Any, prompt: str) -> Any:
    return tokenizer.apply_chat_template(
        _messages_for_server_prompt(prompt),
        tokenize=True,
        add_generation_prompt=True,
        return_dict=True,
        return_tensors="pt",
    )


def _sha256_texts(texts: Sequence[str]) -> str:
    if not texts or any(not isinstance(text, str) or not text for text in texts):
        raise TheLookV2MatchingError("chat prompt bundle must contain non-empty strings")
    return hashlib.sha256("\n".join(texts).encode("utf-8")).hexdigest()


def _template_sha256(tokenizer: Any) -> str:
    template = getattr(tokenizer, "chat_template", None)
    if not isinstance(template, str) or not template:
        raise TheLookV2MatchingError("Qwen tokenizer lacks an official chat template")
    return hashlib.sha256(template.encode("utf-8")).hexdigest()


def load_model(model_dir: Path) -> tuple[Any, Any, Mapping[str, Any]]:
    import torch
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
    model.eval()
    return tokenizer, model, manifest


def generate_completion(
    tokenizer: Any, model: Any, prompt: str, *, max_input: int, max_new: int
) -> tuple[str, int, int]:
    """Greedily generate raw text; shared evaluator owns SQL cleanup later."""

    import torch

    encoded = _chat_inputs(tokenizer, prompt)
    input_length = int(encoded["input_ids"].shape[-1])
    if input_length > max_input:
        raise TheLookV2MatchingError(
            f"Instruct chat prompt has {input_length} tokens, exceeding {max_input}"
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
    completion_ids = output[0, input_length:]
    completion = tokenizer.decode(completion_ids, skip_special_tokens=True).strip()
    if not completion:
        raise TheLookV2MatchingError("Qwen2.5-Coder-Instruct generated an empty completion")
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
        raise TheLookV2MatchingError(
            "generation parameters differ from the frozen TheLook v2 baseline contract"
        )
    if not torch.cuda.is_available():
        raise TheLookV2MatchingError("CUDA is required for Instruct baseline generation")
    for path in (args.cases_jsonl, args.manifest, args.model_dir):
        ensure_path_outside_repository(path, ROOT)
    output_dir = ensure_path_outside_repository(args.output_dir, ROOT)
    if output_dir.exists():
        raise TheLookV2MatchingError("generation output directory must be new")

    cases, frozen_manifest = read_generation_cases(args.cases_jsonl, args.manifest)
    server_prompts = [render_generation_prompt(case) for case in cases]
    random.seed(args.seed)
    np.random.seed(args.seed)
    set_seed(args.seed)
    tokenizer, model, model_manifest = load_model(args.model_dir)
    chat_prompts = [_chat_text(tokenizer, prompt) for prompt in server_prompts]
    prompt_token_lengths = [
        int(_chat_inputs(tokenizer, prompt)["input_ids"].shape[-1])
        for prompt in server_prompts
    ]
    prompt_preflight = {
        "count": len(prompt_token_lengths),
        "min": min(prompt_token_lengths),
        "max": max(prompt_token_lengths),
        "at_input_limit": sum(length == args.max_input_tokens for length in prompt_token_lengths),
    }
    if (
        len(cases) != EXPECTED_CASES
        or prompt_bundle_sha256(server_prompts)
        != "48bdc6763f8a7a5ead870d1ae9b25ba4dede6c99cf39fea0c5294cd4223ab123"
        or prompt_preflight["max"] > args.max_input_tokens
    ):
        raise TheLookV2MatchingError("protected prompt or capacity preflight drifted")

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
    for case, prompt in zip(cases, server_prompts, strict=True):
        try:
            completion, generated_tokens, elapsed_ms = generate_completion(
                tokenizer,
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

    report = build_safe_report(
        report_metadata={
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "runner_version": RUNNER_VERSION,
            "run_label": "instruct_baseline",
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
            "evaluation": {
                "dataset": frozen_manifest["evaluation_version"],
                "case_count": len(cases),
                "cases_jsonl_sha256": sha256_file(args.cases_jsonl),
                "manifest_sha256": sha256_file(args.manifest),
                "server_prompt_bundle_sha256": prompt_bundle_sha256(server_prompts),
                "qwen25coder_instruct_chat_prompt_bundle_sha256": _sha256_texts(
                    chat_prompts
                ),
                "qwen25coder_instruct_chat_template_sha256": _template_sha256(
                    tokenizer
                ),
                "official_chat_template": True,
            },
            "model": {
                "id": model_manifest["model_id"],
                "revision": model_manifest["revision"],
                "download_manifest_sha256": sha256_file(
                    args.model_dir / "download_manifest.json"
                ),
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
    print(json.dumps({"summary": report["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TheLookV2MatchingError, ValueError) as exc:
        print(f"Qwen2.5-Coder-Instruct TheLook baseline error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
