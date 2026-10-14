"""Exercise the production bounded runner on the isolated synthetic perf_shop."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
from time import perf_counter
from unittest.mock import Mock

import psycopg2

from data_analysis_agent.postgres_runner import PostgresConnectionSettings, SecurePostgresRunner
from data_analysis_agent.result_artifact_store import ResultArtifactStore
from data_analysis_agent.result_budget import ResultBudget
from data_analysis_agent.result_validator import ResultValidationError
from data_analysis_agent.workspace import WorkspaceProfile
from vanna.capabilities.sql_runner import RunSqlToolArgs
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory import DemoAgentMemory


def benchmark(output: Path) -> dict:
    settings = PostgresConnectionSettings(database="perf_shop", reader_user="perf_shop_benchmark_reader")
    # Only this disposable performance workspace gains a login. Business roles
    # and privileges are unchanged; trust authentication is local to the fixture.
    with psycopg2.connect(host=settings.host, port=settings.port, database="perf_shop", user="postgres") as connection:
        with connection.cursor() as cursor:
            cursor.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='perf_shop_benchmark_reader') THEN CREATE ROLE perf_shop_benchmark_reader LOGIN IN ROLE perf_shop_reader; END IF; END $$")
            cursor.execute("ALTER ROLE perf_shop_benchmark_reader SET default_transaction_read_only = on")
            cursor.execute("SELECT order_count FROM perf.dataset_meta")
            source_rows = cursor.fetchone()[0]
    workspace = WorkspaceProfile(
        workspace_id="perf-shop", dataset_id="synthetic-shop", dataset_version="perf-v1",
        metric_version="perf-v1", catalog_version="perf-v1", policy_version="perf-v1",
        analytics_schema="perf", reader_role=settings.reader_user,
        allowed_columns={"fact_orders": frozenset({"order_id", "customer_id", "order_ts", "order_amount", "order_status"})},
        analyst_tables=frozenset({"fact_orders"}),
    )
    runner = SecurePostgresRunner(settings=settings, workspace=workspace)
    # perf_shop has no application metadata schema. Query execution uses the
    # real reader role; audits are recorded in memory for this fixture only.
    runner.audit = Mock()
    full = ResultBudget.aggregate(execution_max_rows=100_000)
    group_sql = "SELECT customer_id, COUNT(*) AS order_count FROM fact_orders GROUP BY customer_id ORDER BY customer_id"
    checks = [
        ("million_row_scalar", "SELECT COUNT(*) AS order_count FROM fact_orders", full.for_result_rows(1), None, 1),
        ("daily_groups", "SELECT DATE_TRUNC('day', order_ts) AS time, COUNT(*) AS order_count FROM fact_orders GROUP BY 1 ORDER BY 1", full.for_result_rows(1095), None, 1095),
        ("complete_100k_groups", group_sql + " LIMIT 1000", full, None, 100_000),
        ("explicit_top_5", group_sql, full.for_result_rows(5), 5, 5),
        ("cardinality_overflow", group_sql, full.for_result_rows(1000), None, None),
        ("byte_overflow", group_sql, replace(full, result_max_bytes=1024), None, None),
    ]
    evidence = {"source_order_rows": source_rows, "reader_role": settings.reader_user, "audit_backend": "fixture-in-memory", "checks": []}
    store = ResultArtifactStore(output.parent / "artifacts")
    for name, sql, budget, top_n, expected in checks:
        envelope = budget.as_metadata()
        context = ToolContext(
            user=User(id="perf-runtime", group_memberships=["analyst"]),
            conversation_id="perf-runtime", request_id=name,
            agent_memory=DemoAgentMemory(),
            metadata={**envelope, "query_plan": {**envelope, "top_n": {"customer_id": top_n} if top_n else {}}, "metric_result_columns": ["order_count"]},
        )
        started = perf_counter()
        try:
            frame = runner._run_sql_sync(RunSqlToolArgs(sql=sql), context)
        except ResultValidationError as error:
            assert expected is None, error
            entry = {"name": name, "status": error.validation.state, "partial_result_returned": False}
        else:
            assert expected is not None and len(frame) == expected, (name, len(frame), expected)
            entry = {"name": name, "status": context.metadata["result_status"], "rows": len(frame), "result_bytes": context.metadata["result_bytes"], "summary_chars": len(context.metadata["validated_result_summary"])}
            assert entry["summary_chars"] <= 1200
            if name == "complete_100k_groups":
                scope = dict(artifact_id="complete-100k", user_id="perf-runtime", workspace_id="perf-shop")
                manifest = store.save_result_csv(**scope, columns=list(frame.columns), rows=frame.to_dict("records"), row_count=len(frame), dataset_version="perf-v1", metric_version="perf-v1")
                first = store.read_csv_page(**scope)
                last = store.read_csv_page(**scope, cursor=99_800)
                assert len(first["rows"]) == len(last["rows"]) == 200
                assert first["rows"][0]["customer_id"] == "1"
                assert last["rows"][-1]["customer_id"] == "100000" and not last["has_next_page"]
                entry.update(csv_bytes=manifest["csv_bytes"], csv_sha256=manifest["csv_sha256"], page_rows=200, last_customer_id=last["rows"][-1]["customer_id"])
        entry["elapsed_ms"] = round((perf_counter() - started) * 1000, 2)
        evidence["checks"].append(entry)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.output), ensure_ascii=False, indent=2))
