"""Admit TheLook runtime QueryPlan Gold SQL through its own workspace."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import pandas as pd
import psycopg2

from data_analysis_agent.postgres_runner import cap_probed_rows, execution_probe
from data_analysis_agent.query_plan import MetricPlan, QueryPlan
from data_analysis_agent.result_validator import ResultValidator
from data_analysis_agent.runtime_thelook_queryplan_renderer import (
    RuntimeTheLookGoldRenderError,
    render_runtime_thelook_queryplan_gold_sql,
)
from data_analysis_agent.semantic_catalog import AttributionRequirement, CatalogLoader, ResultContract
from data_analysis_agent.sql_policy import PolicyViolation, SqlPolicy
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE


DEFAULT_INPUT = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/"
    "runtime-candidate-sql-v2-r1/validation.jsonl"
)
DEFAULT_OUTPUT = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/"
    "runtime-thelook-queryplan-gold-admission-20261012-v1"
)
VERSION = "runtime-thelook-queryplan-gold-admission-v1"
DATABASE = "thelook_analytics"
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
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError("validation input must be non-empty JSONL objects")
    return rows


def shape_of(plan: Mapping[str, Any]) -> str:
    if plan.get("time_grain"):
        return "time_series"
    dimensions = tuple(plan.get("dimensions") or ())
    return "scalar" if not dimensions else dimensions[0]


def select_rows(rows: list[dict[str, Any]], mode: str, limit: int) -> list[dict[str, Any]]:
    candidates = [
        row for row in rows
        if row.get("workspace_key") == "thelook"
        and row.get("split", {}).get("name") == "validation"
        and row.get("admission_status") == "admitted"
        and isinstance(row.get("query_plan"), Mapping)
        and isinstance(row.get("result_contract"), Mapping)
    ]
    candidates.sort(key=lambda item: item["sample_id"])
    if mode == "all":
        return candidates
    if mode == "shape":
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in candidates:
            groups.setdefault(shape_of(row["query_plan"]), []).append(row)
        selected: list[dict[str, Any]] = []
        for shape in ("scalar", "event_browser", "event_type", "event_traffic_source", "user_country", "user_state", "user_traffic_source", "time_series"):
            selected.extend(groups.get(shape, [])[:limit])
        return selected
    # One deterministic representative per family, useful for a pre-full-run gate.
    selected, seen = [], set()
    for row in candidates:
        family = row.get("scenario_family_id")
        if family in seen:
            continue
        seen.add(family)
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def query_plan_from_mapping(raw: Mapping[str, Any]) -> QueryPlan:
    return QueryPlan(
        plan_type=raw["plan_type"], intent=str(raw["intent"]), metric_ids=tuple(raw["metric_ids"]),
        dimensions=tuple(raw.get("dimensions") or ()), time_grain=raw.get("time_grain"),
        time_range=dict(raw["time_range"]) if raw.get("time_range") else None,
        comparison_baseline=raw.get("comparison_baseline"),
        required_result_columns=tuple(raw["required_result_columns"]),
        metric_plans=tuple(MetricPlan(
            metric_id=str(item["metric_id"]), grain=str(item["grain"]),
            time_field=str(item["time_field"]), source_tables=tuple(item["source_tables"]),
            default_filters=tuple(item["default_filters"]), allowed_dimensions=tuple(item["allowed_dimensions"]),
        ) for item in raw["metric_plans"]),
        attribution_requirements=tuple(AttributionRequirement(
            metric_id=str(item["metric_id"]), metric_grain=str(item["metric_grain"]),
            dimension=str(item["dimension"]), policy_mode=item["policy_mode"],
            attribution_rule_id=item.get("attribution_rule_id"),
        ) for item in raw.get("attribution_requirements", ())),
        execution_strategy=str(raw["execution_strategy"]), warnings=tuple(raw.get("warnings") or ()),
        top_n=tuple(sorted((raw.get("top_n") or {}).items())),
    )


def result_contract_from_mapping(raw: Mapping[str, Any]) -> ResultContract:
    return ResultContract(
        catalog_version=str(raw["catalog_version"]), dataset_version=str(raw["dataset_version"]),
        metric_version=str(raw["metric_version"]), policy_version=str(raw["policy_version"]),
        metric_ids=tuple(raw["metric_ids"]), required_result_columns=tuple(raw["required_result_columns"]),
        metric_result_columns=tuple(raw["metric_result_columns"]), result_time_column=raw.get("result_time_column"),
        result_time_column_aliases=tuple(raw.get("result_time_column_aliases") or ()),
        requested_start=raw.get("requested_start"), requested_end=raw.get("requested_end"),
        requested_end_exclusive=bool(raw.get("requested_end_exclusive", raw.get("requested_end") is not None)),
        selected_join_ids=tuple(raw.get("selected_join_ids") or ()),
        result_column_labels=MappingProxyType(dict(raw.get("result_column_labels") or {})),
        metric_fact_grains=MappingProxyType(dict(raw.get("metric_fact_grains") or {})),
        exact_result_columns=bool(raw.get("exact_result_columns", True)),
        metric_value_constraints=MappingProxyType({
            metric: MappingProxyType(dict(value)) for metric, value in (raw.get("metric_value_constraints") or {}).items()
        }),
        attribution_requirements=tuple(AttributionRequirement(
            metric_id=str(item["metric_id"]), metric_grain=str(item["metric_grain"]),
            dimension=str(item["dimension"]), policy_mode=item["policy_mode"],
            attribution_rule_id=item.get("attribution_rule_id"),
        ) for item in raw.get("attribution_requirements", ())),
    )


def execute_gold(sql: str, plan: QueryPlan, contract: ResultContract) -> dict[str, Any]:
    decision = SqlPolicy(analyst_limit=MAX_ROWS, admin_limit=MAX_ROWS, workspace=THELOOK_V2_WORKSPACE).evaluate(sql, "analyst")
    connection = psycopg2.connect(host=HOST, port=PORT, database=DATABASE, user=THELOOK_V2_WORKSPACE.reader_role)
    try:
        connection.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL search_path = analytics, pg_catalog")
            cursor.execute("SET LOCAL TIME ZONE 'UTC'")
            cursor.execute("SET LOCAL statement_timeout = 5000")
            cursor.execute("SELECT current_user, current_setting('transaction_read_only')")
            reader, readonly = cursor.fetchone()
            if reader != THELOOK_V2_WORKSPACE.reader_role or readonly != "on":
                raise RuntimeError("TheLook reader identity/read-only mismatch")
            probe, count = execution_probe(decision.final_sql, decision.row_limit, policy_limit_applied=decision.policy_limit_applied)
            cursor.execute(probe)
            columns = [column.name for column in cursor.description]
            values, truncated = cap_probed_rows(cursor.fetchmany(count), decision.row_limit, policy_limit_applied=decision.policy_limit_applied)
        frame = pd.DataFrame(values, columns=columns)
    except psycopg2.Error as exc:
        return {"state": "postgres_execution_error", "sqlstate": exc.pgcode}
    finally:
        connection.close()
    validation = ResultValidator(max_rows=MAX_ROWS, display_row_budget=DISPLAY_ROWS).validate(
        frame, required_columns=contract.required_result_columns, required_column_aliases={},
        metric_columns=contract.metric_result_columns, time_column=contract.result_time_column,
        time_column_aliases=contract.result_time_column_aliases, time_bucket_grain=plan.time_grain,
        requested_start=contract.requested_start, requested_end=contract.requested_end,
        requested_end_exclusive=contract.requested_end_exclusive, exact_columns=contract.exact_result_columns,
        metric_value_constraints=contract.metric_value_constraints, execution_truncated=truncated,
    )
    return {
        "state": "valid" if validation.safe_to_answer and validation.safe_to_display else "result_contract_rejected",
        "validation_state": validation.state, "reader_role": THELOOK_V2_WORKSPACE.reader_role,
        "transaction_read_only": True, "row_count": len(frame), "display_row_budget": DISPLAY_ROWS,
        "execution_truncated": truncated, "policy_limit": decision.row_limit,
        "policy_sql_sha256": hashlib.sha256(decision.final_sql.encode()).hexdigest(),
    }


def admit(row: Mapping[str, Any], catalog) -> dict[str, Any]:
    plan, contract = query_plan_from_mapping(row["query_plan"]), result_contract_from_mapping(row["result_contract"])
    base = {
        "sample_id": row["sample_id"], "workspace": row["workspace_key"], "shape": shape_of(row["query_plan"]),
        "scenario_family_id": row.get("scenario_family_id"), "risk_tags": list(row.get("risk_tags") or ()),
        "metric_ids": list(plan.metric_ids), "dimensions": list(plan.dimensions), "time_grain": plan.time_grain,
        "query_plan_sha256": sha256_json(plan.as_dict()), "result_contract_sha256": sha256_json(contract.as_evidence()),
        "renderer_version": VERSION,
    }
    try:
        artifact = render_runtime_thelook_queryplan_gold_sql(plan, contract, catalog, workspace=THELOOK_V2_WORKSPACE)
        execution = execute_gold(artifact.sql, plan, contract)
    except RuntimeTheLookGoldRenderError as exc:
        return {**base, "state": "renderer_rejected", "reason_code": exc.reason_code, "reason": str(exc)}
    except PolicyViolation as exc:
        return {**base, "state": "policy_rejected", "reason": str(exc)}
    return {**base, "state": "admitted" if execution["state"] == "valid" else execution["state"],
            "sql_sha256": artifact.sql_sha256, "gold_evidence": dict(artifact.evidence), "execution": execution}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--mode", choices=("all", "shape", "family"), default="shape")
    parser.add_argument("--limit", type=int, default=4)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite {args.output}")
    rows = select_rows(load_rows(args.input), args.mode, args.limit)
    if not rows:
        raise SystemExit("selection is empty")
    catalog = CatalogLoader(THELOOK_V2_WORKSPACE).load()
    args.output.mkdir(parents=True)
    results = [admit(row, catalog) for row in rows]
    (args.output / "selection-manifest.json").write_text(json.dumps({
        "version": VERSION, "source_path": str(args.input), "source_sha256": sha256_file(args.input),
        "workspace": "thelook", "split": "validation", "mode": args.mode, "limit": args.limit,
        "rows": len(rows), "sample_ids": [row["sample_id"] for row in rows],
    }, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with (args.output / "gold-admission.jsonl").open("w", encoding="utf-8") as stream:
        for result in results:
            stream.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    summary = {"version": VERSION, "rows": len(results), "state_counts": dict(Counter(item["state"] for item in results)),
               "shape_state_counts": dict(Counter(f"{item['shape']}:{item['state']}" for item in results)),
               "result_sha256": sha256_file(args.output / "gold-admission.jsonl")}
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
