#!/usr/bin/env python3
"""Materialize trusted runtime prompts for an admitted Olist Gold release.

This is a prompt-construction gate, not a training-data builder.  It takes
external admitted Gold metadata and an external, human-reviewed Chinese
question overlay, then rebuilds the same Router, Catalog, QueryPlan and
ResultContract used by the application.  Raw questions and prompts therefore
remain outside the Git worktree.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import uuid
from typing import Any, Mapping

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.candidate_sql_generator import (  # noqa: E402
    CandidateSqlContext,
    OLIST_CANDIDATE_SQL_PROMPT_VERSION,
    render_candidate_sql_prompt,
    require_database_route,
)
from data_analysis_agent.olist_queryspec import QuerySpec, validate_query_spec  # noqa: E402
from data_analysis_agent.query_plan import QueryPlan  # noqa: E402
from data_analysis_agent.question_router import QuestionRouter  # noqa: E402
from data_analysis_agent.semantic_catalog import (  # noqa: E402
    CatalogLoader,
    CatalogRetriever,
    ResultContract,
)
from data_analysis_agent.working_memory import WorkingMemory  # noqa: E402
from vanna.core.user import User  # noqa: E402


SCHEMA_VERSION = "olist-runtime-prompt-materialization-v2"
OVERLAY_SCHEMA_VERSION = "1"
# A bounded release protects the prompt-materialization command from accidental
# unreviewed bulk inputs. It is not an expected release size.
MAX_SEEDS = 1500
MAX_VARIANTS = 1500


class RuntimePromptInputError(ValueError):
    """An external runtime prompt input violates the frozen contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimePromptInputError(f"{label} does not exist or is unreadable: {path}") from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimePromptInputError(f"{label} has invalid JSON at line {line_number}") from exc
        if not isinstance(value, dict):
            raise RuntimePromptInputError(f"{label} line {line_number} must be an object")
        rows.append(value)
    return rows


def _external_existing(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise RuntimePromptInputError(f"{label} must stay outside the Git worktree")
    if not resolved.is_file():
        raise RuntimePromptInputError(f"{label} does not exist: {resolved}")
    return resolved


def _external_new_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise RuntimePromptInputError("runtime prompt output must stay outside the Git worktree")
    if resolved.exists():
        raise RuntimePromptInputError(f"runtime prompt output already exists: {resolved}")
    return resolved


def _validate_admission_assembly(records_path: Path, manifest_path: Path) -> None:
    manifest_path = _external_existing(manifest_path, "admission assembly manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimePromptInputError("admission assembly manifest must be valid JSON") from exc
    if not isinstance(manifest, Mapping) or manifest.get("checks", {}).get("status") != "pass":
        raise RuntimePromptInputError("admission assembly manifest did not pass")
    output = manifest.get("output", {}).get("admitted_records_jsonl", {})
    if not isinstance(output, Mapping):
        raise RuntimePromptInputError("admission assembly manifest has no records evidence")
    rows = output.get("rows")
    if not isinstance(rows, int) or not 1 <= rows <= MAX_SEEDS:
        raise RuntimePromptInputError("admission assembly records have an unsupported row count")
    if rows != len(_read_jsonl(records_path, "admission records")) or output.get("sha256") != sha256_file(records_path):
        raise RuntimePromptInputError("admission assembly records do not match its manifest")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admission-records", type=Path, required=True)
    parser.add_argument("--admission-assembly-manifest", type=Path, default=None)
    parser.add_argument("--question-variants", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args(argv)


def load_question_variants(path: Path, source_ids: set[str]) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimePromptInputError("question variants must be valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise RuntimePromptInputError("question variants must be a JSON object")
    if set(payload) != {"schema_version", "language", "prompt_version", "cases"}:
        raise RuntimePromptInputError("question variants have unsupported top-level fields")
    if payload["schema_version"] != OVERLAY_SCHEMA_VERSION or payload["language"] != "zh":
        raise RuntimePromptInputError("question variants must be schema 1 and language zh")
    if payload["prompt_version"] != OLIST_CANDIDATE_SQL_PROMPT_VERSION:
        raise RuntimePromptInputError("question variants prompt version differs from runtime contract")
    raw_cases = payload["cases"]
    if not isinstance(raw_cases, list) or len(raw_cases) != len(source_ids):
        raise RuntimePromptInputError("question variants must contain exactly one case per admitted seed")
    result: dict[str, str] = {}
    for item in raw_cases:
        if not isinstance(item, Mapping) or set(item) != {"seed_id", "question"}:
            raise RuntimePromptInputError("each question variant requires only seed_id and question")
        seed_id, question = item["seed_id"], item["question"]
        if not isinstance(seed_id, str) or not seed_id.strip():
            raise RuntimePromptInputError("question variant seed_id must be non-empty")
        if seed_id in result or seed_id not in source_ids:
            raise RuntimePromptInputError(f"question variant seed_id is duplicate or unknown: {seed_id}")
        if not isinstance(question, str) or not question.strip():
            raise RuntimePromptInputError(f"question variant is empty: {seed_id}")
        result[seed_id] = question.strip()
    if set(result) != source_ids:
        raise RuntimePromptInputError("question variant IDs must exactly match admitted seeds")
    return result


def load_question_variant_cases(path: Path, source_ids: set[str]) -> list[dict[str, str]]:
    """Load the v2 overlay: exactly two reviewed paraphrases per seed."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimePromptInputError("question variants must be valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise RuntimePromptInputError("question variants must be a JSON object")
    if set(payload) != {"schema_version", "language", "prompt_version", "variant_policy", "cases"}:
        raise RuntimePromptInputError("v2 question variants have unsupported top-level fields")
    if payload["schema_version"] != "2" or payload["language"] != "zh":
        raise RuntimePromptInputError("v2 question variants must be schema 2 and language zh")
    if payload["prompt_version"] != OLIST_CANDIDATE_SQL_PROMPT_VERSION:
        raise RuntimePromptInputError("question variants prompt version differs from runtime contract")
    raw_cases = payload["cases"]
    if not isinstance(raw_cases, list) or len(raw_cases) != len(source_ids) * 2:
        raise RuntimePromptInputError("v2 question variants must contain exactly two cases per admitted seed")
    cases: list[dict[str, str]] = []
    seen_variant_ids: set[str] = set()
    counts: dict[str, int] = dict.fromkeys(source_ids, 0)
    for item in raw_cases:
        if not isinstance(item, Mapping) or set(item) != {"variant_id", "seed_id", "question"}:
            raise RuntimePromptInputError("each v2 question variant requires only variant_id, seed_id and question")
        variant_id, seed_id, question = item["variant_id"], item["seed_id"], item["question"]
        if not isinstance(variant_id, str) or not variant_id.strip() or variant_id in seen_variant_ids:
            raise RuntimePromptInputError(f"question variant variant_id is empty or duplicate: {variant_id}")
        if not isinstance(seed_id, str) or seed_id not in source_ids:
            raise RuntimePromptInputError(f"question variant seed_id is unknown: {seed_id}")
        if not isinstance(question, str) or not question.strip():
            raise RuntimePromptInputError(f"question variant is empty: {variant_id}")
        seen_variant_ids.add(variant_id)
        counts[seed_id] += 1
        cases.append({"variant_id": variant_id.strip(), "seed_id": seed_id, "question": question.strip()})
    if any(count != 2 for count in counts.values()):
        raise RuntimePromptInputError("v2 question variants must contain exactly two cases per admitted seed")
    return cases


def _expected_shape(spec: QuerySpec) -> str:
    if spec.result_shape == "time_series":
        return "time_series"
    if spec.result_shape == "state_grouped":
        return "state_grouped"
    if spec.result_shape == "category_grouped":
        return "category_grouped"
    return "scalar"


def _compare_contract(spec: QuerySpec, plan: QueryPlan, contract: ResultContract) -> list[str]:
    mismatches: list[str] = []
    if set(plan.metric_ids) != set(spec.metric_ids):
        mismatches.append("metric_ids")
    expected_dimensions = (spec.dimension,) if spec.dimension else ()
    if tuple(plan.dimensions) != expected_dimensions:
        mismatches.append("dimensions")
    if plan.time_grain != spec.time.grain:
        mismatches.append("time_grain")
    expected_range = None if spec.time.mode == "all_time" else {
        "start": spec.time.start,
        "end": spec.time.end_exclusive,
    }
    if plan.time_range != expected_range:
        mismatches.append("time_range")
    if set(plan.required_result_columns) != set(spec.required_result_columns):
        mismatches.append("plan_required_result_columns")
    if set(contract.required_result_columns) != set(spec.required_result_columns):
        mismatches.append("contract_required_result_columns")
    if _expected_shape(spec) == "state_grouped" and plan.plan_type not in {"single_metric", "grouped_multi_metric"}:
        mismatches.append("plan_type")
    if plan.warnings:
        mismatches.append("plan_warnings")
    return mismatches


def materialize(
    admission_records: Path,
    variants_path: Path,
    output_dir: Path,
    generated_at: str | None,
    admission_assembly_manifest: Path | None = None,
) -> dict[str, Any]:
    admission_records = _external_existing(admission_records, "admission records")
    records = _read_jsonl(admission_records, "admission records")
    if not records or len(records) > MAX_SEEDS:
        raise RuntimePromptInputError(f"admission records must contain 1-{MAX_SEEDS} rows")
    if admission_assembly_manifest is None:
        raise RuntimePromptInputError("runtime materialization requires an admission assembly manifest")
    _validate_admission_assembly(admission_records, admission_assembly_manifest)
    admitted = [row for row in records if row.get("admission_status") == "admitted"]
    if len(admitted) != len(records):
        raise RuntimePromptInputError("runtime materialization accepts admitted rows only")
    seed_ids = {str(row.get("seed_id")) for row in admitted}
    if None in seed_ids or "" in seed_ids or len(seed_ids) != len(admitted):
        raise RuntimePromptInputError("admission records must have unique non-empty seed IDs")
    variants_path = _external_existing(variants_path, "question variants")
    variant_payload = json.loads(variants_path.read_text(encoding="utf-8"))
    if variant_payload.get("schema_version") == "1":
        questions = load_question_variants(variants_path, seed_ids)
        variant_cases = [
            {"variant_id": f"v1-{seed_id}", "seed_id": seed_id, "question": questions[seed_id]}
            for seed_id in sorted(seed_ids)
        ]
    elif variant_payload.get("schema_version") == "2":
        variant_cases = load_question_variant_cases(variants_path, seed_ids)
    else:
        raise RuntimePromptInputError("question variants schema must be 1 or 2")
    if len(variant_cases) > MAX_VARIANTS:
        raise RuntimePromptInputError(f"question variants must contain at most {MAX_VARIANTS} cases")
    admission_by_seed = {str(row["seed_id"]): row for row in admitted}
    output = _external_new_dir(output_dir)

    load_dotenv(ROOT / ".env")
    catalog = CatalogLoader().load()
    retriever = CatalogRetriever(catalog)
    router = QuestionRouter(retriever)
    user = User(id="olist-runtime-prompt-materializer", group_memberships=["analyst"])
    runtime_rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for variant in variant_cases:
        seed_id = variant["seed_id"]
        question = variant["question"]
        row = admission_by_seed[seed_id]
        try:
            spec = QuerySpec.from_mapping(row["query_spec"])
            validate_query_spec(spec, catalog)
            selection = retriever.retrieve(question, user)
            route = router.classify(question, user=user, selection=selection)
            require_database_route(route)
            memory = WorkingMemory().apply(question, route)
            plan = QueryPlan.from_selection(selection, question, route, memory.as_dict())
            contract = ResultContract.from_selection(
                selection,
                question,
                memory.time_range,
                catalog=catalog,
                # QuerySpec owns the canonical output ordering.  Retrieval may
                # rank synonyms differently, but it must select the same set.
                required_result_columns=spec.required_result_columns,
                requested_dimensions=plan.dimensions,
            )
            mismatches = _compare_contract(spec, plan, contract)
            if mismatches:
                raise RuntimePromptInputError("runtime contract mismatch: " + ", ".join(mismatches))
            context = CandidateSqlContext(
                question=question,
                catalog_prompt=selection.prompt,
                query_plan_prompt=plan.prompt_context(),
                required_result_columns=contract.required_result_columns,
            )
            prompt = render_candidate_sql_prompt(context)
            runtime_rows.append({
                "seed_id": seed_id,
                "variant_id": variant["variant_id"],
                "split": row.get("split"),
                "family_id": row.get("family_id"),
                "sql_program_id": row.get("sql_program_id"),
                "query_spec_id": spec.query_spec_id,
                "question": question,
                "route": route.as_dict(),
                "selection_trace": selection.trace.as_dict(),
                "query_plan": plan.as_dict(),
                "result_contract": contract.as_evidence(),
                "prompt": prompt,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            })
        except Exception as exc:
            failures.append({"seed_id": seed_id, "status": "rejected", "reason": str(exc)[:500]})

    if failures:
        raise RuntimePromptInputError("runtime prompt materialization rejected one or more rows: " + json.dumps(failures, ensure_ascii=False))
    generated_at = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        runtime_path = staging / "runtime_candidates.jsonl"
        with runtime_path.open("x", encoding="utf-8") as handle:
            for item in runtime_rows:
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "input": {
                "admission_records_sha256": sha256_file(admission_records),
                "admission_assembly_manifest_sha256": (
                    sha256_file(admission_assembly_manifest)
                    if admission_assembly_manifest is not None
                    else None
                ),
                "question_variants_sha256": sha256_file(variants_path),
                "admitted_seed_ids": sorted(seed_ids),
            },
            "workspace": {
                "catalog_version": catalog.catalog_version,
                "dataset_version": catalog.dataset_version,
                "metric_version": catalog.metric_version,
                "policy_version": catalog.policy_version,
                "prompt_version": OLIST_CANDIDATE_SQL_PROMPT_VERSION,
            },
            "counts": {"input_rows": len(records), "materialized_rows": len(runtime_rows), "rejected_rows": 0},
            "checks": {
                "router_rebuilt": True,
                "catalog_rebuilt": True,
                "query_plan_rebuilt": True,
                "result_contract_rebuilt": True,
                "sql_executed": False,
                "model_called": False,
                "gpu_used": False,
                "protected_holdout_read": False,
            },
            "output": {"runtime_candidates_jsonl": {"rows": len(runtime_rows), "sha256": sha256_file(runtime_path)}},
        }
        (staging / "runtime_prompt_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = materialize(
        args.admission_records,
        args.question_variants,
        args.output_dir,
        args.generated_at,
        args.admission_assembly_manifest,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
