"""Materialize the two-workspace runtime QueryPlan Gold release.

The model input and Gold sidecar are physically separate.  The input projection
contains the actual runtime prompt/plan/contract but never QuerySpec, Gold SQL,
candidate SQL, training text or historical execution evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE
from data_analysis_agent.runtime_queryplan_renderer import render_runtime_queryplan_gold_sql
from data_analysis_agent.runtime_thelook_queryplan_renderer import render_runtime_thelook_queryplan_gold_sql
from data_analysis_agent.semantic_catalog import CatalogLoader
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE
from scripts.post_training.evaluation.admit_runtime_queryplan_gold_batch import (
    query_plan_from_mapping as olist_query_plan_from_mapping,
    result_contract_from_mapping as olist_result_contract_from_mapping,
)
from scripts.post_training.evaluation.admit_runtime_thelook_queryplan_gold_batch import (
    query_plan_from_mapping as thelook_query_plan_from_mapping,
    result_contract_from_mapping as thelook_result_contract_from_mapping,
)


DEFAULT_INPUT = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/"
    "runtime-candidate-sql-v2-r1/validation.jsonl"
)
DEFAULT_OUTPUT = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/"
    "runtime-queryplan-aligned-v1-20261012"
)
VERSION = "runtime-queryplan-aligned-release-v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError("input must be non-empty JSONL objects")
    return rows


def input_projection(row: dict[str, Any]) -> dict[str, Any]:
    forbidden = {
        "query_spec", "query_spec_id", "candidate_sql", "canonical_gold_sql_sha256",
        "execution_evidence", "training_text", "admission_status",
    }
    projected = {
        "source_id": row["sample_id"],
        "workspace": row["workspace_key"],
        "split": row["split"],
        "question": row["question"],
        "question_sha256": row["question_normalized_sha256"],
        "prompt": row["rendered_prompt"],
        "prompt_sha256": sha256_text(row["rendered_prompt"]),
        "query_plan": row["query_plan"],
        "result_contract": row["result_contract"],
        "catalog_snapshot": {
            key: row["result_contract"][key]
            for key in ("catalog_version", "dataset_version", "metric_version", "policy_version")
        },
        "scenario_family_id": row.get("scenario_family_id"),
        "risk_tags": list(row.get("risk_tags") or ()),
        "token_length": row.get("token_length"),
    }
    if forbidden & set(projected):
        raise ValueError("forbidden Gold/offline field leaked into input projection")
    return projected


def materialize_row(row: dict[str, Any], catalog_cache: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    workspace = row["workspace_key"]
    if workspace == "olist":
        catalog = catalog_cache.setdefault(workspace, CatalogLoader(OLIST_RUNTIME_WORKSPACE).load())
        plan = olist_query_plan_from_mapping(row["query_plan"])
        contract = olist_result_contract_from_mapping(row["result_contract"])
        artifact = render_runtime_queryplan_gold_sql(plan, contract, catalog, workspace=OLIST_RUNTIME_WORKSPACE)
    elif workspace == "thelook":
        catalog = catalog_cache.setdefault(workspace, CatalogLoader(THELOOK_V2_WORKSPACE).load())
        plan = thelook_query_plan_from_mapping(row["query_plan"])
        contract = thelook_result_contract_from_mapping(row["result_contract"])
        artifact = render_runtime_thelook_queryplan_gold_sql(plan, contract, catalog, workspace=THELOOK_V2_WORKSPACE)
    else:
        raise ValueError(f"unsupported workspace {workspace}")
    return input_projection(row), {
        "source_id": row["sample_id"],
        "workspace": workspace,
        "query_plan_sha256": artifact.query_plan_sha256,
        "result_contract_sha256": artifact.result_contract_sha256,
        "sql": artifact.sql,
        "sql_sha256": artifact.sql_sha256,
        "renderer_version": artifact.renderer_version,
        "required_result_columns": list(artifact.required_result_columns),
        "metric_ids": list(artifact.metric_ids),
        "dimensions": list(artifact.dimensions),
        "time_grain": artifact.time_grain,
        "join_program_id": artifact.join_program_id,
        "evidence": dict(artifact.evidence),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite existing output: {args.output}")
    rows = [
        row for row in load_rows(args.input)
        if row.get("split", {}).get("name") == "validation"
        and row.get("workspace_key") in {"olist", "thelook"}
        and row.get("admission_status") == "admitted"
    ]
    rows.sort(key=lambda row: (row["workspace_key"], row["sample_id"]))
    if len(rows) != 1_500 or len({row["sample_id"] for row in rows}) != len(rows):
        raise SystemExit(f"expected 1,500 unique admitted validation rows, got {len(rows)}")
    args.output.mkdir(parents=True)
    (args.output / "inputs").mkdir()
    (args.output / "gold").mkdir()
    cache: dict[str, Any] = {}
    inputs, golds = [], []
    for row in rows:
        runtime_input, gold = materialize_row(row, cache)
        inputs.append(runtime_input)
        golds.append(gold)
    input_path = args.output / "inputs" / "inputs.jsonl"
    gold_path = args.output / "gold" / "gold.jsonl"
    for path, values in ((input_path, inputs), (gold_path, golds)):
        with path.open("w", encoding="utf-8") as stream:
            for value in values:
                stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    manifest = {
        "version": VERSION,
        "source_path": str(args.input),
        "source_sha256": sha256_file(args.input),
        "rows": len(rows),
        "workspace_counts": {
            workspace: sum(row["workspace_key"] == workspace for row in rows)
            for workspace in ("olist", "thelook")
        },
        "input_sha256": sha256_file(input_path),
        "gold_sha256": sha256_file(gold_path),
        "input_fields_forbidden": [
            "query_spec", "query_spec_id", "candidate_sql", "gold_sql", "training_text",
            "execution_evidence",
        ],
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
