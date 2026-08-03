#!/usr/bin/env python3
"""Run the deterministic first-round evaluation suite.

This intentionally does not claim LLM semantic accuracy. It validates the
versioned case inventory, AST policy behavior, and optional PostgreSQL golden
metrics; live-model evidence is reported separately.
"""

from __future__ import annotations

import argparse
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

from data_analysis_agent.sql_policy import PolicyViolation, SqlPolicy


ROOT = Path(__file__).resolve().parents[1]


def run(case_file: Path, verify_database: bool) -> dict:
    suite = yaml.safe_load(case_file.read_text(encoding="utf-8"))
    cases = suite["cases"]
    identifiers = [case["id"] for case in cases]
    policy = SqlPolicy()
    safety_results = []
    for case in (item for item in cases if item["category"] == "safety"):
        try:
            decision = policy.evaluate(case["sql"], role="analyst")
            actual = "allowed_capped" if decision.row_limit == 200 and "999999" in case["sql"] else "allowed"
        except PolicyViolation:
            actual = "rejected"
        safety_results.append({"id": case["id"], "expected": case["expected_shape"], "actual": actual, "passed": actual == case["expected_shape"]})
    database = {"executed": False}
    if verify_database:
        command = ["/disk2/gengnan/conda_envs/pg_runtime/bin/psql", "-p", "35434", "-U", "postgres", "-d", "data_analysis_agent", "-v", "ON_ERROR_STOP=1", "-f", str(ROOT / "evals/sql/verify_olist_golden.sql")]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        database = {"executed": True, "passed": completed.returncode == 0, "output": completed.stdout.strip(), "error": completed.stderr.strip()}
    return {
        "suite_version": suite["version"], "dataset": suite["dataset"], "metric_version": suite["metric_version"],
        "executed_at": datetime.now(timezone.utc).isoformat(), "total_cases": len(cases),
        "categories": dict(Counter(case["category"] for case in cases)), "unique_ids": len(identifiers) == len(set(identifiers)),
        "safety": {"total": len(safety_results), "passed": sum(item["passed"] for item in safety_results), "results": safety_results},
        "database_golden": database,
        "scope_note": "Deterministic coverage validates suite structure, policy behavior, and optional golden SQL. It does not measure live LLM semantic accuracy.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", action="store_true", help="run local PostgreSQL golden SQL")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(ROOT / "evals/cases/v1.yaml", args.database)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(yaml.safe_dump(result, allow_unicode=True, sort_keys=False), encoding="utf-8")
    failures = result["safety"]["total"] - result["safety"]["passed"]
    if not result["unique_ids"] or failures or (args.database and not result["database_golden"].get("passed")):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
