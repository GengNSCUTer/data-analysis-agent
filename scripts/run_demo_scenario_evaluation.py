#!/usr/bin/env python3
"""Validate fixed interview-demo scenario contracts and optional golden data."""

from __future__ import annotations

import argparse
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FIELDS = frozenset(
    {
        "id", "label", "question", "allowed_roles", "metrics", "required_tables",
        "expected_shape", "expected_chart", "expected_row_limit", "result_columns",
        "evidence", "semantics",
    }
)


def run(case_file: Path, verify_database: bool) -> dict:
    suite = yaml.safe_load(case_file.read_text(encoding="utf-8"))
    scenarios = suite["scenarios"]
    identifiers = [scenario["id"] for scenario in scenarios]
    missing_fields = {
        scenario["id"]: sorted(REQUIRED_FIELDS - set(scenario))
        for scenario in scenarios
        if REQUIRED_FIELDS - set(scenario)
    }
    invalid_roles = {
        scenario["id"]: sorted(set(scenario["allowed_roles"]) - {"analyst", "admin"})
        for scenario in scenarios
        if set(scenario["allowed_roles"]) - {"analyst", "admin"}
    }
    invalid_charts = {
        scenario["id"]: scenario["expected_chart"]
        for scenario in scenarios
        if scenario["expected_chart"] not in {"bar", "none"}
    }
    database = {"executed": False}
    if verify_database:
        command = [
            "/disk2/gengnan/conda_envs/pg_runtime/bin/psql", "-p", "35434", "-U", "postgres",
            "-d", "data_analysis_agent", "-v", "ON_ERROR_STOP=1", "-f",
            str(ROOT / "evals/sql/verify_demo_scenarios.sql"),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        database = {
            "executed": True,
            "passed": completed.returncode == 0,
            "output": completed.stdout.strip(),
            "error": completed.stderr.strip(),
        }
    return {
        "suite_version": suite["version"],
        "dataset": suite["dataset"],
        "scenario_count": len(scenarios),
        "categories": dict(Counter(scenario["expected_shape"] for scenario in scenarios)),
        "unique_ids": len(identifiers) == len(set(identifiers)),
        "missing_fields": missing_fields,
        "invalid_roles": invalid_roles,
        "invalid_charts": invalid_charts,
        "database_golden": database,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "scope_note": suite["scope_note"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", action="store_true", help="verify results against local PostgreSQL")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(ROOT / "evals/cases/demo_scenarios.yaml", args.database)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(yaml.safe_dump(result, allow_unicode=True, sort_keys=False), encoding="utf-8")
    valid = (
        result["scenario_count"] == 3
        and result["unique_ids"]
        and not result["missing_fields"]
        and not result["invalid_roles"]
        and not result["invalid_charts"]
        and (not args.database or result["database_golden"].get("passed"))
    )
    if not valid:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
