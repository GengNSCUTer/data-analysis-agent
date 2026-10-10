#!/usr/bin/env python3
"""Write the safe terminal summary for an Olist + TheLook matching evaluation.

This command deliberately accepts only post-generation safe reports.  It
requires two independent semantic stages: Olist's post-generation Gold audit
and TheLook's post-generation Gold/denotation evaluator.  A launcher must run
this command successfully before it creates its final ``completed`` marker.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from data_analysis_agent.external_artifacts import ensure_path_outside_repository


class MatchingSummaryError(ValueError):
    """The terminal evaluation evidence is incomplete or inconsistent."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--olist-safe-comparison", type=Path, required=True)
    parser.add_argument("--olist-gold-audit", type=Path, required=True)
    parser.add_argument("--thelook-evaluation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MatchingSummaryError(f"{label} is unavailable or invalid") from exc
    if not isinstance(value, dict):
        raise MatchingSummaryError(f"{label} must be an object")
    return value


def _require_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or value < 1:
        raise MatchingSummaryError(f"{label} must be a positive integer")
    return value


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MatchingSummaryError(f"{label} must be an object")
    return value


def build_summary(
    *,
    olist_comparison: Mapping[str, Any],
    olist_gold_audit: Mapping[str, Any],
    thelook_evaluation: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate terminal evidence and project it to a safe, compact summary."""

    olist_case_count = _require_int(olist_comparison.get("case_count"), "Olist comparison case_count")
    olist_contract = _require_mapping(olist_comparison.get("comparison_contract"), "Olist comparison contract")
    olist_base = _require_mapping(olist_comparison.get("base_summary"), "Olist Base summary")
    olist_adapter = _require_mapping(olist_comparison.get("adapter_summary"), "Olist Adapter summary")

    olist_scope = _require_mapping(olist_gold_audit.get("scope"), "Olist Gold audit scope")
    if olist_scope.get("mode") != "post_generation_olist_gold_denotation_audit":
        raise MatchingSummaryError("Olist Gold audit has an unexpected mode")
    if _require_int(olist_scope.get("case_count"), "Olist Gold audit case_count") != olist_case_count:
        raise MatchingSummaryError("Olist Gold audit case count differs from Olist comparison")
    olist_evidence = _require_mapping(olist_gold_audit.get("input_evidence"), "Olist Gold audit input evidence")
    if olist_evidence.get("comparison_contract") != olist_contract:
        raise MatchingSummaryError("Olist Gold audit does not bind the matching generation contract")
    olist_gold = _require_mapping(olist_gold_audit.get("summary"), "Olist Gold audit summary")

    thelook_case_count = _require_int(thelook_evaluation.get("case_count"), "TheLook evaluation case_count")
    if thelook_evaluation.get("report_schema_version") != "thelook-matching-evaluation-v1":
        raise MatchingSummaryError("TheLook evaluation has an unexpected report schema")
    thelook_base = _require_mapping(thelook_evaluation.get("base_summary"), "TheLook Base summary")
    thelook_adapter = _require_mapping(thelook_evaluation.get("adapter_summary"), "TheLook Adapter summary")
    transitions = _require_mapping(thelook_evaluation.get("ordered_match_transitions"), "TheLook ordered transitions")

    # Do not report a false global accuracy. Olist's contract-valid count and
    # Gold denotation count are separate fields; callers must preserve that
    # distinction in project prose and dashboards.
    return {
        "report_schema_version": "multi-schema-matching-terminal-summary-v1",
        "status": "complete",
        "boundaries": {
            "gold_sql_read_only_after_generation": True,
            "raw_sql_or_result_rows_in_summary": False,
            "olist_contract_valid_is_not_gold_accuracy": True,
        },
        "olist": {
            "case_count": olist_case_count,
            "dataset": olist_contract.get("dataset"),
            "runtime_governance": {"base": dict(olist_base), "adapter": dict(olist_adapter)},
            "gold_denotation": dict(olist_gold),
        },
        "thelook": {
            "case_count": thelook_case_count,
            "dataset": thelook_evaluation.get("dataset"),
            "gold_denotation": {"base": dict(thelook_base), "adapter": dict(thelook_adapter)},
            "ordered_match_transitions": dict(transitions),
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for path in (args.olist_safe_comparison, args.olist_gold_audit, args.thelook_evaluation):
        ensure_path_outside_repository(path, ROOT)
    output = ensure_path_outside_repository(args.output, ROOT)
    if output.exists():
        raise MatchingSummaryError("terminal summary output must be new")
    summary = build_summary(
        olist_comparison=_read_json(args.olist_safe_comparison, "Olist safe comparison"),
        olist_gold_audit=_read_json(args.olist_gold_audit, "Olist Gold audit"),
        thelook_evaluation=_read_json(args.thelook_evaluation, "TheLook evaluation"),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": summary["status"], "olist_cases": summary["olist"]["case_count"], "thelook_cases": summary["thelook"]["case_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MatchingSummaryError, ValueError) as exc:
        print(f"matching terminal summary error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
