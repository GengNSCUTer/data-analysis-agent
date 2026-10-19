#!/usr/bin/env python3
"""Review saved product SSE runs; do not call a model or re-run candidate SQL.

Reference queries below are independently written from business definitions,
not compiled from runtime QueryPlans or the training Gold renderer. Compare
against the original verified CSV artifacts. Raw data remains outside Git.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

import pandas as pd
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from data_analysis_agent.postgres_runner import PostgresConnectionSettings
from data_analysis_agent.result_artifact_store import ResultArtifactStore
from run_product_capability_online import _safe_event_summary
from run_product_release_dev import _actual, _compare, load_suite

# scope = (metrics, dimension, calendar grain, start, exclusive end, top N).
# These expectations come from each question, never the model's plan.
SCOPES = {
    "scalar_gmv": (("gmv",), None, None, None, None, None),
    "scalar_paid_orders": (("paid_order_count",), None, None, None, None, None),
    "scalar_delivery": (("average_delivery_days",), None, None, None, None, None),
    "state_gmv_top10": (("gmv",), "customer_state", None, None, None, 10),
    "category_orders_top10": (("paid_order_count",), "product_category_name", None, None, None, 10),
    "monthly_gmv_2017": (("gmv",), None, "month", "2017-01-01", "2018-01-01", None),
    "quarterly_orders_2017": (("paid_order_count",), None, "quarter", "2017-01-01", "2018-01-01", None),
    "overview_four_metrics": (("gmv", "paid_order_count", "average_delivery_days", "positive_review_rate"), None, None, None, None, None),
    "state_aov": (("average_order_value",), "customer_state", None, None, None, None),
    "compare_gmv_years": (("gmv",), None, "year", "2017-01-01", "2019-01-01", None),
    "compare_orders_years": (("paid_order_count",), None, "year", "2017-01-01", "2019-01-01", None),
    "compare_half_year_orders": (("paid_order_count",), None, "half_year", "2017-01-01", "2018-01-01", None),
    "monthly_review_rate": (("positive_review_rate",), None, "month", None, None, None),
    "monthly_multi_metric_trend": (("gmv", "paid_order_count"), None, "month", "2017-01-01", "2018-01-01", None),
    "state_delivery_comparison": (("average_delivery_days", "on_time_delivery_rate"), "customer_state", None, None, None, None),
    "chart_category_orders": (("paid_order_count",), "product_category_name", None, None, None, None),
    "chart_state_delivery": (("average_delivery_days",), "customer_state", None, None, None, None),
    "export_state_gmv": (("gmv",), "customer_state", None, "2017-01-01", "2018-01-01", None),
}
for alias, target in {
    "chart_state_gmv": "state_gmv_top10", "chart_month_gmv": "monthly_gmv_2017",
    "monthly_gmv_trend": "monthly_gmv_2017", "quarterly_orders_trend": "quarterly_orders_2017",
    "chart_quarter_orders": "quarterly_orders_2017", "chart_month_multi": "monthly_multi_metric_trend",
}.items():
    SCOPES[alias] = SCOPES[target]


def reference_sql(scope: tuple) -> str:
    metrics, dimension, grain, start, end, top_n = scope
    branches = []
    keys = ([dimension] if dimension else []) + (["time"] if grain else [])
    for metric in metrics:
        joins = ""
        predicate = "o.order_status NOT IN ('canceled','unavailable')"
        time_column = "o.order_purchase_timestamp"
        if metric == "gmv":
            expression = "SUM(i.price)"
            joins += " JOIN analytics.fact_order_items i ON i.order_id=o.order_id"
        elif metric == "paid_order_count":
            expression = "COUNT(DISTINCT o.order_id)"
        elif metric == "average_delivery_days":
            expression = "AVG(EXTRACT(EPOCH FROM (o.order_delivered_customer_date-o.order_purchase_timestamp))/86400.0)"
            predicate = "o.order_delivered_customer_date IS NOT NULL AND o.order_purchase_timestamp IS NOT NULL"
        elif metric == "positive_review_rate":
            expression = "AVG(CASE WHEN r.review_score>=4 THEN 1.0 ELSE 0.0 END)"
            joins += " JOIN analytics.fact_reviews r ON r.order_id=o.order_id"
            predicate, time_column = "r.review_score BETWEEN 1 AND 5", "r.review_creation_date"
        elif metric == "on_time_delivery_rate":
            expression = "AVG(CASE WHEN o.order_delivered_customer_date<=o.order_estimated_delivery_date THEN 1.0 ELSE 0.0 END)"
            predicate = "o.order_status='delivered' AND o.order_purchase_timestamp IS NOT NULL AND o.order_delivered_customer_date IS NOT NULL AND o.order_estimated_delivery_date IS NOT NULL"
        elif metric == "average_order_value":
            joins += " JOIN (SELECT order_id, SUM(price) AS order_total FROM analytics.fact_order_items GROUP BY order_id) totals ON totals.order_id=o.order_id"
            expression = "AVG(totals.order_total)"
        else:
            raise ValueError(metric)
        groups = []
        if dimension == "customer_state":
            joins += " JOIN analytics.dim_customers c ON c.customer_id=o.customer_id"
            groups.append("c.customer_state AS customer_state")
        elif dimension == "product_category_name":
            # Count distinct orders per category, never multiply order counts
            # by its items. Keep the NULL category instead of dropping it.
            joins += " JOIN (SELECT DISTINCT i.order_id, COALESCE(t.product_category_name_english,p.product_category_name) AS product_category_name FROM analytics.fact_order_items i JOIN analytics.dim_products p ON p.product_id=i.product_id LEFT JOIN analytics.dim_category_translation t ON t.product_category_name=p.product_category_name) cats ON cats.order_id=o.order_id"
            groups.append("cats.product_category_name AS product_category_name")
        if grain:
            time_expression = (
                f"DATE_TRUNC('year',{time_column}) + CASE WHEN EXTRACT(MONTH FROM {time_column})>=7 THEN INTERVAL '6 months' ELSE INTERVAL '0 months' END"
                if grain == "half_year" else f"DATE_TRUNC('{grain}',{time_column})"
            )
            groups.append(f"{time_expression} AS time")
        if start:
            predicate += f" AND {time_column}>='{start}' AND {time_column}<'{end}'"
        grouping = " GROUP BY " + ",".join(str(i + 1) for i in range(len(groups))) if groups else ""
        branches.append(f"SELECT {','.join([*groups, expression + ' AS ' + metric])} FROM analytics.fact_orders o{joins} WHERE {predicate}{grouping}")
    ctes = ",".join(f"m{i} AS ({sql})" for i, sql in enumerate(branches))
    source = "m0"
    for i in range(1, len(branches)):
        source += f" FULL OUTER JOIN m{i} USING ({','.join(keys)})" if keys else f" CROSS JOIN m{i}"
    columns = ",".join([*keys, *metrics])
    result = f"WITH {ctes} SELECT {columns} FROM {source}"
    if top_n:
        result += f" ORDER BY {metrics[0]} DESC LIMIT {top_n}"
    return result


def compare_frames(expected: pd.DataFrame, actual: pd.DataFrame, metrics: tuple[str, ...]) -> str:
    if set(expected.columns) != set(actual.columns):
        return "column_mismatch"
    actual = actual.loc[:, expected.columns].copy()
    for name in metrics:
        expected[name] = pd.to_numeric(expected[name])
        actual[name] = pd.to_numeric(actual[name])
    if "time" in expected:
        for frame in (expected, actual):
            if pd.api.types.is_numeric_dtype(frame["time"]):
                frame["time"] = pd.to_datetime(frame["time"].astype(int).astype(str), format="%Y")
            else:
                frame["time"] = pd.to_datetime(frame["time"])
    keys = [name for name in expected if name not in metrics]
    if keys:
        expected = expected.sort_values(keys, na_position="last")
        actual = actual.sort_values(keys, na_position="last")
    try:
        pd.testing.assert_frame_equal(expected.reset_index(drop=True), actual.reset_index(drop=True),
                                      check_dtype=False, rtol=1e-6, atol=1e-6)
    except AssertionError:
        return "denotation_mismatch"
    return "match"


def review(raw_roots: list[Path], output: Path, artifact_root: Path) -> dict:
    settings = PostgresConnectionSettings.from_environment()
    store = ResultArtifactStore(artifact_root)
    app = psycopg2.connect(host=settings.host, port=settings.port, database=settings.database, user=settings.writer_user)
    reader = psycopg2.connect(host=settings.host, port=settings.port, database=settings.database, user=settings.reader_user)
    reader.set_session(readonly=True)
    records = []
    try:
        with reader.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout='5s'")
        for case in load_suite(ROOT / "evals/cases/product_release_dev_v1.yaml"):
            steps = case.get("steps") or [{"step_id": "turn-1", "expected": case["expected"]}]
            details = []
            for step in steps:
                name = f"{case['case_id']}-{step['step_id']}.json"
                raw_path = next((root / name for root in reversed(raw_roots) if (root / name).exists()), None)
                if raw_path is None:
                    raise FileNotFoundError(name)
                raw = json.loads(raw_path.read_text())
                with app.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SELECT *, EXTRACT(EPOCH FROM (finished_at-started_at))*1000 AS latency_ms FROM app.agent_runs WHERE request_id=%s", (raw["client"]["request_id"],))
                    run = dict(cur.fetchone())
                trace = run["catalog_trace"]
                runtime = {**run, **_safe_event_summary(raw["events"])}
                actual = _actual(step["expected"], runtime, trace)
                mismatches = _compare(step["expected"], actual)
                check_id = step["expected"].get("check_id")
                numeric = "not_selected"
                artifact = trace.get("result_artifact") or {}
                if check_id in SCOPES:
                    numeric = "no_validated_artifact"
                    if artifact.get("artifact_id"):
                        path = store.verified_csv_path(artifact_id=artifact["artifact_id"], user_id=run["user_id"], workspace_id="olist-demo")
                        observed = pd.read_csv(path, keep_default_na=True)
                        with reader.cursor() as cur:
                            cur.execute(reference_sql(SCOPES[check_id]))
                            expected = pd.DataFrame(cur.fetchall(), columns=[col.name for col in cur.description])
                        numeric = compare_frames(expected, observed, SCOPES[check_id][0])
                    if numeric != "match":
                        mismatches.append("independent_numeric_check")
                details.append({"step_id": step["step_id"], "actual": actual, "mismatches": sorted(set(mismatches)),
                                "numeric": numeric, "latency_ms": round(float(run["latency_ms"] or 0)),
                                "source_run": raw_path.parent.parent.name})
            mismatches = sorted({m for detail in details for m in detail["mismatches"]})
            records.append({"case_id": case["case_id"], "category": case["category"], "passed": not mismatches,
                            "mismatches": mismatches, "steps": details})
    finally:
        app.close()
        reader.close()
    result = {"case_count": len(records), "passed": sum(r["passed"] for r in records), "records": records,
              "numeric_counts": dict(Counter(s["numeric"] for r in records for s in r["steps"])),
              "note": "Mixed original/targeted rerun evidence; numeric checks use original verified CSVs and independent SQL, not a new full run."}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, default=Path("/disk2/gengnan/data-analysis-agent-data/runtime/result-artifacts"))
    args = parser.parse_args()
    result = review(args.raw_root, args.output, args.artifact_root)
    print(json.dumps({key: result[key] for key in ("case_count", "passed", "numeric_counts")}, ensure_ascii=False))
