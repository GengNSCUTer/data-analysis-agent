"""Check an onboarding configuration and, optionally, actual PostgreSQL objects.

This is an operator diagnostic, not another online request gate. Business
formulas and join cardinalities still need independent queries and review.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv
import psycopg2
from psycopg2 import sql
from sqlglot import exp, parse_one

from data_analysis_agent.postgres_runner import PostgresConnectionSettings
from data_analysis_agent.runtime_workspace import load_runtime_workspace
from data_analysis_agent.semantic_catalog import CatalogLoader


def check_configuration(path: Path):
    runtime = load_runtime_workspace(path)
    profile = runtime.profile
    catalog = CatalogLoader(profile).load()
    issues = []
    unknown_additive = profile.additive_metric_ids - catalog.metrics_by_id.keys()
    if unknown_additive:
        issues.append(f"unknown additive metrics: {sorted(unknown_additive)}")
    for metric in catalog.metrics:
        for dimension in metric.allowed_dimensions:
            if dimension != "date" and dimension not in profile.dimension_bindings:
                issues.append(f"metric {metric.metric_id}: unbound dimension {dimension}")
    if profile.reader_role == profile.writer_role:
        issues.append("reader and metadata writer must be distinct roles")
    return runtime, catalog, {
        "workspace_id": profile.workspace_id,
        "tables": len(catalog.tables), "metrics": len(catalog.metrics),
        "dimensions": len(profile.dimension_bindings), "joins": len(catalog.joins),
        "issues": issues,
        "business_review_required": ["formula and denominator", "fact grain and join fanout", "time attribution and units"],
    }


def check_database(runtime, catalog, settings):
    """Read metadata with the same dedicated roles the application uses."""
    profile = runtime.profile
    issues, objects, joins = [], [], []
    for role, business_reader in ((profile.reader_role, True), (profile.writer_role, False)):
        connection = psycopg2.connect(host=settings.host, port=settings.port, dbname=settings.database, user=role, connect_timeout=10)
        try:
            connection.set_session(readonly=True)
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '10s'")
                cursor.execute("SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname=current_user")
                if any(cursor.fetchone()):
                    issues.append(f"{role}: application role has administrative privileges")
                for table in catalog.tables:
                    qualified = f"{profile.analytics_schema}.{table.physical_name}"
                    cursor.execute("SELECT has_table_privilege(c.oid,'SELECT'), has_table_privilege(c.oid,'INSERT,UPDATE,DELETE,TRUNCATE') FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s", (profile.analytics_schema, table.physical_name))
                    privileges = cursor.fetchone()
                    if privileges is None:
                        issues.append(f"missing database object {qualified}")
                        continue
                    can_read, can_write = privileges
                    if business_reader and not can_read:
                        issues.append(f"reader cannot select {qualified}")
                    if can_write or (not business_reader and can_read):
                        issues.append(f"{role}: unexpected business privileges on {qualified}")
                    if business_reader:
                        cursor.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema=%s AND table_name=%s", (profile.analytics_schema, table.physical_name))
                        actual = dict(cursor.fetchall())
                        missing = set(table.columns_by_name) - set(actual)
                        if missing:
                            issues.append(f"{qualified}: missing columns {sorted(missing)}")
                        objects.append({"table": qualified, "columns": actual})
                if business_reader and not issues:
                    for join in catalog.joins:
                        condition = parse_one(join.on, read="postgres")
                        if not isinstance(condition, exp.EQ) or not all(isinstance(node, exp.Column) for node in (condition.left, condition.right)):
                            joins.append({"join_id": join.join_id, "review": "manual: complex join condition"})
                            continue
                        columns = {node.table: node.name for node in (condition.left, condition.right)}
                        source_key, target_key = columns[join.from_table], columns[join.to_table]
                        source = sql.Identifier(profile.analytics_schema, join.from_table)
                        target = sql.Identifier(profile.analytics_schema, join.to_table)
                        cursor.execute(sql.SQL("SELECT COUNT(*) FROM (SELECT {key} FROM {table} WHERE {key} IS NOT NULL GROUP BY {key} HAVING COUNT(*)>1) duplicates").format(key=sql.Identifier(target_key), table=target))
                        duplicates = cursor.fetchone()[0]
                        cursor.execute(sql.SQL("SELECT COUNT(*) FROM {source} s WHERE s.{sk} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {target} t WHERE t.{tk}=s.{sk})").format(source=source, target=target, sk=sql.Identifier(source_key), tk=sql.Identifier(target_key)))
                        unmatched = cursor.fetchone()[0]
                        joins.append({"join_id": join.join_id, "duplicate_target_keys": duplicates, "unmatched_source_rows": unmatched})
                        if join.cardinality == "many_to_one" and duplicates:
                            issues.append(f"{join.join_id}: target key is not unique")
                cursor.execute("SELECT has_schema_privilege(oid,'USAGE') FROM pg_namespace WHERE nspname='app'")
                metadata_schema = cursor.fetchone()
                if metadata_schema is None:
                    issues.append("app schema missing: initialize application metadata first")
                elif bool(metadata_schema[0]) == business_reader:
                    issues.append(f"{role}: unexpected app schema access")
        finally:
            connection.close()
    return {"issues": issues, "objects": objects, "joins": joins}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--database", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.env_file:
        load_dotenv(args.env_file, override=True)
    runtime, catalog, report = check_configuration(args.workspace)
    if args.database:
        report["database"] = check_database(runtime, catalog, PostgresConnectionSettings.from_environment())
    report["status"] = "failed" if report["issues"] or report.get("database", {}).get("issues") else "passed"
    content = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    print(content)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
