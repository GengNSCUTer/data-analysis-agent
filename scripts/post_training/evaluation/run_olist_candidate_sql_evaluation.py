#!/usr/bin/env python3
"""Run one protected Olist Base or Adapter candidate-SQL evaluation.

This script intentionally evaluates only the candidate generator.  It rebuilds
the current server-owned QuestionRouter, Semantic Catalog, QueryPlan and
ResultContract before executing a completion through the production SQL Policy,
readonly PostgreSQL role and ResultValidator.  It never invokes Vanna's online
model, SQL repair, chart generation, or a production-model switch.

Raw questions, candidate SQL, database rows and SQL audit details stay in the
external ``--output-dir``.  The only report intended for comparison is the
redacted ``safe-report.json`` in that same external directory.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import sys
from time import perf_counter
from typing import Any, Mapping, Sequence
import uuid

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import numpy as np
from peft import PeftModel
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, set_seed
import yaml

from data_analysis_agent.candidate_sql_generator import (
    CandidateSqlContext,
    CandidateSqlGenerationError,
    OLIST_CANDIDATE_SQL_PROMPT_VERSION,
    render_candidate_sql_prompt,
    require_database_route,
    unwrap_sql_completion,
)
from data_analysis_agent.external_artifacts import ensure_path_outside_repository
from data_analysis_agent.olist_candidate_sql_evaluation import (
    CandidateEvaluationRecord,
    OlistCandidateEvaluationError,
    build_safe_report,
    validate_manifest_cases,
)
from data_analysis_agent.post_training_comparison import sha256_file
from data_analysis_agent.postgres_runner import PostgresConnectionSettings, SecurePostgresRunner
from data_analysis_agent.query_plan import QueryPlan
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.result_validator import ResultValidationError, ResultValidator
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever, ResultContract
from data_analysis_agent.sql_policy import PolicyViolation
from data_analysis_agent.sql_repair import SafeSqlExecutionError
from data_analysis_agent.working_memory import WorkingMemory
from vanna.capabilities.sql_runner import RunSqlToolArgs
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory import DemoAgentMemory


EXPECTED_MODEL_ID = "Qwen/Qwen2.5-Coder-1.5B"


class EvaluationInputError(ValueError):
    """A frozen transfer-evaluation input contract was violated."""


@dataclass(frozen=True)
class SelectedCase:
    source_id: str
    question: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--run-label", choices=("base", "adapter"), required=True)
    parser.add_argument("--adapter-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-input-tokens", type=int, default=4_096)
    parser.add_argument("--max-new-tokens", type=int, default=256)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--physical-nvidia-smi-device", type=int, required=True)
    parser.add_argument("--expected-gpu-uuid", required=True)
    return parser.parse_args(argv)


def load_yaml_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvaluationInputError(f"{label} does not exist: {path}") from exc
    except yaml.YAMLError as exc:
        raise EvaluationInputError(f"{label} is not valid YAML: {path}") from exc
    if not isinstance(value, dict):
        raise EvaluationInputError(f"{label} must be a YAML object")
    return value


def load_selected_cases(manifest_path: Path) -> tuple[dict[str, Any], tuple[SelectedCase, ...]]:
    manifest = load_yaml_mapping(manifest_path, "evaluation manifest")
    source_path = (manifest_path.parent / str(manifest.get("source_suite", ""))).resolve()
    holdout_path = (
        manifest_path.parent / str(manifest.get("protected_holdout_manifest", ""))
    ).resolve()
    source = load_yaml_mapping(source_path, "source suite")
    holdout = load_yaml_mapping(holdout_path, "protected holdout manifest")
    source_cases = source.get("cases")
    if not isinstance(source_cases, list):
        raise EvaluationInputError("source suite cases must be a list")
    source_ids = validate_manifest_cases(manifest, source_cases, holdout)
    cases_by_id = {
        str(case.get("id")): case
        for case in source_cases
        if isinstance(case, Mapping) and isinstance(case.get("id"), str)
    }
    selected: list[SelectedCase] = []
    for source_id in source_ids:
        question = cases_by_id[source_id].get("question")
        if not isinstance(question, str) or not question.strip():
            raise EvaluationInputError(f"source case has no usable question: {source_id}")
        selected.append(SelectedCase(source_id=source_id, question=question.strip()))
    return manifest, tuple(selected)


def load_model(
    model_dir: Path, *, run_label: str, adapter_dir: Path | None
) -> tuple[Any, Any, dict[str, Any], Mapping[str, Any]]:
    manifest_path = model_dir / "download_manifest.json"
    if not manifest_path.is_file():
        raise EvaluationInputError("model directory lacks download_manifest.json")
    try:
        model_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvaluationInputError("model download manifest is invalid JSON") from exc
    if model_manifest.get("model_id") != EXPECTED_MODEL_ID:
        raise EvaluationInputError("evaluation requires the frozen Qwen 1.5B base model")
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
    adapter_metadata: dict[str, Any] = {"enabled": False}
    if run_label == "adapter":
        if adapter_dir is None:
            raise EvaluationInputError("adapter run requires --adapter-dir")
        config_path = adapter_dir / "adapter_config.json"
        model_path = adapter_dir / "adapter_model.safetensors"
        if not config_path.is_file() or not model_path.is_file():
            raise EvaluationInputError("adapter directory lacks PEFT adapter files")
        model = PeftModel.from_pretrained(model, adapter_dir, is_trainable=False)
        adapter_metadata = {
            "enabled": True,
            "adapter_config_sha256": sha256_file(config_path),
            "adapter_model_sha256": sha256_file(model_path),
            "adapter_model_bytes": model_path.stat().st_size,
        }
    elif adapter_dir is not None:
        raise EvaluationInputError("base run must not receive --adapter-dir")
    model.eval()
    return tokenizer, model, adapter_metadata, model_manifest


def prepare_case_context(
    case: SelectedCase,
    *,
    retriever: CatalogRetriever,
    router: QuestionRouter,
    user: User,
    run_label: str,
) -> tuple[CandidateSqlContext, ToolContext, str]:
    """Rebuild exactly the server-owned SQL context used by the trusted runtime."""
    selection = retriever.retrieve(case.question, user)
    route = router.classify(case.question, user=user, selection=selection)
    require_database_route(route)
    memory = WorkingMemory().apply(case.question, route)
    plan = QueryPlan.from_selection(selection, case.question, route, memory.as_dict())
    contract = ResultContract.from_selection(
        selection,
        case.question,
        memory.time_range,
        catalog=retriever.catalog,
        required_result_columns=plan.required_result_columns,
        requested_dimensions=plan.dimensions,
    )
    candidate_context = CandidateSqlContext(
        question=case.question,
        catalog_prompt=selection.prompt,
        query_plan_prompt=plan.prompt_context(),
        required_result_columns=contract.required_result_columns,
        dialect="PostgreSQL",
    )
    request_suffix = uuid.uuid4().hex
    tool_context = ToolContext(
        user=user,
        conversation_id=f"post-training-olist-{run_label}-{case.source_id}-{request_suffix}",
        request_id=f"post-training-olist-{run_label}-{case.source_id}-{request_suffix}",
        agent_memory=DemoAgentMemory(),
        metadata={
            "question": case.question,
            "evaluation_purpose": "offline_olist_candidate_sql",
            "prompt_version": OLIST_CANDIDATE_SQL_PROMPT_VERSION,
            "query_plan": plan.as_dict(),
            **contract.as_tool_metadata(),
        },
    )
    return candidate_context, tool_context, route.state


def generate_completion(
    tokenizer: Any,
    model: Any,
    prompt: str,
    *,
    max_input_tokens: int,
    max_new_tokens: int,
) -> tuple[str, int, int]:
    encoded = tokenizer(prompt, add_special_tokens=False, return_tensors="pt")
    input_ids = encoded["input_ids"]
    if input_ids.shape[-1] > max_input_tokens:
        raise EvaluationInputError("candidate prompt exceeds the frozen input-token limit")
    batch = {name: value.to("cuda:0") for name, value in encoded.items()}
    started_at = perf_counter()
    with torch.inference_mode():
        output_ids = model.generate(
            **batch,
            do_sample=False,
            num_beams=1,
            max_new_tokens=max_new_tokens,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
            use_cache=True,
        )
    completion_ids = output_ids[0, input_ids.shape[-1] :]
    completion = tokenizer.decode(completion_ids, skip_special_tokens=True).strip()
    if not completion:
        raise CandidateSqlGenerationError("model generated an empty completion")
    return completion, int(completion_ids.shape[-1]), round((perf_counter() - started_at) * 1_000)


async def execute_candidate(
    runner: SecurePostgresRunner, sql: str, context: ToolContext
) -> tuple[str, str, str | None, bool, str | None]:
    """Execute through the production boundary without writing error text to reports."""
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
    validation = context.metadata.get("result_validation")
    state = validation.get("state") if isinstance(validation, Mapping) else None
    satisfied = context.metadata.get("result_contract_satisfied") is True
    if state != "valid" or not satisfied:
        return "accepted", "executed", str(state) if state else None, False, "missing_valid_contract_state"
    return "accepted", "executed", "valid", True, None


def append_raw_candidate(path: Path, *, source_id: str, candidate_sql: str) -> None:
    """Persist raw model SQL only in the caller-selected external artifact directory."""
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {"source_id": source_id, "candidate_sql": candidate_sql},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        stream.flush()
        os.fsync(stream.fileno())


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    os.environ.setdefault("NCCL_P2P_DISABLE", "1")
    os.environ.setdefault("NCCL_IB_DISABLE", "1")
    if not torch.cuda.is_available():
        raise EvaluationInputError("CUDA is required for Olist candidate evaluation")
    if args.max_input_tokens <= 0 or args.max_new_tokens <= 0:
        raise EvaluationInputError("token limits must be positive")

    manifest_path = args.manifest.resolve()
    if not manifest_path.is_relative_to(ROOT):
        raise EvaluationInputError("evaluation manifest must be versioned in this repository")
    model_dir = ensure_path_outside_repository(args.model_dir, ROOT)
    output_dir = ensure_path_outside_repository(args.output_dir, ROOT)
    adapter_dir = (
        ensure_path_outside_repository(args.adapter_dir, ROOT)
        if args.adapter_dir is not None
        else None
    )
    if output_dir.exists() and any(output_dir.iterdir()):
        raise EvaluationInputError("output directory must be new and empty")

    manifest, cases = load_selected_cases(manifest_path)
    frozen_model = manifest.get("model")
    if not isinstance(frozen_model, Mapping):
        raise EvaluationInputError("manifest model contract is missing")
    if frozen_model.get("id") != EXPECTED_MODEL_ID:
        raise EvaluationInputError("manifest model ID differs from the required base model")
    if frozen_model.get("base_weight_mode") != "bf16_lora":
        raise EvaluationInputError("this business transfer evaluation is frozen to bf16 LoRA")
    generation = manifest.get("generation")
    if not isinstance(generation, Mapping) or generation.get("prompt_version") != OLIST_CANDIDATE_SQL_PROMPT_VERSION:
        raise EvaluationInputError("manifest prompt version differs from the candidate generator")
    decode = generation.get("decode")
    if not isinstance(decode, Mapping):
        raise EvaluationInputError("manifest decode contract is missing")
    if args.seed != decode.get("seed") or args.max_new_tokens != decode.get("max_new_tokens"):
        raise EvaluationInputError("CLI generation settings differ from the frozen manifest")

    output_dir.mkdir(parents=True, exist_ok=False)
    raw_candidates_path = output_dir / "raw-candidates.jsonl"
    safe_report_path = output_dir / "safe-report.json"
    random.seed(args.seed)
    np.random.seed(args.seed)
    set_seed(args.seed)
    tokenizer, model, adapter_metadata, model_manifest = load_model(
        model_dir, run_label=args.run_label, adapter_dir=adapter_dir
    )
    if model_manifest.get("revision") != frozen_model.get("revision"):
        raise EvaluationInputError("model revision differs from the frozen manifest")
    gpu = torch.cuda.get_device_properties(0)
    gpu_uuid = str(gpu.uuid)
    if not gpu_uuid.startswith("GPU-"):
        gpu_uuid = "GPU-" + gpu_uuid
    if gpu_uuid != args.expected_gpu_uuid:
        raise EvaluationInputError(
            f"CUDA device UUID does not match launcher guard: expected {args.expected_gpu_uuid}, got {gpu_uuid}"
        )
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()

    catalog = CatalogLoader().load()
    retriever = CatalogRetriever(catalog)
    router = QuestionRouter(retriever)
    user = User(id="post-training-olist-evaluator", group_memberships=["analyst"])
    settings = PostgresConnectionSettings.from_environment()
    runner = SecurePostgresRunner(
        settings=settings,
        result_validator=ResultValidator(settings.max_rows),
        model_name=f"post-training/{EXPECTED_MODEL_ID}:{args.run_label}",
    )
    started_at = datetime.now(timezone.utc)
    records: list[CandidateEvaluationRecord] = []
    for case in cases:
        try:
            candidate_context, tool_context, route_state = prepare_case_context(
                case,
                retriever=retriever,
                router=router,
                user=user,
                run_label=args.run_label,
            )
        except CandidateSqlGenerationError:
            records.append(
                CandidateEvaluationRecord(
                    source_id=case.source_id,
                    route_state="not_answerable",
                    generation_status="skipped",
                    generated_tokens=None,
                    generation_elapsed_ms=None,
                    policy_status="not_run",
                    execution_status="not_run",
                    result_validation_state=None,
                    result_contract_satisfied=False,
                    failure_category="route_not_answerable",
                )
            )
            continue

        try:
            raw_completion, token_count, elapsed_ms = generate_completion(
                tokenizer,
                model,
                render_candidate_sql_prompt(candidate_context),
                max_input_tokens=args.max_input_tokens,
                max_new_tokens=args.max_new_tokens,
            )
            candidate_sql = unwrap_sql_completion(raw_completion)
        except (CandidateSqlGenerationError, EvaluationInputError):
            records.append(
                CandidateEvaluationRecord(
                    source_id=case.source_id,
                    route_state=route_state,
                    generation_status="failed",
                    generated_tokens=None,
                    generation_elapsed_ms=None,
                    policy_status="not_run",
                    execution_status="not_run",
                    result_validation_state=None,
                    result_contract_satisfied=False,
                    failure_category="generation_error",
                )
            )
            continue
        append_raw_candidate(raw_candidates_path, source_id=case.source_id, candidate_sql=candidate_sql)
        policy_status, execution_status, validation_state, satisfied, failure_category = asyncio.run(
            execute_candidate(runner, candidate_sql, tool_context)
        )
        records.append(
            CandidateEvaluationRecord(
                source_id=case.source_id,
                route_state=route_state,
                generation_status="generated",
                generated_tokens=token_count,
                generation_elapsed_ms=elapsed_ms,
                policy_status=policy_status,
                execution_status=execution_status,
                result_validation_state=validation_state,
                result_contract_satisfied=satisfied,
                failure_category=failure_category,
            )
        )

    source_path = (manifest_path.parent / str(manifest["source_suite"])).resolve()
    holdout_path = (
        manifest_path.parent / str(manifest["protected_holdout_manifest"])
    ).resolve()
    comparison_contract = {
        "manifest_id": manifest["manifest_id"],
        "manifest_sha256": sha256_file(manifest_path),
        "source_suite_sha256": sha256_file(source_path),
        "holdout_manifest_sha256": sha256_file(holdout_path),
        "source_ids": [case.source_id for case in cases],
        "prompt_version": OLIST_CANDIDATE_SQL_PROMPT_VERSION,
        "model_id": model_manifest["model_id"],
        "model_revision": model_manifest["revision"],
        "base_weight_mode": "bf16_lora",
        "decode": {
            "do_sample": False,
            "num_beams": 1,
            "max_new_tokens": args.max_new_tokens,
            "seed": args.seed,
        },
        "sql_dialect": "postgres",
        "repair_enabled": False,
    }
    report = build_safe_report(
        report_metadata={
            "experiment_type": "olist_business_candidate_sql_transfer_evaluation",
            "run_label": args.run_label,
            "started_at": started_at.replace(microsecond=0).isoformat(),
            "finished_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "comparison_contract": comparison_contract,
            "adapter": adapter_metadata,
            "catalog": {
                "catalog_version": catalog.catalog_version,
                "dataset_version": catalog.dataset_version,
                "metric_version": catalog.metric_version,
                "policy_version": catalog.policy_version,
            },
            "gpu": {
                "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
                "process_local_device": 0,
                "physical_nvidia_smi_device": args.physical_nvidia_smi_device,
                "name": gpu.name,
                "uuid": gpu_uuid,
                "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
            },
            "raw_artifacts": {
                "raw_candidates_sha256": sha256_file(raw_candidates_path)
                if raw_candidates_path.exists()
                else None,
                "raw_candidates_outside_repository": True,
            },
            "boundaries": {
                "production_default_unchanged": True,
                "repair_enabled": False,
                "raw_questions_in_repository": False,
                "raw_candidate_sql_in_repository": False,
                "raw_result_rows_in_repository": False,
            },
        },
        records=records,
    )
    safe_report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"safe_report": report["summary"], "run_label": args.run_label}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EvaluationInputError, OlistCandidateEvaluationError, ValueError) as exc:
        print(f"olist candidate evaluation input error: {exc}", file=os.sys.stderr)
        raise SystemExit(2) from exc
