#!/usr/bin/env python3
"""Generate a matching Qwen3.5 Base/Adapter SQL evaluation on one frozen suite.

The runner supports the current protected Olist v3.1 and TheLook v3 suites.
It intentionally uses the exact Qwen3.5 non-thinking chat envelope used at
SFT time.  Gold SQL is never read during generation.  Olist performs its
normal governed execution while TheLook keeps Gold/denotation comparison in
the separate post-generation evaluator.
"""

from __future__ import annotations

import argparse
import asyncio
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
SRC = ROOT / "src"
for directory in (ROOT, SRC):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from data_analysis_agent.candidate_sql_generator import CandidateSqlGenerationError, unwrap_sql_completion
from data_analysis_agent.external_artifacts import ensure_path_outside_repository
from data_analysis_agent.olist_candidate_sql_evaluation import CandidateEvaluationRecord, build_safe_report
from data_analysis_agent.postgres_runner import PostgresConnectionSettings, SecurePostgresRunner
from data_analysis_agent.result_validator import ResultValidator


EXPECTED_MODEL_ID = "Qwen/Qwen3.5-2B"
EXPECTED_INPUT_TOKENS = 3072
EXPECTED_NEW_TOKENS = 768


class Qwen35MatchingError(ValueError):
    """A frozen Qwen3.5 matching-evaluation contract was violated."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("olist", "thelook"), required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--run-label", choices=("base", "adapter"), required=True)
    parser.add_argument("--adapter-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-model-id", default=EXPECTED_MODEL_ID)
    parser.add_argument("--max-input-tokens", type=int, default=EXPECTED_INPUT_TOKENS)
    parser.add_argument("--max-new-tokens", type=int, default=EXPECTED_NEW_TOKENS)
    parser.add_argument("--seed", type=int, default=20260916)
    parser.add_argument("--physical-nvidia-smi-device", type=int, required=True)
    parser.add_argument("--expected-gpu-uuid", required=True)
    # Olist-only, deliberately separate from TheLook's frozen manifest.
    parser.add_argument("--olist-test-jsonl", type=Path)
    parser.add_argument("--olist-runtime-candidates", type=Path)
    parser.add_argument("--olist-split-audit", type=Path)
    parser.add_argument("--evaluation-suite")
    # TheLook-only.
    parser.add_argument("--thelook-cases-jsonl", type=Path)
    parser.add_argument("--thelook-manifest", type=Path)
    return parser.parse_args()


def _validate_args(args: argparse.Namespace) -> None:
    if args.max_input_tokens != EXPECTED_INPUT_TOKENS or args.max_new_tokens != EXPECTED_NEW_TOKENS:
        raise Qwen35MatchingError("generation limits differ from the frozen matching contract")
    if args.run_label == "adapter" and args.adapter_dir is None:
        raise Qwen35MatchingError("adapter run requires --adapter-dir")
    if args.run_label == "base" and args.adapter_dir is not None:
        raise Qwen35MatchingError("base run cannot receive an adapter")
    if args.suite == "olist":
        if not all((args.olist_test_jsonl, args.olist_runtime_candidates, args.olist_split_audit, args.evaluation_suite)):
            raise Qwen35MatchingError("Olist run lacks a frozen test/runtime/split contract")
    elif not all((args.thelook_cases_jsonl, args.thelook_manifest)):
        raise Qwen35MatchingError("TheLook run lacks frozen cases or manifest")


def _messages(prompt: str) -> list[dict[str, list[dict[str, str]]]]:
    return [{"role": "user", "content": [{"type": "text", "text": prompt}]}]


def _encode(processor: Any, prompt: str) -> Any:
    return processor.apply_chat_template(
        _messages(prompt),
        tokenize=True,
        add_generation_prompt=True,
        return_dict=True,
        return_tensors="pt",
        enable_thinking=False,
    )


def _load_model(args: argparse.Namespace) -> tuple[Any, Any, Mapping[str, Any], dict[str, Any]]:
    import torch
    from peft import PeftModel
    from transformers import AutoModelForImageTextToText, AutoProcessor

    try:
        manifest = json.loads((args.model_dir / "download_manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Qwen35MatchingError("model manifest is unavailable or invalid") from exc
    if manifest.get("model_id") != args.expected_model_id:
        raise Qwen35MatchingError("base model differs from the frozen Qwen3.5-2B identity")
    processor = AutoProcessor.from_pretrained(args.model_dir, local_files_only=True)
    if processor.tokenizer.pad_token_id is None:
        processor.tokenizer.pad_token = processor.tokenizer.eos_token
    model = AutoModelForImageTextToText.from_pretrained(
        args.model_dir, local_files_only=True, dtype=torch.bfloat16
    ).to("cuda:0")
    adapter: dict[str, Any] = {"enabled": False}
    if args.run_label == "adapter":
        assert args.adapter_dir is not None
        model_file = args.adapter_dir / "adapter_model.safetensors"
        config_file = args.adapter_dir / "adapter_config.json"
        if not model_file.is_file() or not config_file.is_file():
            raise Qwen35MatchingError("adapter artifacts are incomplete")
        try:
            config = json.loads(config_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise Qwen35MatchingError("adapter configuration is unavailable") from exc
        if config.get("task_type") != "CAUSAL_LM":
            raise Qwen35MatchingError("adapter task type is incompatible with causal SQL generation")
        model = PeftModel.from_pretrained(model, args.adapter_dir, is_trainable=False)
        adapter = {
            "enabled": True,
            "adapter_model_sha256": sha256_file(model_file),
            "adapter_config_sha256": sha256_file(config_file),
            "adapter_model_bytes": model_file.stat().st_size,
        }
    model.eval()
    return processor, model, manifest, adapter


def _generate(processor: Any, model: Any, prompt: str, *, max_input: int, max_new: int) -> tuple[str, int, int]:
    import torch

    encoded = _encode(processor, prompt)
    input_length = int(encoded["input_ids"].shape[-1])
    if input_length > max_input:
        raise Qwen35MatchingError(f"Qwen3.5 chat prompt has {input_length} tokens above {max_input}")
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
        raise CandidateSqlGenerationError("Qwen3.5 produced an empty completion")
    return completion, int(completion_ids.shape[-1]), round((perf_counter() - started) * 1000)


def _gpu_guard(expected_uuid: str) -> tuple[Any, str]:
    import torch

    properties = torch.cuda.get_device_properties(0)
    uuid = str(properties.uuid)
    uuid = uuid if uuid.startswith("GPU-") else f"GPU-{uuid}"
    if uuid != expected_uuid:
        raise Qwen35MatchingError("CUDA UUID differs from the explicit physical-GPU guard")
    return properties, uuid


def _run_olist(args: argparse.Namespace, *, processor: Any, model: Any, manifest: Mapping[str, Any], adapter: Mapping[str, Any], gpu: Any, gpu_uuid: str) -> None:
    from scripts.post_training.evaluation.run_olist_medium_matching_evaluation import execute, load_test_contract

    assert args.olist_test_jsonl and args.olist_runtime_candidates and args.olist_split_audit and args.evaluation_suite
    rows = load_test_contract(args.olist_test_jsonl, args.olist_runtime_candidates, args.olist_split_audit)
    prompts = [str(row["prompt"]) for row in rows]
    if max(int(_encode(processor, prompt)["input_ids"].shape[-1]) for prompt in prompts) > args.max_input_tokens:
        raise Qwen35MatchingError("an Olist Qwen3.5 chat prompt exceeds the frozen input limit")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    raw_path = args.output_dir / "raw-candidates.jsonl"
    settings = PostgresConnectionSettings.from_environment()
    runner = SecurePostgresRunner(settings=settings, result_validator=ResultValidator(settings.max_rows), model_name=f"post-training/{manifest['model_id']}:{args.run_label}")
    records: list[CandidateEvaluationRecord] = []
    started = datetime.now(timezone.utc)
    for row in rows:
        source_id = str(row["seed_id"])
        try:
            raw, tokens, elapsed = _generate(processor, model, str(row["prompt"]), max_input=args.max_input_tokens, max_new=args.max_new_tokens)
            sql = unwrap_sql_completion(raw)
        except (CandidateSqlGenerationError, Qwen35MatchingError, ValueError):
            records.append(CandidateEvaluationRecord(source_id, "answerable", "failed", None, None, "not_run", "not_run", None, False, "generation_error"))
            continue
        with raw_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"source_id": source_id, "candidate_sql": sql}, ensure_ascii=False) + "\n")
        policy, execution, validation, valid, failure = asyncio.run(execute(runner, sql, row, source_id, args.run_label))
        records.append(CandidateEvaluationRecord(source_id, "answerable", "generated", tokens, elapsed, policy, execution, validation, valid, failure))
    contract = {
        "dataset": f"{args.evaluation_suite}_in_domain_test",
        "test_case_count": len(rows),
        "test_jsonl_sha256": sha256_file(args.olist_test_jsonl),
        "runtime_candidates_sha256": sha256_file(args.olist_runtime_candidates),
        "split_audit_sha256": sha256_file(args.olist_split_audit),
        "model_id": manifest["model_id"], "model_revision": manifest["revision"],
        "base_weight_mode": "bf16_frozen", "prompt_version": "qwen35-olist-instruct-sft-v1",
        "qwen35_chat_template": True, "enable_thinking": False,
        "decode": {"do_sample": False, "num_beams": 1, "max_input_tokens": args.max_input_tokens, "max_new_tokens": args.max_new_tokens, "seed": args.seed},
        "gold_sql_read_for_generation": False,
    }
    report = build_safe_report(report_metadata={
        "experiment_type": "qwen35_olist_matching_base_adapter_evaluation", "evaluation_suite": args.evaluation_suite,
        "run_label": args.run_label, "started_at": started.replace(microsecond=0).isoformat(),
        "finished_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "comparison_contract": contract,
        "adapter": dict(adapter),
        "gpu": {"cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"), "physical_nvidia_smi_device": args.physical_nvidia_smi_device, "name": gpu.name, "uuid": gpu_uuid},
        "raw_artifacts": {"raw_candidates_sha256": sha256_file(raw_path) if raw_path.exists() else None, "raw_candidates_outside_repository": True},
        "boundaries": {"production_default_unchanged": True, "gold_sql_read_for_generation": False, "raw_candidate_sql_in_repository": False, "raw_result_rows_in_repository": False},
    }, records=records)
    (args.output_dir / "safe-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"run_label": args.run_label, "summary": report["summary"]}, ensure_ascii=False))


def _run_thelook(args: argparse.Namespace, *, processor: Any, model: Any, manifest: Mapping[str, Any], adapter: Mapping[str, Any], gpu: Any, gpu_uuid: str) -> None:
    from scripts.post_training.evaluation.run_thelook_matching_evaluation import _catalog_prompt, _read_cases, _render_prompt

    assert args.thelook_cases_jsonl and args.thelook_manifest
    rows, frozen_manifest = _read_cases(args.thelook_cases_jsonl, args.thelook_manifest)
    catalog_prompt = _catalog_prompt()
    prompts = [_render_prompt(row, catalog_prompt) for row in rows]
    token_lengths = [int(_encode(processor, prompt)["input_ids"].shape[-1]) for prompt in prompts]
    if max(token_lengths) > args.max_input_tokens:
        raise Qwen35MatchingError("a TheLook Qwen3.5 chat prompt exceeds the frozen input limit")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    raw_path = args.output_dir / "raw-candidates.jsonl"
    safe_records: list[dict[str, Any]] = []
    started = datetime.now(timezone.utc)
    for row, prompt in zip(rows, prompts, strict=True):
        try:
            raw, tokens, elapsed = _generate(processor, model, prompt, max_input=args.max_input_tokens, max_new=args.max_new_tokens)
            sql = unwrap_sql_completion(raw)
            with raw_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"case_id": row["case_id"], "candidate_sql": sql}, ensure_ascii=False) + "\n")
            failure = None
        except Exception:
            tokens = elapsed = None
            failure = "generation_error"
        safe_records.append({"case_id": row["case_id"], "generated_tokens": tokens, "generation_elapsed_ms": elapsed, "policy_status": "not_run", "execution_status": "not_run", "result_validation_state": None, "result_contract_satisfied": False, "failure_category": failure})
    contract = {
        "dataset": frozen_manifest["evaluation_version"], "cases_sha256": sha256_file(args.thelook_cases_jsonl),
        "manifest_sha256": sha256_file(args.thelook_manifest), "case_count": len(rows),
        "prompt_format_version": "thelook-candidate-sql-v1", "prompt_sha256": hashlib.sha256("\n".join(prompts).encode()).hexdigest(),
        "qwen35_chat_template": True, "enable_thinking": False,
        "decode": {"do_sample": False, "num_beams": 1, "max_input_tokens": args.max_input_tokens, "max_new_tokens": args.max_new_tokens, "seed": args.seed},
        "gold_sql_read_for_generation": False, "database": "thelook_analytics", "reader_role": "daa_thelook_reader",
    }
    report = {
        "report_schema_version": "qwen35-thelook-matching-v1", "run_label": args.run_label,
        "started_at": started.replace(microsecond=0).isoformat(), "finished_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "model": {"id": manifest["model_id"], "revision": manifest["revision"], "base_weight_mode": "bf16", "adapter": dict(adapter)},
        "comparison_contract": contract,
        "gpu": {"cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"), "physical_nvidia_smi_device": args.physical_nvidia_smi_device, "name": gpu.name, "uuid": gpu_uuid},
        "summary": {"case_count": len(safe_records), "generation_success": sum(record["failure_category"] is None for record in safe_records), "policy_accepted": 0, "postgres_executed": 0, "result_contract_valid": 0},
        "records": safe_records,
        "raw_artifacts": {"raw_candidates_sha256": sha256_file(raw_path) if raw_path.exists() else None, "raw_candidates_outside_repository": True},
        "boundaries": {"gold_sql_read_for_generation": False, "production_default_unchanged": True},
    }
    (args.output_dir / "safe-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))


def main() -> int:
    args = parse_args()
    _validate_args(args)
    import numpy as np
    import torch
    from transformers import set_seed

    if not torch.cuda.is_available():
        raise Qwen35MatchingError("CUDA is required")
    paths = [args.model_dir]
    if args.adapter_dir:
        paths.append(args.adapter_dir)
    if args.suite == "olist":
        paths.extend([args.olist_test_jsonl, args.olist_runtime_candidates, args.olist_split_audit])
    else:
        paths.extend([args.thelook_cases_jsonl, args.thelook_manifest])
    for path in paths:
        assert path is not None
        ensure_path_outside_repository(path, ROOT)
    args.output_dir = ensure_path_outside_repository(args.output_dir, ROOT)
    if args.output_dir.exists():
        raise Qwen35MatchingError("output directory must be new")
    random.seed(args.seed)
    np.random.seed(args.seed)
    set_seed(args.seed)
    processor, model, manifest, adapter = _load_model(args)
    gpu, gpu_uuid = _gpu_guard(args.expected_gpu_uuid)
    if args.suite == "olist":
        _run_olist(args, processor=processor, model=model, manifest=manifest, adapter=adapter, gpu=gpu, gpu_uuid=gpu_uuid)
    else:
        _run_thelook(args, processor=processor, model=model, manifest=manifest, adapter=adapter, gpu=gpu, gpu_uuid=gpu_uuid)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Qwen35MatchingError, ValueError) as exc:
        print(f"Qwen3.5 matching input error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
