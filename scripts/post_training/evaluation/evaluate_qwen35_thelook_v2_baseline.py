#!/usr/bin/env python3
"""Execute one frozen Qwen3.5 TheLook v2 baseline after generation completes.

Generation and Gold evaluation are deliberately separate.  This command first
binds the external raw-completion file to its generation-safe report, and only
then reads protected Gold SQL to perform Policy, reader-role, ResultContract,
and denotation checks.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src"
# Direct ``python scripts/.../evaluate_qwen35_...py`` execution puts the
# script directory, rather than the repository root, on ``sys.path``.  This
# evaluator intentionally reuses the established TheLook evaluator helpers
# under ``scripts/``, so both the importable source tree and the repository
# package namespace must be available.  Keep this explicit instead of relying
# on a caller's working directory.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from data_analysis_agent.external_artifacts import ensure_path_outside_repository
from data_analysis_agent.olist_candidate_sql_evaluation import build_safe_report
from data_analysis_agent.sql_policy import SqlPolicy
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE
from data_analysis_agent.thelook_v2_matching import (
    EXPECTED_CASES,
    TheLookV2MatchingError,
    read_generation_cases,
    read_raw_completions,
    sha256_file,
)
from scripts.post_training.evaluation.evaluate_thelook_v2_matching_outputs import (
    CandidateOutcome,
    _candidate_outcome,
    _execution_report,
    _gold_frame,
    _load_full_cases_after_marker,
    _normalised_rows,
    denotation_state,
)
from scripts.post_training.evaluation.run_qwen35_thelook_v2_baseline_generation import (
    RUNNER_VERSION,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-jsonl", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--safe-report", type=Path, required=True)
    parser.add_argument("--completions", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TheLookV2MatchingError(f"{label} is unavailable or invalid") from exc
    if not isinstance(value, Mapping):
        raise TheLookV2MatchingError(f"{label} must be an object")
    return value


def _verify_generation(
    report: Mapping[str, Any],
    *,
    model_dir: Path,
    completions: Path,
    expected_case_ids: Sequence[str],
    cases_jsonl: Path,
    manifest: Path,
) -> None:
    """Fail closed before Gold access if external generation evidence drifts."""

    try:
        model = _read_json(model_dir / "download_manifest.json", "model manifest")
    except TheLookV2MatchingError as exc:
        raise TheLookV2MatchingError("cannot verify Qwen3.5 model identity") from exc
    evaluation = report.get("evaluation")
    report_model = report.get("model")
    decode = report.get("decode")
    raw = report.get("raw_artifacts")
    boundaries = report.get("boundaries")
    records = report.get("records")
    if not all(
        isinstance(value, Mapping)
        for value in (evaluation, report_model, decode, raw, boundaries)
    ) or not isinstance(records, list):
        raise TheLookV2MatchingError("generation safe report lacks required evidence")
    if (
        report.get("report_schema_version")
        != "qwen35-thelook-v2-generation-safe-report-v1"
        or report.get("runner_version") != RUNNER_VERSION
        or evaluation.get("case_count") != EXPECTED_CASES
        or evaluation.get("cases_jsonl_sha256") != sha256_file(cases_jsonl)
        or evaluation.get("manifest_sha256") != sha256_file(manifest)
        or evaluation.get("qwen35_chat_template") is not True
        or evaluation.get("thinking_enabled") is not False
        or report_model.get("id") != model.get("model_id")
        or report_model.get("revision") != model.get("revision")
        or report_model.get("adapter") != {"enabled": False}
        or report_model.get("download_manifest_sha256")
        != sha256_file(model_dir / "download_manifest.json")
        or decode.get("do_sample") is not False
        or decode.get("num_beams") != 1
        or raw.get("raw_completions_sha256") != sha256_file(completions)
        or boundaries.get("gold_sql_read_for_generation") is not False
        or boundaries.get("database_rows_read_for_generation") is not False
        or boundaries.get("production_default_unchanged") is not True
    ):
        raise TheLookV2MatchingError("generation report differs from frozen inputs")
    ids = [row.get("source_id") for row in records if isinstance(row, Mapping)]
    if (
        ids != list(expected_case_ids)
        or len(ids) != EXPECTED_CASES
        or any(
            not isinstance(row, Mapping) or row.get("generation_status") != "generated"
            for row in records
        )
    ):
        raise TheLookV2MatchingError("generation did not complete the exact protected case set")


def _report(outcomes: Sequence[CandidateOutcome], denotation: Mapping[str, str]) -> Mapping[str, Any]:
    execution = _execution_report("qwen35_baseline", outcomes)
    return {
        "report_schema_version": "qwen35-thelook-v2-baseline-evaluation-v1",
        "dataset": "thelook-cross-schema-final-test-v2",
        "case_count": EXPECTED_CASES,
        "execution": execution,
        "denotation_summary": dict(sorted(Counter(denotation.values()).items())),
        "boundaries": {
            "gold_sql_read_only_after_generation_evidence": True,
            "production_default_unchanged": True,
            "raw_candidate_sql_in_repository": False,
            "raw_result_rows_in_repository": False,
        },
    }


def main() -> int:
    args = parse_args()
    for path in (
        args.cases_jsonl,
        args.manifest,
        args.model_dir,
        args.safe_report,
        args.completions,
    ):
        ensure_path_outside_repository(path, ROOT)
    output_dir = ensure_path_outside_repository(args.output_dir, ROOT)
    if output_dir.exists():
        raise TheLookV2MatchingError("baseline evaluation output directory must be new")

    generation_cases, _ = read_generation_cases(args.cases_jsonl, args.manifest)
    expected_ids = [case.case_id for case in generation_cases]
    report = _read_json(args.safe_report, "generation safe report")
    _verify_generation(
        report,
        model_dir=args.model_dir,
        completions=args.completions,
        expected_case_ids=expected_ids,
        cases_jsonl=args.cases_jsonl,
        manifest=args.manifest,
    )

    # Gold access begins only after the external generation evidence above is
    # complete and bound to the protected 600-case projection.
    cases = _load_full_cases_after_marker(
        args.cases_jsonl, expected_case_ids=expected_ids
    )
    completions = read_raw_completions(
        args.completions, expected_case_ids=expected_ids, label="Qwen3.5 completions"
    )
    policy = SqlPolicy(workspace=THELOOK_V2_WORKSPACE)
    outcomes = [
        _candidate_outcome(cases[case_id], completions[case_id], policy)
        for case_id in expected_ids
    ]
    gold_cache: dict[str, pd.DataFrame] = {}
    denotation: dict[str, str] = {}
    for case_id, outcome in zip(expected_ids, outcomes, strict=True):
        if not outcome.record.result_contract_satisfied or outcome.frame is None:
            denotation[case_id] = "not_result_contract_valid"
            continue
        gold = gold_cache.setdefault(case_id, _gold_frame(cases[case_id], policy))
        denotation[case_id] = denotation_state(outcome.frame, gold)

    output_dir.mkdir(parents=True)
    normalized_path = output_dir / "normalized-candidates.jsonl"
    with normalized_path.open("x", encoding="utf-8") as handle:
        for row in _normalised_rows(outcomes):
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    output = dict(_report(outcomes, denotation))
    output["raw_artifacts"] = {
        "normalized_candidates_sha256": sha256_file(normalized_path),
        "raw_artifacts_outside_repository": True,
    }
    (output_dir / "evaluation-report.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"execution": output["execution"], "denotation": output["denotation_summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TheLookV2MatchingError, ValueError) as exc:
        print(f"Qwen3.5 TheLook baseline evaluation error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
