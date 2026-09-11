#!/usr/bin/env python3
"""Turn the completed Qwen3.5-2B Base run into auditable pair evidence.

This is deliberately metadata-only: it verifies the already completed Base
generation report and hashes, writes no completions, starts no model, and does
not read Gold SQL or database rows.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src"
for directory in (ROOT, SOURCE_ROOT):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from data_analysis_agent.external_artifacts import ensure_path_outside_repository
from data_analysis_agent.olist_candidate_sql_evaluation import (
    CandidateEvaluationRecord,
    build_safe_report,
)
from data_analysis_agent.qwen35_thelook_v2_matching import (
    DECODE,
    MODEL_ID,
    MODEL_REVISION,
    PROMPT_TOKEN_PREFLIGHT,
    SERVER_PROMPT_BUNDLE_SHA256,
    WEIGHT_DTYPE,
    build_comparison_contract,
)
from data_analysis_agent.thelook_v2_matching import (
    TheLookV2MatchingError,
    read_generation_cases,
    read_raw_completions,
    sha256_file,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-jsonl", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--base-safe-report", type=Path, required=True)
    parser.add_argument("--base-completions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TheLookV2MatchingError(
            "Base safe report is unavailable or invalid"
        ) from exc
    if not isinstance(value, Mapping):
        raise TheLookV2MatchingError("Base safe report must be an object")
    return value


def _records(value: object) -> list[CandidateEvaluationRecord]:
    if not isinstance(value, list):
        raise TheLookV2MatchingError("Base safe report lacks records")
    try:
        return [
            CandidateEvaluationRecord(**dict(row))
            for row in value
            if isinstance(row, Mapping)
        ]
    except TypeError as exc:
        raise TheLookV2MatchingError("Base safe report record schema drifted") from exc


def main() -> int:
    args = parse_args()
    for path in (
        args.cases_jsonl,
        args.manifest,
        args.base_safe_report,
        args.base_completions,
    ):
        ensure_path_outside_repository(path, ROOT)
    output = ensure_path_outside_repository(args.output, ROOT)
    if output.exists():
        raise TheLookV2MatchingError("Base reuse output must be new")

    cases, manifest = read_generation_cases(args.cases_jsonl, args.manifest)
    source = _read_json(args.base_safe_report)
    evaluation = source.get("evaluation")
    model = source.get("model")
    if not isinstance(evaluation, Mapping) or not isinstance(model, Mapping):
        raise TheLookV2MatchingError("Base report lacks evaluation or model metadata")
    if (
        evaluation.get("dataset") != manifest["evaluation_version"]
        or evaluation.get("case_count") != len(cases)
        or evaluation.get("cases_jsonl_sha256") != sha256_file(args.cases_jsonl)
        or evaluation.get("manifest_sha256") != sha256_file(args.manifest)
        or evaluation.get("server_prompt_bundle_sha256") != SERVER_PROMPT_BUNDLE_SHA256
        or evaluation.get("qwen35_chat_template") is not True
        or evaluation.get("thinking_enabled") is not False
        or source.get("decode") != DECODE
        or source.get("prompt_token_preflight") != PROMPT_TOKEN_PREFLIGHT
        or model.get("id") != MODEL_ID
        or model.get("revision") != MODEL_REVISION
        or model.get("weight_dtype") != WEIGHT_DTYPE
        or model.get("adapter") != {"enabled": False}
    ):
        raise TheLookV2MatchingError(
            "existing Base evidence is not a matching Qwen3.5-2B run"
        )
    expected_ids = [case.case_id for case in cases]
    raw = read_raw_completions(
        args.base_completions, expected_case_ids=expected_ids, label="Base completions"
    )
    artifacts = source.get("raw_artifacts")
    if (
        not isinstance(artifacts, Mapping)
        or artifacts.get("raw_completions_sha256") != sha256_file(args.base_completions)
        or artifacts.get("raw_completions_outside_repository") is not True
        or len(raw) != len(cases)
    ):
        raise TheLookV2MatchingError("existing Base raw completion evidence drifted")
    records = _records(source.get("records"))
    if [record.source_id for record in records] != expected_ids or any(
        record.generation_status != "generated" for record in records
    ):
        raise TheLookV2MatchingError(
            "existing Base generation is incomplete or reordered"
        )

    report = build_safe_report(
        report_metadata={
            "report_schema_version": "qwen35-2b-thelook-v2-base-reuse-safe-report-v1",
            "run_label": "base",
            "comparison_contract": build_comparison_contract(
                cases_sha256=sha256_file(args.cases_jsonl),
                manifest_sha256=sha256_file(args.manifest),
                workspace=manifest["workspace"],
            ),
            "model": dict(model),
            "raw_artifacts": dict(artifacts),
            "boundaries": {
                "gold_sql_read_for_generation": False,
                "database_rows_read_for_generation": False,
                "production_default_unchanged": True,
                "raw_completion_in_repository": False,
            },
            "source_base_safe_report_sha256": sha256_file(args.base_safe_report),
            "reused_generation": True,
        },
        records=records,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"reused_cases": len(records), "output": str(output)}, ensure_ascii=False
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TheLookV2MatchingError, ValueError) as exc:
        print(f"Qwen3.5 Base reuse error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
