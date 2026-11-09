"""Independent business references; never import runtime plans or renderers.

The specification comes from authored evaluation questions, not the agent's
interpretation. SQL is executed through a read-only reference connection. Full
rows and diagnostics belong in the external evaluation directory.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import io
import math
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILES = {
    "olist_metrics": "olist_online_metrics_v1.yaml",
    "thelook_metrics": "thelook_online_metrics_v1.yaml",
    "thelook_queries": "thelook_product_queries.yaml",
}
MONEY = {"gmv", "payment_amount", "freight_amount"}
COUNTS = {"paid_order_count", "total_order_count", "item_count", "completed_order_count",
          "completed_item_count", "event_count", "registered_user_count", "review_count",
          "unique_customer_count", "canceled_order_count", "cancelled_order_count",
          "returned_order_count", "delivered_order_count", "unavailable_order_count",
          "unique_session_count", "completed_customer_count", "received_inventory_unit_count",
          "sold_inventory_unit_count", "current_unsold_inventory_unit_count"}
DIMENSIONS = {"customer_state", "customer_city", "product_category_name", "product_category",
              "product_brand", "product_department", "distribution_center", "event_type",
              "event_browser", "event_traffic_source", "user_traffic_source", "user_country", "payment_type", "time"}


def source_case(name: str) -> dict:
    suite, case_id = name.split(":", 1)
    data = yaml.safe_load((ROOT / "evals/cases" / SOURCE_FILES[suite]).read_text())
    return next(item for item in data["cases"] if item["id"] == case_id)


def read_csv(text: str) -> pd.DataFrame:
    # NA is a real dimension value, not a null marker. No dtype guessing for keys.
    return pd.read_csv(io.StringIO(text), keep_default_na=False, na_values=[""], dtype=str)


def reference_sql(dataset: str, spec: dict) -> tuple[str, list]:
    """Compile only the small, authored reference recipes in this task suite."""
    metric = spec["metric"]
    group, grain = spec.get("group"), spec.get("grain")
    joins, where, params = [], [], []
    if dataset == "olist":
        source, timestamp = "analytics.fact_orders o", "o.order_purchase_timestamp"
        status = "o.order_status NOT IN ('canceled','unavailable')"
        expressions = {"gmv": "SUM(i.price)", "paid_order_count": "COUNT(DISTINCT o.order_id)",
            "total_order_count": "COUNT(DISTINCT o.order_id)", "item_count": "COUNT(*)",
            "payment_amount": "SUM(pay.payment_value)", "freight_amount": "SUM(i.freight_value)",
            "average_order_value": "AVG(ot.amount)",
            "average_delivery_days": "AVG(EXTRACT(EPOCH FROM (o.order_delivered_customer_date-o.order_purchase_timestamp))/86400.0)",
            "positive_review_rate": "AVG(CASE WHEN r.review_score>=4 THEN 1.0 ELSE 0.0 END)"}
        if metric in {"gmv", "item_count", "freight_amount"}:
            joins.append("JOIN analytics.fact_order_items i ON i.order_id=o.order_id")
        elif metric == "payment_amount":
            joins.append("JOIN analytics.fact_payments pay ON pay.order_id=o.order_id")
        elif metric == "average_order_value":
            joins.append("JOIN (SELECT order_id,SUM(price) amount FROM analytics.fact_order_items GROUP BY order_id) ot ON ot.order_id=o.order_id")
        elif metric == "average_delivery_days":
            status = "o.order_delivered_customer_date IS NOT NULL AND o.order_purchase_timestamp IS NOT NULL"
        elif metric == "positive_review_rate":
            joins.append("JOIN analytics.fact_reviews r ON r.order_id=o.order_id")
            status, timestamp = "r.review_score BETWEEN 1 AND 5", "r.review_creation_date"
        if group in {"customer_state", "customer_city"} or spec.get("state"):
            joins.append("JOIN analytics.dim_customers c ON c.customer_id=o.customer_id")
        dimensions = {"customer_state": "c.customer_state", "customer_city": "c.customer_city"}
        if group == "product_category_name":
            if metric not in {"gmv", "item_count", "freight_amount"}:
                raise ValueError("This reference recipe requires an item-grain metric")
            joins.extend(["JOIN analytics.dim_products p ON p.product_id=i.product_id",
                "LEFT JOIN analytics.dim_category_translation tr ON tr.product_category_name=p.product_category_name"])
            dimensions[group] = "COALESCE(tr.product_category_name_english,p.product_category_name)"
    elif dataset == "thelook":
        source, timestamp = "analytics.orders o", "o.created_at"
        status = "o.status='Complete'"
        expressions = {"completed_sale_amount": "SUM(i.sale_price)",
            "completed_order_count": "COUNT(DISTINCT o.order_id)", "total_order_count": "COUNT(DISTINCT o.order_id)",
            "completed_item_count": "COUNT(*)", "average_order_value": "AVG(ot.amount)"}
        if metric in {"completed_sale_amount", "completed_item_count"}:
            joins.append("JOIN analytics.order_items i ON i.order_id=o.order_id")
        elif metric == "average_order_value":
            joins.append("JOIN (SELECT order_id,SUM(sale_price) amount FROM analytics.order_items GROUP BY order_id) ot ON ot.order_id=o.order_id")
        if group in {"customer_state", "customer_city"} or spec.get("state"):
            joins.append("JOIN analytics.users c ON c.id=o.user_id")
        dimensions = {"customer_state": "c.state", "customer_city": "c.city"}
        if group in {"product_category", "product_department", "product_brand"}:
            if metric not in {"completed_sale_amount", "completed_item_count"}:
                raise ValueError("This reference recipe requires an item-grain metric")
            joins.append("JOIN analytics.products p ON p.id=i.product_id")
            dimensions.update(product_category="p.category", product_department="p.department", product_brand="p.brand")
    else:
        raise ValueError(f"Unknown dataset: {dataset}")
    if metric != "total_order_count":
        where.append(status)
    if spec.get("start"):
        where.extend([f"{timestamp}>=%s", f"{timestamp}<%s"])
        params.extend([spec["start"], spec["end"]])
    if spec.get("state"):
        where.append(("c.customer_state" if dataset == "olist" else "c.state") + "=%s")
        params.append(spec["state"])
    fields = [f"{dimensions[group]} AS {group}"] if group else []
    if grain:
        if grain not in {"day", "week", "month", "quarter", "year", "half_year"}:
            raise ValueError(grain)
        bucket = (f"DATE_TRUNC('year',{timestamp}) + CASE WHEN EXTRACT(MONTH FROM {timestamp})>=7 THEN INTERVAL '6 months' ELSE INTERVAL '0 months' END"
            if grain == "half_year" else f"DATE_TRUNC('{grain}',{timestamp})")
        fields.append(f"{bucket} AS time")
    sql = f"SELECT {','.join([*fields, expressions[metric] + ' AS ' + metric])} FROM {source} {' '.join(joins)}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if fields:
        sql += " GROUP BY " + ",".join(str(i + 1) for i in range(len(fields)))
    if spec.get("partition_top_n"):
        sql = (f"WITH totals AS ({sql}), ranked AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY time ORDER BY {metric} DESC,{group}) AS position FROM totals) "
               f"SELECT {group},time,{metric} FROM ranked WHERE position<=%s ORDER BY time,{metric} DESC,{group}")
        params.append(spec["partition_top_n"])
    elif spec.get("top_n"):
        sql += f" ORDER BY {metric} DESC" + (f",{group}" if group else ",time") + " LIMIT %s"
        params.append(spec["top_n"])
    elif grain:
        sql += " ORDER BY time" + (f",{group}" if group else "")
    return sql, params


def query_frame(connection, statement: str, params=()) -> pd.DataFrame:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        return pd.DataFrame(cursor.fetchall(), columns=[col.name for col in cursor.description])


def reference_frame(connection, dataset: str, spec: dict) -> pd.DataFrame:
    if "source" in spec:
        return query_frame(connection, source_case(spec["source"])["reference_sql"])
    spec = dict(spec)
    if spec.get("state") == "BEST":
        best = reference_frame(connection, dataset, {"metric": "gmv" if dataset == "olist" else "completed_sale_amount",
            "group": "customer_state", "top_n": 1})
        spec["state"] = best.iloc[0]["customer_state"]
    sql, params = reference_sql(dataset, spec)
    return query_frame(connection, sql, params)


def numeric_equal(actual, expected, metric: str) -> bool:
    if pd.isna(actual) or pd.isna(expected):
        return bool(pd.isna(actual) and pd.isna(expected))
    try:
        a, b = Decimal(str(actual)), Decimal(str(expected))
        if not a.is_finite() or not b.is_finite():
            return False
        if metric in COUNTS:
            return a == b and a == a.to_integral_value()
        if metric in MONEY:
            return a.quantize(Decimal(".01"), rounding=ROUND_HALF_UP) == b.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        return math.isclose(float(a), float(b), rel_tol=1e-8, abs_tol=1e-6)
    except (ValueError, ArithmeticError):
        return False


def compare_frames(actual: pd.DataFrame, expected: pd.DataFrame, *, ordered=False) -> list[str]:
    if set(actual.columns) != set(expected.columns):
        return ["columns"]
    if len(actual) != len(expected):
        return ["row_count"]
    actual, expected = actual.loc[:, expected.columns].copy(), expected.copy()
    keys = [name for name in expected if name in DIMENSIONS]
    for name in keys:
        if name == "time":
            for frame in (actual, expected):
                # EXTRACT(YEAR) is a valid year bucket, including CSV '2017.0'.
                years = pd.to_numeric(frame[name], errors="coerce")
                values = (years.astype(int).astype(str) + "-01-01" if
                    len(years) and years.notna().all() and years.between(1000,9999).all()
                    and (years % 1 == 0).all() else frame[name])
                frame[name] = pd.to_datetime(values, utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        for frame in (actual, expected):
            frame[name] = frame[name].map(lambda v: "<SQL_NULL>" if pd.isna(v) else str(v))
    if not ordered and keys:
        actual, expected = [frame.sort_values(keys).reset_index(drop=True) for frame in (actual, expected)]
    else:
        actual, expected = actual.reset_index(drop=True), expected.reset_index(drop=True)
    errors = []
    for name in expected:
        if name in keys:
            if actual[name].tolist() != expected[name].tolist():
                errors.append("dimension:" + name)
        elif not all(numeric_equal(a, b, name) for a, b in zip(actual[name], expected[name])):
            errors.append("numeric:" + name)
    return errors


def expected_calculation(frame: pd.DataFrame, metric: str, operation: str, rows=None):
    values = [Decimal(str(v)) for v in frame[metric]]
    if operation == "sum":
        return sum(values)
    if operation == "compare":
        first, second = rows or (1, 2)
        return values[first - 1] - values[second - 1]
    raise ValueError(operation)
