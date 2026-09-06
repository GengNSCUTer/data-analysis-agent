#!/usr/bin/env python3
"""Evaluate matching Base/Adapter Olist Medium v1 candidate SQL generation.

Generation consumes only the physically isolated in-domain test runtime prompts
and their server-owned QueryPlan/ResultContract metadata. Gold SQL is not read
or exposed by this command. Generated SQL is retained only in the external run
directory; the persisted safe report contains aggregate-safe fields only.
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
import uuid

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from data_analysis_agent.candidate_sql_generator import CandidateSqlGenerationError, unwrap_sql_completion
from data_analysis_agent.external_artifacts import ensure_path_outside_repository
from data_analysis_agent.olist_candidate_sql_evaluation import CandidateEvaluationRecord, build_safe_report
from data_analysis_agent.post_training_comparison import sha256_file
from data_analysis_agent.postgres_runner import PostgresConnectionSettings, SecurePostgresRunner
from data_analysis_agent.result_validator import ResultValidationError, ResultValidator
from data_analysis_agent.sql_policy import PolicyViolation
from data_analysis_agent.sql_repair import SafeSqlExecutionError
from vanna.capabilities.sql_runner import RunSqlToolArgs
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory import DemoAgentMemory

EXPECTED_MODEL_ID = "Qwen/Qwen2.5-Coder-1.5B"
EXPECTED_PROMPT_VERSION = "olist-candidate-sql-v1"
EXPECTED_SPLIT = "in_domain_test"


class MediumEvaluationError(ValueError):
    """The frozen Olist Medium v1 evaluation contract was violated."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-jsonl", type=Path, required=True)
    parser.add_argument("--runtime-candidates", type=Path, required=True)
    parser.add_argument("--split-audit", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--run-label", choices=("base", "adapter"), required=True)
    parser.add_argument("--adapter-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-input-tokens", type=int, default=3072)
    parser.add_argument("--max-new-tokens", type=int, default=768)
    parser.add_argument("--seed", type=int, default=20260904)
    parser.add_argument("--physical-nvidia-smi-device", type=int, required=True)
    parser.add_argument("--expected-gpu-uuid", required=True)
    return parser.parse_args()


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, json.JSONDecodeError) as exc:
        raise MediumEvaluationError(f"{label} is unavailable or invalid") from exc
    if not rows:
        raise MediumEvaluationError(f"{label} is empty")
    if not all(isinstance(row, dict) for row in rows):
        raise MediumEvaluationError(f"{label} must contain objects")
    return rows


def load_test_contract(test_jsonl: Path, runtime_candidates: Path, split_audit: Path) -> list[dict[str, Any]]:
    """Bind final-test identities and runtime contexts without reading Gold SQL."""
    audit = json.loads(split_audit.read_text(encoding="utf-8"))
    if audit.get("checks", {}).get("status") != "pass":
        raise MediumEvaluationError("split audit did not pass")
    if audit.get("checks", {}).get("in_domain_test_forbidden_for_training") is not True:
        raise MediumEvaluationError("split audit does not isolate final test")
    outputs = audit.get("outputs", {})
    if Path(str(outputs.get("in_domain_test_jsonl", ""))).resolve() != test_jsonl.resolve():
        raise MediumEvaluationError("test JSONL does not match split audit")
    metadata = audit.get("splits", {}).get(EXPECTED_SPLIT, {})
    if metadata.get("rows") != 240 or metadata.get("sha256") != sha256_file(test_jsonl):
        raise MediumEvaluationError("test rows or hash differ from the frozen audit")

    # This projection intentionally does not access candidate_sql/training_text.
    test_ids: set[str] = set()
    for row in _read_jsonl(test_jsonl, "test JSONL"):
        if row.get("split", {}).get("name") != EXPECTED_SPLIT:
            raise MediumEvaluationError("test JSONL contains a non-test row")
        seed_id = row.get("seed_id")
        if not isinstance(seed_id, str) or not seed_id or seed_id in test_ids:
            raise MediumEvaluationError("test JSONL has invalid seed IDs")
        test_ids.add(seed_id)

    selected: list[dict[str, Any]] = []
    for row in _read_jsonl(runtime_candidates, "runtime candidates"):
        if row.get("split") != EXPECTED_SPLIT:
            continue
        seed_id = row.get("seed_id")
        required = {"seed_id", "prompt", "prompt_sha256", "query_plan", "result_contract", "route"}
        if not isinstance(seed_id, str) or set(row) < required:
            raise MediumEvaluationError("runtime test candidate has missing required fields")
        if seed_id not in test_ids:
            raise MediumEvaluationError("runtime candidate is not in final test")
        prompt = row["prompt"]
        if not isinstance(prompt, str) or not prompt.endswith("### SQL"):
            raise MediumEvaluationError("runtime candidate has invalid SQL prompt")
        if row["prompt_sha256"] != hashlib.sha256(prompt.encode("utf-8")).hexdigest():
            raise MediumEvaluationError("runtime prompt hash mismatch")
        route = row["route"]
        if not isinstance(route, Mapping) or route.get("state") != "answerable":
            raise MediumEvaluationError("final test must contain answerable database routes")
        if not isinstance(row["query_plan"], Mapping) or not isinstance(row["result_contract"], Mapping):
            raise MediumEvaluationError("runtime candidate lacks server-owned metadata")
        selected.append(row)
    if len(selected) != len(test_ids) or {row["seed_id"] for row in selected} != test_ids:
        raise MediumEvaluationError("runtime/test seed identities differ")
    return sorted(selected, key=lambda row: str(row["seed_id"]))


def load_model(model_dir: Path, run_label: str, adapter_dir: Path | None) -> tuple[Any, Any, dict[str, Any], Mapping[str, Any]]:
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    manifest = json.loads((model_dir / "download_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("model_id") != EXPECTED_MODEL_ID:
        raise MediumEvaluationError("unexpected base model")
    tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"
    model = AutoModelForCausalLM.from_pretrained(model_dir, local_files_only=True, torch_dtype=torch.bfloat16, device_map={"": 0})
    adapter: dict[str, Any] = {"enabled": False}
    if run_label == "adapter":
        if adapter_dir is None:
            raise MediumEvaluationError("adapter run requires adapter directory")
        model_path = adapter_dir / "adapter_model.safetensors"
        config_path = adapter_dir / "adapter_config.json"
        if not model_path.is_file() or not config_path.is_file():
            raise MediumEvaluationError("adapter artifacts are incomplete")
        model = PeftModel.from_pretrained(model, adapter_dir, is_trainable=False)
        adapter = {"enabled": True, "adapter_model_sha256": sha256_file(model_path), "adapter_config_sha256": sha256_file(config_path), "adapter_model_bytes": model_path.stat().st_size}
    elif adapter_dir is not None:
        raise MediumEvaluationError("base run cannot receive an adapter")
    model.eval()
    return tokenizer, model, adapter, manifest


def generate(tokenizer: Any, model: Any, prompt: str, max_input: int, max_new: int) -> tuple[str, int, int]:
    import torch
    encoded = tokenizer(prompt, add_special_tokens=False, return_tensors="pt")
    if encoded["input_ids"].shape[-1] > max_input:
        raise MediumEvaluationError("test prompt exceeds frozen token limit")
    started = perf_counter()
    with torch.inference_mode():
        output = model.generate(**{key: value.to("cuda:0") for key, value in encoded.items()}, do_sample=False, num_beams=1, max_new_tokens=max_new, pad_token_id=tokenizer.pad_token_id, eos_token_id=tokenizer.eos_token_id, use_cache=True)
    tokens = output[0, encoded["input_ids"].shape[-1]:]
    completion = tokenizer.decode(tokens, skip_special_tokens=True).strip()
    if not completion:
        raise CandidateSqlGenerationError("empty completion")
    return unwrap_sql_completion(completion), int(tokens.shape[-1]), round((perf_counter() - started) * 1000)


async def execute(runner: SecurePostgresRunner, sql: str, metadata: Mapping[str, Any], source_id: str, run_label: str) -> tuple[str, str, str | None, bool, str | None]:
    context = ToolContext(user=User(id="olist-medium-evaluator", group_memberships=["analyst"]), conversation_id=f"olist-medium-{run_label}-{source_id}-{uuid.uuid4().hex}", request_id=f"olist-medium-{run_label}-{source_id}-{uuid.uuid4().hex}", agent_memory=DemoAgentMemory(), metadata={"question": "offline final test", "query_plan": dict(metadata["query_plan"]), **dict(metadata["result_contract"])})
    try:
        await runner.run_sql(RunSqlToolArgs(sql=sql), context)
    except PolicyViolation:
        return "rejected", "not_run", None, False, "policy_rejected"
    except ResultValidationError as exc:
        return "accepted", "executed", exc.validation.state, False, "result_contract_rejected"
    except SafeSqlExecutionError:
        return "accepted", "error", None, False, "postgres_execution_error"
    except Exception:
        return "accepted", "error", None, False, "unexpected_execution_error"
    valid = context.metadata.get("result_contract_satisfied") is True
    return "accepted", "executed", "valid" if valid else None, valid, None if valid else "missing_valid_contract_state"


def main() -> int:
    args = parse_args()
    import numpy as np
    import torch
    from transformers import set_seed

    os.environ.setdefault("NCCL_P2P_DISABLE", "1")
    os.environ.setdefault("NCCL_IB_DISABLE", "1")
    if not torch.cuda.is_available():
        raise MediumEvaluationError("CUDA is required")
    if args.max_input_tokens != 3072 or args.max_new_tokens <= 0:
        raise MediumEvaluationError("generation token limits differ from the frozen contract")
    for path, label in ((args.test_jsonl, "test JSONL"), (args.runtime_candidates, "runtime candidates"), (args.split_audit, "split audit"), (args.model_dir, "model")):
        ensure_path_outside_repository(path, ROOT)
    output_dir = ensure_path_outside_repository(args.output_dir, ROOT)
    if output_dir.exists():
        raise MediumEvaluationError("output directory must be new")
    adapter_dir = ensure_path_outside_repository(args.adapter_dir, ROOT) if args.adapter_dir else None
    rows = load_test_contract(args.test_jsonl, args.runtime_candidates, args.split_audit)
    random.seed(args.seed); np.random.seed(args.seed); set_seed(args.seed)
    tokenizer, model, adapter, model_manifest = load_model(args.model_dir, args.run_label, adapter_dir)
    gpu = torch.cuda.get_device_properties(0)
    gpu_uuid = str(gpu.uuid); gpu_uuid = gpu_uuid if gpu_uuid.startswith("GPU-") else "GPU-" + gpu_uuid
    if gpu_uuid != args.expected_gpu_uuid:
        raise MediumEvaluationError("CUDA UUID differs from the frozen guard")
    torch.cuda.empty_cache(); torch.cuda.reset_peak_memory_stats()
    output_dir.mkdir(parents=True)
    raw_path = output_dir / "raw-candidates.jsonl"
    settings = PostgresConnectionSettings.from_environment()
    runner = SecurePostgresRunner(settings=settings, result_validator=ResultValidator(settings.max_rows), model_name=f"post-training/{EXPECTED_MODEL_ID}:{args.run_label}")
    records: list[CandidateEvaluationRecord] = []
    started = datetime.now(timezone.utc)
    for row in rows:
        source_id = str(row["seed_id"])
        try:
            sql, tokens, elapsed = generate(tokenizer, model, row["prompt"], args.max_input_tokens, args.max_new_tokens)
        except (CandidateSqlGenerationError, MediumEvaluationError):
            records.append(CandidateEvaluationRecord(source_id, "answerable", "failed", None, None, "not_run", "not_run", None, False, "generation_error")); continue
        with raw_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"source_id": source_id, "candidate_sql": sql}, ensure_ascii=False) + "\n")
        policy, execution, validation, valid, failure = asyncio.run(execute(runner, sql, row, source_id, args.run_label))
        records.append(CandidateEvaluationRecord(source_id, "answerable", "generated", tokens, elapsed, policy, execution, validation, valid, failure))
    contract = {"dataset": "olist_medium_v1_in_domain_test", "test_jsonl_sha256": sha256_file(args.test_jsonl), "runtime_candidates_sha256": sha256_file(args.runtime_candidates), "split_audit_sha256": sha256_file(args.split_audit), "model_id": model_manifest["model_id"], "model_revision": model_manifest["revision"], "base_weight_mode": "bf16_lora", "prompt_version": EXPECTED_PROMPT_VERSION, "decode": {"do_sample": False, "num_beams": 1, "max_input_tokens": args.max_input_tokens, "max_new_tokens": args.max_new_tokens, "seed": args.seed}, "gold_sql_read_for_generation": False}
    report = build_safe_report(report_metadata={"experiment_type": "olist_medium_matching_base_adapter_evaluation", "run_label": args.run_label, "started_at": started.replace(microsecond=0).isoformat(), "finished_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "comparison_contract": contract, "adapter": adapter, "gpu": {"cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"), "physical_nvidia_smi_device": args.physical_nvidia_smi_device, "name": gpu.name, "uuid": gpu_uuid, "peak_allocated_bytes": torch.cuda.max_memory_allocated(), "peak_reserved_bytes": torch.cuda.max_memory_reserved()}, "raw_artifacts": {"raw_candidates_sha256": sha256_file(raw_path) if raw_path.exists() else None, "raw_candidates_outside_repository": True}, "boundaries": {"production_default_unchanged": True, "gold_sql_read_for_generation": False, "raw_candidate_sql_in_repository": False, "raw_result_rows_in_repository": False}}, records=records)
    (output_dir / "safe-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"run_label": args.run_label, "summary": report["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MediumEvaluationError, ValueError) as exc:
        print(f"olist medium evaluation input error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
