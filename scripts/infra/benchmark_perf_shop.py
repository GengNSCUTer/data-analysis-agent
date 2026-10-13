"""Run bounded result-budget checks against the synthetic perf_shop workspace.

This benchmark measures database/result-shape behavior only. It does not invoke
an LLM and does not claim anything about Text-to-SQL semantic accuracy.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import time
from typing import Any

import psycopg2


QUERIES: tuple[tuple[str, str], ...] = (
    (
        "aggregate",
        "SELECT SUM(order_amount) AS gmv, COUNT(*) AS order_count "
        "FROM perf.fact_orders",
    ),
    (
        "time_series",
        "SELECT DATE_TRUNC('month', order_ts)::date AS month, COUNT(*) AS order_count "
        "FROM perf.fact_orders GROUP BY 1 ORDER BY 1",
    ),
    (
        "detail_page",
        "SELECT order_id, customer_id, order_ts, order_status, order_amount "
        "FROM perf.fact_orders ORDER BY order_id LIMIT 200",
    ),
    (
        "detail_keyset_page",
        "SELECT order_id, customer_id, order_ts, order_status, order_amount "
        "FROM perf.fact_orders WHERE order_id > 200 ORDER BY order_id LIMIT 200",
    ),
    (
        "high_cardinality_group",
        "SELECT customer_id, COUNT(*) AS order_count "
        "FROM perf.fact_orders GROUP BY customer_id ORDER BY customer_id",
    ),
)


def run_benchmark(
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    statement_timeout_ms: int,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    results: list[dict[str, Any]] = []
    with psycopg2.connect(
        host=host,
        port=port,
        database=database,
        user=user,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE perf_shop_reader")
            cursor.execute("SET LOCAL statement_timeout = %s", (statement_timeout_ms,))
            cursor.execute("SELECT current_user, current_database()")
            current_user, current_database = cursor.fetchone()
            for mode, sql in QUERIES:
                query_started = time.perf_counter()
                cursor.execute(sql)
                rows = cursor.fetchall()
                elapsed_ms = round((time.perf_counter() - query_started) * 1000, 3)
                columns = [description[0] for description in cursor.description or ()]
                json_bytes, csv_bytes = _serialized_sizes(columns, rows)
                check: dict[str, Any] = {
                    "mode": mode,
                    "row_count": len(rows),
                    "elapsed_ms": elapsed_ms,
                    "json_bytes": json_bytes,
                    "csv_bytes": csv_bytes,
                }
                if mode == "detail_page":
                    cursor.execute(
                        "SELECT order_id FROM perf.fact_orders "
                        "ORDER BY order_id LIMIT 201"
                    )
                    page_probe_count = len(cursor.fetchall())
                    check["preview_row_count"] = min(page_probe_count, 200)
                    check["has_next_page"] = page_probe_count > 200
                results.append(check)

            export_started = time.perf_counter()
            export_buffer = io.BytesIO()
            cursor.copy_expert(
                "COPY (SELECT customer_id, COUNT(*) AS order_count "
                "FROM perf.fact_orders GROUP BY customer_id ORDER BY customer_id) "
                "TO STDOUT WITH CSV HEADER",
                export_buffer,
            )
            export_elapsed_ms = round((time.perf_counter() - export_started) * 1000, 3)
            cursor.execute(
                "SELECT count(*) FROM (SELECT customer_id FROM perf.fact_orders "
                "GROUP BY customer_id) grouped"
            )
            export_rows = cursor.fetchone()[0]
            results.append(
                {
                    "mode": "export_simulation",
                    "row_count": int(export_rows),
                    "elapsed_ms": export_elapsed_ms,
                    "csv_bytes": export_buffer.tell(),
                    "artifact_written": False,
                }
            )

            explain: dict[str, Any] = {}
            for name, sql in (
                ("aggregate", "SELECT SUM(order_amount) FROM perf.fact_orders"),
                (
                    "high_cardinality_group",
                    "SELECT customer_id, COUNT(*) FROM perf.fact_orders GROUP BY customer_id",
                ),
            ):
                explain_started = time.perf_counter()
                cursor.execute("EXPLAIN (FORMAT JSON) " + sql)
                explain_payload = cursor.fetchone()[0][0]
                explain_elapsed_ms = round((time.perf_counter() - explain_started) * 1000, 3)
                plan = explain_payload.get("Plan", {})
                explain[name] = {
                    "total_cost": plan.get("Total Cost"),
                    "plan_rows": plan.get("Plan Rows"),
                    "node_type": plan.get("Node Type"),
                    "elapsed_ms": explain_elapsed_ms,
                }

    return {
        "workspace_id": "perf-shop",
        "database": current_database,
        "reader_role": current_user,
        "statement_timeout_ms": statement_timeout_ms,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "total_elapsed_ms": round((time.perf_counter() - started_at) * 1000, 3),
        "checks": results,
        "explain": explain,
    }


def _serialized_sizes(columns: list[str], rows: list[tuple[Any, ...]]) -> tuple[int, int]:
    """Estimate transport sizes without putting the payload in model context."""

    json_payload = json.dumps(
        [dict(zip(columns, row)) for row in rows],
        default=str,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    text_buffer = io.StringIO()
    writer = csv.writer(text_buffer, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return len(json_payload), len(text_buffer.getvalue().encode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="/tmp")
    parser.add_argument("--port", type=int, default=35434)
    parser.add_argument("--database", default="perf_shop")
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--statement-timeout-ms", type=int, default=30_000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.database != "perf_shop":
        raise SystemExit("refusing unexpected database; use perf_shop")
    if args.statement_timeout_ms <= 0:
        raise SystemExit("statement timeout must be positive")
    evidence = run_benchmark(
        host=args.host,
        port=args.port,
        database=args.database,
        user=args.user,
        statement_timeout_ms=args.statement_timeout_ms,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
