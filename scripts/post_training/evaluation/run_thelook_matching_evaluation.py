#!/usr/bin/env python3
"""Run a frozen, matching Base/Adapter TheLook SQL evaluation.

Generation and trusted execution are deliberately separated from Gold SQL
comparison.  The runner reads only the question, QuerySpec and result
contract fields from ``cases.jsonl``; ``gold_sql`` is never loaded.  Raw model
SQL is written outside the repository, while the repository-safe report keeps
only aggregate execution outcomes.
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
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from data_analysis_agent.external_artifacts import ensure_path_outside_repository
from data_analysis_agent.thelook_context import THELOOK_WORKSPACE
from data_analysis_agent.thelook_queryspec import validate_thelook_query_spec, TheLookQuerySpec
from data_analysis_agent.semantic_catalog import CatalogLoader


MODEL_ID = "Qwen/Qwen2.5-Coder-1.5B"
PROMPT_VERSION = "thelook-candidate-sql-v1"
EXPECTED_CASES = 206
EXPECTED_MAX_INPUT = 3072
EXPECTED_MAX_NEW = 768


class TheLookMatchingError(ValueError):
    """The frozen matching contract cannot be trusted."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-jsonl", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--run-label", choices=("base", "adapter"), required=True)
    parser.add_argument("--adapter-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-input-tokens", type=int, default=EXPECTED_MAX_INPUT)
    parser.add_argument("--max-new-tokens", type=int, default=EXPECTED_MAX_NEW)
    parser.add_argument("--seed", type=int, default=20260908)
    parser.add_argument("--physical-nvidia-smi-device", type=int, required=True)
    parser.add_argument("--expected-gpu-uuid", required=True)
    return parser.parse_args()


def _read_cases(path: Path, manifest_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("evaluation_version") != "thelook-cross-schema-final-test-v1":
        raise TheLookMatchingError("unexpected TheLook evaluation version")
    output = manifest.get("output", {}).get("cases_jsonl", {})
    if output.get("rows") != EXPECTED_CASES or output.get("sha256") != sha256_file(path):
        raise TheLookMatchingError("cases hash or row count differs from frozen manifest")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        row = json.loads(line)
        if not isinstance(row, dict):
            raise TheLookMatchingError(f"case {line_number} is not an object")
        required = {"case_id", "question", "query_spec", "required_result_columns"}
        if set(row) < required:
            raise TheLookMatchingError(f"case {line_number} lacks matching fields")
        # Deliberately project fields instead of retaining gold_sql.
        rows.append({key: row[key] for key in required})
    if len(rows) != EXPECTED_CASES or [r["case_id"] for r in rows] != sorted(r["case_id"] for r in rows):
        raise TheLookMatchingError("case order or count is not frozen")
    if len({r["case_id"] for r in rows}) != len(rows):
        raise TheLookMatchingError("duplicate case IDs")
    catalog = CatalogLoader(THELOOK_WORKSPACE).load()
    for row in rows:
        spec = TheLookQuerySpec.from_mapping(row["query_spec"])
        validate_thelook_query_spec(spec, catalog)
        if tuple(row["required_result_columns"]) != spec.required_result_columns:
            raise TheLookMatchingError(f"result contract mismatch for {row['case_id']}")
    return rows, manifest


def _catalog_prompt() -> str:
    catalog = CatalogLoader(THELOOK_WORKSPACE).load()
    lines = [
        f"Catalog version: {catalog.catalog_version}; dataset: {catalog.dataset_version}; dialect: PostgreSQL",
        "Use only analytics views and the listed columns. IDs are join keys, not output columns.",
        "Tables:",
    ]
    for table in catalog.tables:
        columns = ", ".join(column.name for column in table.columns)
        lines.append(f"- analytics.{table.physical_name} ({table.grain}): {columns}")
    lines.append("Metrics:")
    for metric in catalog.metrics:
        lines.append(
            f"- {metric.metric_id}: {metric.description}; filters={'; '.join(metric.default_filters)}; "
            f"allowed_dimensions={', '.join(metric.allowed_dimensions)}"
        )
    lines.append("Joins:")
    for join in catalog.joins:
        lines.append(f"- {join.join_id}: {join.from_table} -> {join.to_table} ON {join.on}")
    return "\n".join(lines)


def _render_prompt(row: Mapping[str, Any], catalog_prompt: str) -> str:
    spec = row["query_spec"]
    contract = {"required_result_columns": row["required_result_columns"], "exact_result_columns": True}
    return "\n".join(
        [
            "### Task",
            "Generate exactly one read-only SQL query for the supplied Chinese business question.",
            "### SQL dialect\nPostgreSQL",
            "### Candidate contract",
            "- Return SQL only: no Markdown, explanation, tool call, or prose.",
            "- Generate one SELECT or WITH ... SELECT statement only.",
            "- Use only tables, columns, joins, metrics, and filters in the server-provided Catalog.",
            "- The server independently enforces SQL policy, readonly access, and the result contract.",
            "### Server-provided Semantic Catalog",
            catalog_prompt,
            "### Server-provided QuerySpec",
            json.dumps(spec, ensure_ascii=False, sort_keys=True),
            "### Server-provided Result Contract",
            json.dumps(contract, ensure_ascii=False, sort_keys=True),
            "### Question",
            str(row["question"]),
            "### SQL",
        ]
    )


def _load_model(model_dir: Path, run_label: str, adapter_dir: Path | None) -> tuple[Any, Any, dict[str, Any], dict[str, Any]]:
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    manifest = json.loads((model_dir / "download_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("model_id") != MODEL_ID:
        raise TheLookMatchingError("unexpected base model")
    tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"
    model = AutoModelForCausalLM.from_pretrained(
        model_dir, local_files_only=True, torch_dtype=torch.bfloat16, device_map={"": 0}
    )
    adapter_meta: dict[str, Any] = {"enabled": False}
    if run_label == "adapter":
        if adapter_dir is None:
            raise TheLookMatchingError("adapter run requires --adapter-dir")
        model_file = adapter_dir / "adapter_model.safetensors"
        config_file = adapter_dir / "adapter_config.json"
        if not model_file.is_file() or not config_file.is_file():
            raise TheLookMatchingError("adapter artifacts are incomplete")
        model = PeftModel.from_pretrained(model, adapter_dir, is_trainable=False)
        adapter_meta = {
            "enabled": True,
            "adapter_model_sha256": sha256_file(model_file),
            "adapter_config_sha256": sha256_file(config_file),
            "adapter_model_bytes": model_file.stat().st_size,
        }
    elif adapter_dir is not None:
        raise TheLookMatchingError("base run cannot load an adapter")
    model.eval()
    return tokenizer, model, adapter_meta, manifest


def _generate(tokenizer: Any, model: Any, prompt: str, max_input: int, max_new: int) -> tuple[str, int, int]:
    import torch
    encoded = tokenizer(prompt, add_special_tokens=False, return_tensors="pt")
    input_len = int(encoded["input_ids"].shape[-1])
    if input_len > max_input:
        raise TheLookMatchingError(f"prompt has {input_len} tokens, above {max_input}")
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
    tokens = output[0, input_len:]
    completion = tokenizer.decode(tokens, skip_special_tokens=True).strip()
    if not completion:
        raise TheLookMatchingError("model generated empty SQL")
    if completion.startswith("```") and completion.endswith("```"):
        lines = completion.splitlines()
        completion = "\n".join(lines[1:-1]).strip()
    if completion.lower().startswith("sql:"):
        completion = completion[4:].lstrip()
    return completion, int(tokens.shape[-1]), round((perf_counter() - started) * 1000)


def main() -> int:
    args = parse_args()
    import numpy as np
    import torch
    from transformers import set_seed

    if args.max_input_tokens != EXPECTED_MAX_INPUT or args.max_new_tokens != EXPECTED_MAX_NEW:
        raise TheLookMatchingError("generation limits differ from frozen matching contract")
    if not torch.cuda.is_available():
        raise TheLookMatchingError("CUDA is required")
    for path in (args.cases_jsonl, args.manifest, args.model_dir):
        ensure_path_outside_repository(path, ROOT)
    output_dir = ensure_path_outside_repository(args.output_dir, ROOT)
    if output_dir.exists():
        raise TheLookMatchingError("output directory must be new")
    adapter_dir = ensure_path_outside_repository(args.adapter_dir, ROOT) if args.adapter_dir else None
    rows, manifest = _read_cases(args.cases_jsonl, args.manifest)
    catalog_prompt = _catalog_prompt()
    prompts = [_render_prompt(row, catalog_prompt) for row in rows]
    random.seed(args.seed); np.random.seed(args.seed); set_seed(args.seed)
    tokenizer, model, adapter_meta, model_manifest = _load_model(args.model_dir, args.run_label, adapter_dir)
    token_lengths = [len(tokenizer(prompt, add_special_tokens=False)["input_ids"]) for prompt in prompts]
    if max(token_lengths) > args.max_input_tokens:
        raise TheLookMatchingError(f"prompt token length {max(token_lengths)} exceeds contract")
    device = torch.cuda.get_device_properties(0)
    uuid = str(device.uuid)
    if not uuid.startswith("GPU-"):
        uuid = "GPU-" + uuid
    if uuid != args.expected_gpu_uuid:
        raise TheLookMatchingError("CUDA UUID differs from the frozen guard")
    output_dir.mkdir(parents=True)
    raw_path = output_dir / "raw-candidates.jsonl"
    safe_records: list[dict[str, Any]] = []
    started = datetime.now(timezone.utc)
    for row, prompt in zip(rows, prompts):
        case_id = row["case_id"]
        try:
            sql, tokens, elapsed = _generate(tokenizer, model, prompt, args.max_input_tokens, args.max_new_tokens)
            with raw_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"case_id": case_id, "candidate_sql": sql}, ensure_ascii=False) + "\n")
        except Exception:
            tokens = elapsed = None
            failure = "generation_error"
        else:
            failure = None
        safe_records.append({
            "case_id": case_id,
            "generated_tokens": tokens,
            "generation_elapsed_ms": elapsed,
            "policy_status": "not_run",
            "execution_status": "not_run",
            "result_validation_state": None,
            "result_contract_satisfied": False,
            "failure_category": failure,
        })
    contract = {
        "dataset": "thelook-cross-schema-final-test-v1",
        "cases_sha256": sha256_file(args.cases_jsonl),
        "manifest_sha256": sha256_file(args.manifest),
        "case_count": len(rows),
        "prompt_format_version": PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256("\n".join(prompts).encode()).hexdigest(),
        "decode": {"do_sample": False, "num_beams": 1, "max_input_tokens": args.max_input_tokens, "max_new_tokens": args.max_new_tokens, "seed": args.seed},
        "gold_sql_read_for_generation": False,
        "database": "thelook_analytics",
        "reader_role": "daa_thelook_reader",
        "policy_version": THELOOK_WORKSPACE.policy_version,
    }
    report = {
        "report_schema_version": "thelook-matching-v1",
        "run_label": args.run_label,
        "started_at": started.replace(microsecond=0).isoformat(),
        "finished_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "model": {"id": model_manifest["model_id"], "revision": model_manifest["revision"], "base_weight_mode": "bf16", "adapter": adapter_meta},
        "comparison_contract": contract,
        "gpu": {"cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"), "physical_nvidia_smi_device": args.physical_nvidia_smi_device, "name": device.name, "uuid": uuid, "peak_allocated_bytes": torch.cuda.max_memory_allocated(), "peak_reserved_bytes": torch.cuda.max_memory_reserved()},
        "summary": {
            "case_count": len(safe_records),
            "generation_success": sum(r["failure_category"] is None for r in safe_records),
            "policy_accepted": 0,
            "postgres_executed": 0,
            "result_contract_valid": 0,
        },
        "records": safe_records,
        "raw_artifacts": {"raw_candidates_sha256": sha256_file(raw_path), "raw_candidates_outside_repository": True},
        "boundaries": {"gold_sql_read_for_generation": False, "production_default_unchanged": True},
    }
    (output_dir / "safe-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TheLookMatchingError, ValueError) as exc:
        print(f"thelook matching input error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
