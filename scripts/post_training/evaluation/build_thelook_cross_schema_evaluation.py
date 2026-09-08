#!/usr/bin/env python3
"""Build the frozen TheLook cross-schema Base/Adapter evaluation set.

The command is deliberately an evaluation-set builder, not a training-data
builder. It enumerates reviewed QuerySpec families, renders deterministic Gold
SQL, validates every artifact through SqlPolicy and the read-only TheLook role,
then writes questions, SQL and evidence only outside the Git worktree.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any, Callable, Iterable
import uuid

import pandas as pd
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.result_validator import ResultValidator  # noqa: E402
from data_analysis_agent.semantic_catalog import Catalog, CatalogLoader  # noqa: E402
from data_analysis_agent.sql_policy import SqlPolicy  # noqa: E402
from data_analysis_agent.thelook_context import THELOOK_WORKSPACE  # noqa: E402
from data_analysis_agent.thelook_queryspec import (  # noqa: E402
    TheLookQuerySpec,
    TheLookQueryTime,
    validate_thelook_query_spec,
)
from data_analysis_agent.thelook_renderer import render_thelook_gold_sql  # noqa: E402


EVALUATION_VERSION = "thelook-cross-schema-final-test-v1"
QUESTION_LANGUAGE = "zh"
MAX_CASES = 300
EXPECTED_CASES = 206
_YEAR_WINDOWS = (
    ("2019-01-01", "2020-01-01"),
    ("2020-01-01", "2021-01-01"),
    ("2021-01-01", "2022-01-01"),
    ("2022-01-01", "2023-01-01"),
    ("2023-01-01", "2024-01-01"),
)
_STATE_WINDOWS = (
    ("2019-01-01", "2020-01-01"),
    ("2020-01-01", "2021-01-01"),
    ("2021-01-01", "2021-07-01"),
)
_METRICS = (
    "completed_sale_amount",
    "completed_order_count",
    "average_order_value",
    "average_fulfillment_days",
    "return_rate",
    "completed_customer_count",
)
_MULTI_METRICS = (
    ("completed_sale_amount", "completed_order_count"),
    ("completed_order_count", "completed_customer_count"),
    ("completed_order_count", "average_fulfillment_days"),
    ("completed_order_count", "return_rate"),
)
_LOW_CARDINALITY_DIMENSIONS = ("traffic_source", "category", "department")
_METRIC_LABELS = {
    "completed_sale_amount": "已完成成交额",
    "completed_order_count": "已完成订单数",
    "average_order_value": "平均完成订单金额",
    "average_fulfillment_days": "平均履约天数",
    "return_rate": "退货率",
    "completed_customer_count": "已完成客户数",
}
_DIMENSION_LABELS = {
    "state": "用户州/地区",
    "traffic_source": "流量来源",
    "category": "商品品类",
    "department": "商品部门",
}


class TheLookEvaluationBuildError(ValueError):
    """The frozen cross-schema evaluation release cannot be trusted."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args(argv)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _external_new_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise TheLookEvaluationBuildError("evaluation output must stay outside the Git worktree")
    if resolved.exists():
        raise FileExistsError(resolved)
    return resolved


def _metric_phrase(metrics: Iterable[str]) -> str:
    labels = [_METRIC_LABELS[metric] for metric in metrics]
    return "和".join(labels)


def _question(spec: TheLookQuerySpec, index: int) -> str:
    metrics = _metric_phrase(spec.metric_ids)
    if spec.time.mode == "all_time":
        period = "全部可用时间"
    else:
        period = f"{spec.time.start} 至 {spec.time.end_exclusive}（不含结束日）"
    if spec.result_shape == "scalar":
        templates = (
            "请汇总{period}的{metrics}。",
            "统计{period}内的{metrics}，按已冻结口径返回。",
        )
        return templates[index % len(templates)].format(period=period, metrics=metrics)
    if spec.result_shape == "dimension_grouped":
        dimension = _DIMENSION_LABELS[spec.dimension or ""]
        templates = (
            "按{dimension}统计{period}的{metrics}。",
            "请展示{period}内各{dimension}的{metrics}。",
        )
        return templates[index % len(templates)].format(
            dimension=dimension, period=period, metrics=metrics
        )
    templates = (
        "按{grain}展示{period}的{metrics}变化。",
        "请给出{period}内{metrics}的{grain}时间序列。",
    )
    return templates[index % len(templates)].format(
        grain={"day": "日", "week": "周", "month": "月", "quarter": "季度", "year": "年"}[spec.time.grain or ""],
        period=period,
        metrics=metrics,
    )


def _append_case(
    cases: list[dict[str, Any]],
    catalog: Catalog,
    *,
    metric_ids: tuple[str, ...],
    result_shape: str,
    time: TheLookQueryTime,
    dimension: str | None = None,
) -> None:
    spec = validate_thelook_query_spec(
        TheLookQuerySpec.create(
            metric_ids=metric_ids,
            result_shape=result_shape,
            time=time,
            dimension=dimension,
        ),
        catalog,
    )
    cases.append(
        {
            "query_spec": spec,
            "family_id": (
                f"{result_shape}|{'-'.join(metric_ids)}|{dimension or '-'}|"
                f"{time.mode}:{time.start or '-'}:{time.end_exclusive or '-'}:{time.grain or '-'}"
            ),
        }
    )


def build_cases() -> list[dict[str, Any]]:
    """Enumerate 206 unique, executable TheLook final-test plans."""
    cases: list[dict[str, Any]] = []
    catalog = CatalogLoader(THELOOK_WORKSPACE).load()
    windows = [TheLookQueryTime("all_time")] + [
        TheLookQueryTime("absolute_range", start, end) for start, end in _YEAR_WINDOWS
    ]

    for metrics in ((metric,) for metric in _METRICS):
        for query_time in windows:
            _append_case(cases, catalog, metric_ids=metrics, result_shape="scalar", time=query_time)
    for metrics in _MULTI_METRICS:
        for query_time in windows:
            _append_case(cases, catalog, metric_ids=metrics, result_shape="scalar", time=query_time)

    for dimension in _LOW_CARDINALITY_DIMENSIONS:
        allowed_metrics = (
            ("completed_sale_amount",)
            if dimension in {"category", "department"}
            else _METRICS
        )
        for metric in allowed_metrics:
            for query_time in windows:
                _append_case(
                    cases,
                    catalog,
                    metric_ids=(metric,),
                    result_shape="dimension_grouped",
                    dimension=dimension,
                    time=query_time,
                )
    # State has 210+ full-year groups after 2021.  Its first three yearly windows
    # remain below the analyst cap and cover the user geography join safely.
    for metric in _METRICS:
        for start, end in _STATE_WINDOWS:
            _append_case(
                cases,
                catalog,
                metric_ids=(metric,),
                result_shape="dimension_grouped",
                dimension="state",
                time=TheLookQueryTime("absolute_range", start, end),
            )

    for grain in ("month", "quarter"):
        for metric in _METRICS:
            for start, end in _YEAR_WINDOWS:
                _append_case(
                    cases,
                    catalog,
                    metric_ids=(metric,),
                    result_shape="time_series",
                    time=TheLookQueryTime("series", start, end, grain),
                )
    for metrics in _MULTI_METRICS:
        for start, end in _YEAR_WINDOWS:
            _append_case(
                cases,
                catalog,
                metric_ids=metrics,
                result_shape="time_series",
                time=TheLookQueryTime("series", start, end, "month"),
            )

    if len(cases) != EXPECTED_CASES:
        raise AssertionError(f"expected {EXPECTED_CASES} cases, got {len(cases)}")
    query_spec_ids = [item["query_spec"].query_spec_id for item in cases]
    families = [item["family_id"] for item in cases]
    if len(set(query_spec_ids)) != len(cases) or len(set(families)) != len(cases):
        raise AssertionError("TheLook evaluation families and QuerySpecs must be unique")
    return cases


def _execute_as_reader(sql: str) -> pd.DataFrame:
    """Run through the reader role without using the Olist audit writer role."""
    connection = psycopg2.connect(
        host="/tmp", port=35434, database="thelook_analytics", user="postgres"
    )
    try:
        connection.set_session(readonly=True, autocommit=False)
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute("SET LOCAL ROLE daa_thelook_reader")
            cursor.execute("SET LOCAL statement_timeout = 5000")
            cursor.execute(sql)
            rows = [dict(row) for row in cursor.fetchmany(200)]
    finally:
        connection.close()
    return pd.DataFrame(rows)


def _validate_execution(
    spec: TheLookQuerySpec,
    frame: pd.DataFrame,
    metric_constraints: dict[str, dict[str, float | bool]],
) -> dict[str, Any]:
    validation = ResultValidator(max_rows=200).validate(
        frame,
        required_columns=spec.required_result_columns,
        metric_columns=spec.metric_ids,
        time_column="time" if spec.result_shape == "time_series" else None,
        time_bucket_grain=spec.time.grain,
        requested_start=spec.time.start,
        requested_end=spec.time.end_exclusive,
        exact_columns=True,
        metric_value_constraints=metric_constraints,
    )
    if not validation.safe_to_answer:
        raise TheLookEvaluationBuildError(
            f"{spec.query_spec_id} failed ResultValidator: {validation.reason}"
        )
    return validation.as_dict()


def materialize(
    output_dir: Path,
    *,
    generated_at: str | None = None,
    execute: Callable[[str], pd.DataFrame] = _execute_as_reader,
) -> dict[str, Any]:
    output_path = _external_new_dir(output_dir)
    cases = build_cases()
    if len(cases) > MAX_CASES:
        raise TheLookEvaluationBuildError("case count exceeds frozen safety bound")
    catalog = CatalogLoader(THELOOK_WORKSPACE).load()
    policy = SqlPolicy(workspace=THELOOK_WORKSPACE)
    records: list[dict[str, Any]] = []
    for index, case in enumerate(cases, 1):
        spec = validate_thelook_query_spec(case["query_spec"], catalog)
        artifact = render_thelook_gold_sql(spec, catalog)
        decision = policy.evaluate(artifact.sql)
        frame = execute(decision.final_sql)
        constraints = {
            metric: dict(catalog.metrics_by_id[metric].result_value_constraints)
            for metric in spec.metric_ids
        }
        validation = _validate_execution(spec, frame, constraints)
        records.append(
            {
                "case_id": f"thelook-final-v1-{index:03d}",
                "split": "cross_schema_final_test",
                "language": QUESTION_LANGUAGE,
                "question": _question(spec, index),
                "family_id": case["family_id"],
                "query_spec": spec.as_dict(),
                "gold_sql": artifact.sql,
                "gold_sql_sha256": artifact.sql_sha256,
                "renderer_version": artifact.renderer_version,
                "required_result_columns": list(spec.required_result_columns),
                "execution": {
                    "policy_status": decision.status,
                    "reader_role": THELOOK_WORKSPACE.reader_role,
                    "result_validation": validation,
                },
            }
        )

    generated_at = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = output_path.parent / f".{output_path.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        cases_path = staging / "cases.jsonl"
        with cases_path.open("x", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        shape_counts = Counter(record["query_spec"]["result_shape"] for record in records)
        manifest = {
            "evaluation_version": EVALUATION_VERSION,
            "generated_at": generated_at,
            "workspace": records[0]["query_spec"]["workspace"],
            "source": {
                "catalog_path": str(THELOOK_WORKSPACE.catalog_path.relative_to(ROOT)),
                "snapshot_manifest_sha256": sha256_file(
                    Path("/disk2/gengnan/data-analysis-agent-data/datasets/thelook/")
                    / "kaggle-mirror-v1-20260908/audit/snapshot_manifest.json"
                ),
            },
            "output": {"cases_jsonl": {"rows": len(records), "sha256": sha256_file(cases_path)}},
            "coverage": {
                "result_shapes": dict(sorted(shape_counts.items())),
                "metrics": sorted({metric for row in records for metric in row["query_spec"]["metric_ids"]}),
                "dimensions": sorted({row["query_spec"]["dimension"] for row in records if row["query_spec"]["dimension"]}),
                "high_cardinality_dimensions_excluded": ["city", "brand"],
                "state_windows_limited_to": ["2019", "2020", "2021-01-01/2021-07-01"],
            },
            "checks": {
                "all_cases_queryspec_renderer_policy_reader_result_contract": True,
                "case_ids_unique": len({row["case_id"] for row in records}) == len(records),
                "query_spec_ids_unique": len({row["query_spec"]["query_spec_id"] for row in records}) == len(records),
                "family_ids_unique": len({row["family_id"] for row in records}) == len(records),
                "olist_training_or_prompt_input": False,
                "base_or_adapter_generation_run": False,
            },
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(output_path)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def main() -> int:
    args = parse_args()
    print(json.dumps(materialize(args.output_dir, generated_at=args.generated_at), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
