"""Admit a small Olist runtime QueryPlan Gold batch.

This is a CPU/database gate before aligned Base/Adapter generation.  It reads
only the frozen runtime validation inputs, renders server-owned Gold SQL, then
checks Policy, the analytics reader role and ResultValidator.  Raw SQL and
result rows stay out of the report; hashes and bounded execution evidence are
enough to reproduce the release without turning the report into a label dump.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import pandas as pd
import psycopg2

from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE
from data_analysis_agent.postgres_runner import cap_probed_rows, execution_probe
from data_analysis_agent.query_plan import MetricPlan, QueryPlan
from data_analysis_agent.result_validator import ResultValidator
from data_analysis_agent.runtime_queryplan_renderer import (
    RuntimeGoldRenderError,
    render_runtime_queryplan_gold_sql,
)
from data_analysis_agent.semantic_catalog import (
    AttributionRequirement,
    CatalogLoader,
    ResultContract,
)
from data_analysis_agent.sql_policy import PolicyViolation, SqlPolicy


DEFAULT_INPUT = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/"
    "runtime-candidate-sql-v2-r1/validation.jsonl"
)
DEFAULT_OUTPUT = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/"
    "runtime-queryplan-gold-admission-20261012-v1"
)
RENDERER_VERSION = "runtime-queryplan-gold-admission-v1"
DATABASE = "data_analysis_agent"
HOST = "/tmp"
PORT = 35434
MAX_ROWS = 1_000
DISPLAY_ROWS = 200


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError("validation input must be non-empty JSONL objects")
    return rows


def shape_of(plan: Mapping[str, Any]) -> str:
    if plan.get("time_grain"):
        return "time_series"
    dimensions = tuple(plan.get("dimensions") or ())
    if not dimensions:
        return "scalar"
    return dimensions[0]


def select_stratified(rows: list[dict[str, Any]], rows_per_shape: int) -> list[dict[str, Any]]:
    candidates = [
        row for row in rows
        if row.get("workspace_key") == "olist"
        and row.get("split", {}).get("name") == "validation"
        and row.get("admission_status") == "admitted"
        and isinstance(row.get("query_plan"), Mapping)
        and isinstance(row.get("result_contract"), Mapping)
    ]
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(candidates, key=lambda item: item["sample_id"]):
        groups.setdefault(shape_of(row["query_plan"]), []).append(row)
    wanted = ("scalar", "customer_state", "product_category_name", "time_series")
    selected: list[dict[str, Any]] = []
    for shape in wanted:
        group = groups.get(shape, [])
        if len(group) < rows_per_shape:
            raise ValueError(f"not enough {shape} validation rows: {len(group)}")
        # Prefer different metric counts and time grains inside each shape;
        # deterministic ordering keeps the admission manifest reproducible.
        chosen: list[dict[str, Any]] = []
        seen: set[tuple[int, str | None]] = set()
        for row in group:
            plan = row["query_plan"]
            key = (len(plan.get("metric_ids", ())), plan.get("time_grain"))
            if key not in seen:
                chosen.append(row)
                seen.add(key)
            if len(chosen) == rows_per_shape:
                break
        for row in group:
            if len(chosen) == rows_per_shape:
                break
            if row not in chosen:
                chosen.append(row)
        selected.extend(chosen)
    return selected


def select_family_stratified(rows: list[dict[str, Any]], max_families: int) -> list[dict[str, Any]]:
    """Choose one deterministic representative from a risk-aware family set.

    A full validation release has more than one hundred small families.  The
    pre-GPU gate should cover family/risk semantics without turning into a
    second full evaluation, so greedy set cover selects at most the requested
    number while prioritizing unseen risk tags, shapes and time grains.
    """

    candidates = [
        row for row in rows
        if row.get("workspace_key") == "olist"
        and row.get("split", {}).get("name") == "validation"
        and row.get("admission_status") == "admitted"
        and isinstance(row.get("query_plan"), Mapping)
        and isinstance(row.get("result_contract"), Mapping)
    ]
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(candidates, key=lambda item: item["sample_id"]):
        family = row.get("scenario_family_id")
        if isinstance(family, str) and family:
            groups.setdefault(family, []).append(row)
    if not groups:
        raise ValueError("validation rows do not contain scenario_family_id")
    representatives = [group[0] for group in groups.values()]
    all_risks = {
        tag for row in representatives for tag in (row.get("risk_tags") or ())
    }
    covered_risks: set[str] = set()
    covered_shapes: set[str] = set()
    covered_grains: set[str | None] = set()
    selected: list[dict[str, Any]] = []
    while representatives and len(selected) < max_families:
        def score(row: Mapping[str, Any]) -> tuple[int, int, int, str]:
            shape = shape_of(row["query_plan"])
            grain = row["query_plan"].get("time_grain")
            risks = set(row.get("risk_tags") or ())
            return (
                len(risks - covered_risks) * 100,
                int(shape not in covered_shapes) * 10,
                int(grain not in covered_grains),
                str(row["scenario_family_id"]),
            )

        chosen = max(representatives, key=score)
        selected.append(chosen)
        representatives.remove(chosen)
        covered_risks.update(chosen.get("risk_tags") or ())
        covered_shapes.add(shape_of(chosen["query_plan"]))
        covered_grains.add(chosen["query_plan"].get("time_grain"))
    if len(selected) < max_families:
        # The caller may request more families than exist; the manifest should
        # report the real count rather than fabricate duplicate structures.
        return selected
    return selected


def query_plan_from_mapping(raw: Mapping[str, Any]) -> QueryPlan:
    metric_plans = tuple(
        MetricPlan(
            metric_id=str(item["metric_id"]),
            grain=str(item["grain"]),
            time_field=str(item["time_field"]),
            source_tables=tuple(item["source_tables"]),
            default_filters=tuple(item["default_filters"]),
            allowed_dimensions=tuple(item["allowed_dimensions"]),
        )
        for item in raw["metric_plans"]
    )
    requirements = tuple(
        AttributionRequirement(
            metric_id=str(item["metric_id"]),
            metric_grain=str(item["metric_grain"]),
            dimension=str(item["dimension"]),
            policy_mode=item["policy_mode"],
            attribution_rule_id=item.get("attribution_rule_id"),
        )
        for item in raw.get("attribution_requirements", ())
    )
    return QueryPlan(
        plan_type=raw["plan_type"],
        intent=str(raw["intent"]),
        metric_ids=tuple(raw["metric_ids"]),
        dimensions=tuple(raw.get("dimensions") or ()),
        time_grain=raw.get("time_grain"),
        time_range=dict(raw["time_range"]) if raw.get("time_range") else None,
        comparison_baseline=raw.get("comparison_baseline"),
        required_result_columns=tuple(raw["required_result_columns"]),
        metric_plans=metric_plans,
        attribution_requirements=requirements,
        execution_strategy=str(raw["execution_strategy"]),
        warnings=tuple(raw.get("warnings") or ()),
        top_n=tuple(sorted((raw.get("top_n") or {}).items())),
    )


def result_contract_from_mapping(raw: Mapping[str, Any]) -> ResultContract:
    requirements = tuple(
        AttributionRequirement(
            metric_id=str(item["metric_id"]),
            metric_grain=str(item["metric_grain"]),
            dimension=str(item["dimension"]),
            policy_mode=item["policy_mode"],
            attribution_rule_id=item.get("attribution_rule_id"),
        )
        for item in raw.get("attribution_requirements", ())
    )
    return ResultContract(
        catalog_version=str(raw["catalog_version"]),
        dataset_version=str(raw["dataset_version"]),
        metric_version=str(raw["metric_version"]),
        policy_version=str(raw["policy_version"]),
        metric_ids=tuple(raw["metric_ids"]),
        required_result_columns=tuple(raw["required_result_columns"]),
        metric_result_columns=tuple(raw["metric_result_columns"]),
        result_time_column=raw.get("result_time_column"),
        result_time_column_aliases=tuple(raw.get("result_time_column_aliases") or ()),
        requested_start=raw.get("requested_start"),
        requested_end=raw.get("requested_end"),
        requested_end_exclusive=bool(
            raw.get("requested_end_exclusive", raw.get("requested_end") is not None)
        ),
        selected_join_ids=tuple(raw.get("selected_join_ids") or ()),
        result_column_labels=MappingProxyType(dict(raw.get("result_column_labels") or {})),
        metric_fact_grains=MappingProxyType(dict(raw.get("metric_fact_grains") or {})),
        exact_result_columns=bool(raw.get("exact_result_columns", True)),
        metric_value_constraints=MappingProxyType(
            {
                metric_id: MappingProxyType(dict(constraints))
                for metric_id, constraints in (raw.get("metric_value_constraints") or {}).items()
            }
        ),
        attribution_requirements=requirements,
    )


def execute_gold(sql: str, contract: ResultContract, plan: QueryPlan) -> dict[str, Any]:
    decision = SqlPolicy(
        analyst_limit=MAX_ROWS,
        admin_limit=MAX_ROWS,
        workspace=OLIST_RUNTIME_WORKSPACE,
    ).evaluate(sql, "analyst")
    connection = psycopg2.connect(
        host=HOST,
        port=PORT,
        database=DATABASE,
        user=OLIST_RUNTIME_WORKSPACE.reader_role,
    )
    started_state = "valid"
    try:
        connection.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL search_path = analytics, pg_catalog")
            cursor.execute("SET LOCAL TIME ZONE 'UTC'")
            cursor.execute("SET LOCAL statement_timeout = 5000")
            cursor.execute("SELECT current_user, current_setting('transaction_read_only')")
            reader, readonly = cursor.fetchone()
            if reader != OLIST_RUNTIME_WORKSPACE.reader_role or readonly != "on":
                raise RuntimeError("reader identity/read-only transaction mismatch")
            probe, fetch_count = execution_probe(
                decision.final_sql,
                decision.row_limit,
                policy_limit_applied=decision.policy_limit_applied,
            )
            cursor.execute(probe)
            columns = [column.name for column in cursor.description]
            rows, truncated = cap_probed_rows(
                cursor.fetchmany(fetch_count),
                decision.row_limit,
                policy_limit_applied=decision.policy_limit_applied,
            )
        frame = pd.DataFrame(rows, columns=columns)
    except psycopg2.Error as exc:
        return {
            "state": "postgres_execution_error",
            "sqlstate": exc.pgcode,
            "policy_sql_sha256": hashlib.sha256(decision.final_sql.encode()).hexdigest(),
        }
    finally:
        connection.close()

    validation = ResultValidator(max_rows=MAX_ROWS, display_row_budget=DISPLAY_ROWS).validate(
        frame,
        required_columns=contract.required_result_columns,
        required_column_aliases={},
        metric_columns=contract.metric_result_columns,
        time_column=contract.result_time_column,
        time_column_aliases=contract.result_time_column_aliases,
        time_bucket_grain=plan.time_grain,
        requested_start=contract.requested_start,
        requested_end=contract.requested_end,
        requested_end_exclusive=contract.requested_end_exclusive,
        exact_columns=contract.exact_result_columns,
        metric_value_constraints=contract.metric_value_constraints,
        execution_truncated=truncated,
    )
    if not validation.safe_to_answer or not validation.safe_to_display:
        started_state = "result_contract_rejected"
    return {
        "state": started_state,
        "validation_state": validation.state,
        "reader_role": OLIST_RUNTIME_WORKSPACE.reader_role,
        "transaction_read_only": True,
        "row_count": len(frame),
        "display_row_budget": DISPLAY_ROWS,
        "execution_truncated": truncated,
        "policy_limit": decision.row_limit,
        "policy_sql_sha256": hashlib.sha256(decision.final_sql.encode()).hexdigest(),
    }


def admit_row(row: Mapping[str, Any], catalog) -> dict[str, Any]:
    plan = query_plan_from_mapping(row["query_plan"])
    contract = result_contract_from_mapping(row["result_contract"])
    base = {
        "sample_id": row["sample_id"],
        "workspace": row["workspace_key"],
        "shape": shape_of(row["query_plan"]),
        "metric_ids": list(plan.metric_ids),
        "dimensions": list(plan.dimensions),
        "time_grain": plan.time_grain,
        "query_plan_sha256": sha256_json(plan.as_dict()),
        "result_contract_sha256": sha256_json(contract.as_evidence()),
        "renderer_version": RENDERER_VERSION,
    }
    try:
        artifact = render_runtime_queryplan_gold_sql(
            plan,
            contract,
            catalog,
            workspace=OLIST_RUNTIME_WORKSPACE,
        )
    except RuntimeGoldRenderError as exc:
        return {
            **base,
            "state": "renderer_rejected",
            "reason_code": exc.reason_code,
            "reason": str(exc),
        }
    try:
        policy_sql_sha = hashlib.sha256(
            SqlPolicy(analyst_limit=MAX_ROWS, workspace=OLIST_RUNTIME_WORKSPACE)
            .evaluate(artifact.sql, "analyst")
            .final_sql.encode()
        ).hexdigest()
        execution = execute_gold(artifact.sql, contract, plan)
    except PolicyViolation as exc:
        return {**base, "state": "policy_rejected", "reason": str(exc)}
    return {
        **base,
        "state": execution["state"] if execution["state"] != "valid" else "admitted",
        "sql_sha256": artifact.sql_sha256,
        "policy_sql_sha256": policy_sql_sha,
        "gold_evidence": dict(artifact.evidence),
        "execution": execution,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--rows-per-shape", type=int, default=4)
    parser.add_argument(
        "--max-families",
        type=int,
        default=None,
        help="select one risk-aware representative per family, capped at this count",
    )
    args = parser.parse_args()
    if args.rows_per_shape < 1:
        raise SystemExit("--rows-per-shape must be positive")
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite existing output: {args.output}")

    source_rows = load_rows(args.input)
    if args.max_families is not None and args.max_families < 1:
        raise SystemExit("--max-families must be positive")
    selected = (
        select_family_stratified(source_rows, args.max_families)
        if args.max_families is not None
        else select_stratified(source_rows, args.rows_per_shape)
    )
    catalog = CatalogLoader(OLIST_RUNTIME_WORKSPACE).load()
    args.output.mkdir(parents=True)
    manifest = {
        "version": RENDERER_VERSION,
        "source_path": str(args.input),
        "source_sha256": sha256_file(args.input),
        "workspace": "olist",
        "split": "validation",
        "rows_per_shape": args.rows_per_shape,
        "max_families": args.max_families,
        "selected_rows": len(selected),
        "source_selection_ids": [row["sample_id"] for row in selected],
        "source_family_ids": [row.get("scenario_family_id") for row in selected],
    }
    (args.output / "selection-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    results = [admit_row(row, catalog) for row in selected]
    with (args.output / "gold-admission.jsonl").open("w", encoding="utf-8") as stream:
        for result in results:
            stream.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    summary = {
        "version": RENDERER_VERSION,
        "manifest_sha256": sha256_file(args.output / "selection-manifest.json"),
        "rows": len(results),
        "state_counts": dict(Counter(result["state"] for result in results)),
        "shape_state_counts": dict(
            Counter(f"{result['shape']}:{result['state']}" for result in results)
        ),
        "result_sha256": sha256_file(args.output / "gold-admission.jsonl"),
    }
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
