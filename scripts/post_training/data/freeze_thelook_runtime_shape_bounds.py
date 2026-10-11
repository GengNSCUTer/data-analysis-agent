#!/usr/bin/env python3
"""Freeze aggregate shape bounds from the pinned, readonly TheLook snapshot.

Only cardinalities and daily unfiltered row counts are exported. No user IDs,
questions, holdout cases, Gold SQL or result values enter this planning profile.
"""

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

import psycopg2  # noqa: E402
from data_analysis_agent.thelook_context import THELOOK_DATASET_VERSION  # noqa: E402

DIMENSIONS = {
    "customer_state": (("users", "state"),),
    "customer_city": (("users", "city"),),
    "customer_traffic_source": (("users", "traffic_source"),),
    "product_category": (("products", "category"), ("inventory_items", "product_category")),
    "product_brand": (("products", "brand"), ("inventory_items", "product_brand")),
    "product_department": (("products", "department"), ("inventory_items", "product_department")),
    "distribution_center": (("distribution_centers", "name"),),
    "event_type": (("events", "event_type"),),
    "event_browser": (("events", "browser"),),
    "event_traffic_source": (("events", "traffic_source"),),
    "user_country": (("users", "country"),),
    "user_traffic_source": (("users", "traffic_source"),),
    "user_state": (("users", "state"),),
}
FACTS = {
    # LEFT JOIN bounds both order-level and item-level metrics, including an
    # order without items. Status filters can only reduce these row counts.
    "orders.created_at": (
        "SELECT o.created_at::date AS day, COUNT(*) AS n FROM analytics.orders o "
        "LEFT JOIN analytics.order_items i ON i.order_id=o.order_id "
        "WHERE o.created_at IS NOT NULL GROUP BY 1"
    ),
    **{f"{table}.{column}": (
        f"SELECT {column}::date AS day, COUNT(*) AS n FROM analytics.{table} "
        f"WHERE {column} IS NOT NULL GROUP BY 1"
    ) for table, column in (
        ("inventory_items", "created_at"), ("inventory_items", "sold_at"),
        ("events", "created_at"), ("users", "created_at"),
    )},
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-json", type=Path, required=True)
    args = parser.parse_args()
    output = {"version": "thelook-runtime-shape-bounds-v1", "dataset_version": THELOOK_DATASET_VERSION,
              "generated_at": datetime.now(timezone.utc).isoformat(), "timezone": "UTC",
              "reader_role": "daa_thelook_reader", "cardinalities": {}, "fact_date_bounds": {},
              "query_sha256": []}
    connection = psycopg2.connect(host="/tmp", port=35434, database="thelook_analytics", user="postgres")
    connection.set_session(readonly=True, isolation_level="REPEATABLE READ")
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE daa_thelook_reader")
            cursor.execute("SET LOCAL TIME ZONE 'UTC'")
            for dimension, sources in DIMENSIONS.items():
                union = " UNION ".join(f"SELECT DISTINCT {column} AS value FROM analytics.{table}"
                                       for table, column in sources)
                sql = f"SELECT COUNT(*) FROM ({union}) dimension_values"
                cursor.execute(sql)
                output["cardinalities"][dimension] = int(cursor.fetchone()[0])
                output["query_sha256"].append(hashlib.sha256(sql.encode()).hexdigest())
            for source, sql in FACTS.items():
                cursor.execute(sql)
                rows = dict(cursor.fetchall())
                if not rows:
                    raise ValueError("empty fact source cannot supply a frozen shape bound")
                first, last = min(rows), max(rows)
                if not isinstance(first, date):
                    raise ValueError("fact bucket is not a date")
                days = (last - first).days + 1
                output["fact_date_bounds"][source] = {
                    "start": first.isoformat(),
                    "counts": [int(rows.get(first + timedelta(days=i), 0)) for i in range(days)],
                }
                output["query_sha256"].append(hashlib.sha256(sql.encode()).hexdigest())
    finally:
        connection.close()
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    with args.output_json.open("x", encoding="utf-8") as handle:
        # Dense daily arrays keep this aggregate-only, reproducible metadata
        # compact enough to version alongside the Catalog.
        json.dump(output, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        handle.write("\n")
    print(json.dumps({"output": str(args.output_json), "cardinalities": output["cardinalities"],
                      "bytes": args.output_json.stat().st_size, "fact_sources": len(FACTS)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
