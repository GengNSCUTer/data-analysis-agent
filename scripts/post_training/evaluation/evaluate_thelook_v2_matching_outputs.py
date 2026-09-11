#!/usr/bin/env python3
"""Execute a verified TheLook v2 Base/Adapter pair and audit Gold denotation.

This is intentionally the only v2 stage allowed to load ``gold_sql``.  It
first revalidates the completed matching-generation marker and both external
raw-completion hashes.  Candidate completion normalization is shared with the
runtime helper, then each candidate goes through the same AST policy, readonly
role, timeout, and ResultValidator used while materializing the final test.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd
import psycopg2
import psycopg2.extras
from sqlglot import exp, parse_one

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from data_analysis_agent.candidate_sql_generator import (
    CandidateSqlGenerationError,
    unwrap_sql_completion,
)
from data_analysis_agent.external_artifacts import ensure_path_outside_repository
from data_analysis_agent.olist_candidate_sql_evaluation import (
    CandidateEvaluationRecord,
    build_safe_comparison,
    build_safe_report,
)
from data_analysis_agent.result_validator import ResultValidator
from data_analysis_agent.semantic_catalog import CatalogLoader
from data_analysis_agent.sql_policy import PolicyViolation, SqlPolicy
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE
from data_analysis_agent.thelook_v2_matching import (
    EXPECTED_CASES,
    MATCHING_MARKER_VERSION,
    TheLookV2MatchingError,
    read_generation_cases,
    read_raw_completions,
    sha256_bytes,
    sha256_file,
    verify_matching_generation,
)
from data_analysis_agent.thelook_v2_queryspec import (
    TheLookV2QuerySpec,
    validate_thelook_v2_query_spec,
)


ABSOLUTE_NUMERIC_TOLERANCE = Decimal("0.000001")
RELATIVE_NUMERIC_TOLERANCE = Decimal("0.000001")


@dataclass(frozen=True)
class FullCase:
    case_id: str
    query_spec: TheLookV2QuerySpec
    gold_sql: str


@dataclass(frozen=True)
class CandidateOutcome:
    record: CandidateEvaluationRecord
    frame: pd.DataFrame | None
    normalized_sql: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-jsonl", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--base-safe-report", type=Path, required=True)
    parser.add_argument("--adapter-safe-report", type=Path, required=True)
    parser.add_argument("--base-completions", type=Path, required=True)
    parser.add_argument("--adapter-completions", type=Path, required=True)
    parser.add_argument("--matching-marker", type=Path, required=True)
    parser.add_argument(
        "--matching-profile",
        choices=("qwen25coder15b", "qwen35_2b", "qwen35_4b"),
        default="qwen25coder15b",
        help="Frozen Base/Adapter generation contract used to create the marker.",
    )
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


def _load_full_cases_after_marker(
    cases_jsonl: Path, *, expected_case_ids: Sequence[str]
) -> dict[str, FullCase]:
    """Load Gold only after the pair marker has already been validated."""

    catalog = CatalogLoader(THELOOK_V2_WORKSPACE).load()
    full_cases: dict[str, FullCase] = {}
    try:
        rows = [
            json.loads(line)
            for line in cases_jsonl.read_text(encoding="utf-8").splitlines()
            if line
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise TheLookV2MatchingError(
            "protected final test cannot be read for Gold audit"
        ) from exc
    for row_number, row in enumerate(rows, 1):
        if not isinstance(row, Mapping):
            raise TheLookV2MatchingError(f"Gold case {row_number} is invalid")
        case_id = row.get("case_id")
        gold_sql = row.get("gold_sql")
        query_spec = row.get("query_spec")
        if (
            not isinstance(case_id, str)
            or not isinstance(gold_sql, str)
            or not isinstance(query_spec, Mapping)
        ):
            raise TheLookV2MatchingError(
                f"Gold case {row_number} lacks required fields"
            )
        if case_id in full_cases:
            raise TheLookV2MatchingError("protected final test has duplicate Gold IDs")
        spec = validate_thelook_v2_query_spec(
            TheLookV2QuerySpec.from_mapping(query_spec), catalog
        )
        if row.get("gold_sql_sha256") != sha256_file_text(gold_sql):
            raise TheLookV2MatchingError(f"Gold SQL hash differs for {case_id}")
        full_cases[case_id] = FullCase(case_id, spec, gold_sql)
    if list(full_cases) != list(expected_case_ids) or len(full_cases) != EXPECTED_CASES:
        raise TheLookV2MatchingError(
            "Gold case IDs differ from verified generation pair"
        )
    return full_cases


def sha256_file_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _policy_cap_may_have_truncated_candidate(
    sql: str, *, policy: SqlPolicy, policy_limit_applied: bool
) -> bool:
    """Return whether Policy tightened a model-authored row limit.

    ``SqlPolicy`` adds the analyst's default LIMIT when a query has no LIMIT.
    That is a transport safety rail, not proof that the returned relation was
    truncated: a one-row scalar query still receives that syntactic LIMIT.
    Conversely, when the candidate itself asks for more rows than Policy
    allows, its intended result may have been shortened even if the first page
    happens to contain fewer rows.  Keep that case fail-safe for the result
    contract.

    This mirrors the runtime runner's distinction while retaining the extra
    evaluator guarantee for an explicitly over-budget candidate.  It is only
    called after ``policy.evaluate`` has parsed and accepted the SQL.
    """

    if not policy_limit_applied:
        return False
    statement = parse_one(sql, read=policy.sql_dialect)
    limit = statement.args.get("limit")
    if limit is None:
        # The limit was inserted by Policy, rather than requested by the
        # candidate.  ``len(frame) >= max_rows`` remains the truncation guard.
        return False
    value = limit.expression
    return isinstance(value, exp.Literal) and value.is_int and int(value.this) > policy.limits["analyst"]


def _execute_sql(
    sql: str, policy: SqlPolicy
) -> tuple[str, pd.DataFrame | None, bool, str | None]:
    """Policy-check and execute one candidate using the protected reader role."""

    try:
        decision = policy.evaluate(sql, role="analyst")
    except PolicyViolation:
        return "rejected", None, False, "policy_rejected"
    connection = psycopg2.connect(
        host="/tmp", port=35434, database="thelook_analytics", user="postgres"
    )
    try:
        connection.set_session(readonly=True, autocommit=False)
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute("SET LOCAL ROLE daa_thelook_reader")
            cursor.execute("SET LOCAL statement_timeout = 5000")
            cursor.execute(decision.final_sql)
            rows = [dict(row) for row in cursor.fetchmany(200)]
        return (
            "accepted",
            pd.DataFrame(rows),
            _policy_cap_may_have_truncated_candidate(
                sql,
                policy=policy,
                policy_limit_applied=decision.policy_limit_applied,
            ),
            None,
        )
    except Exception:
        connection.rollback()
        return (
            "accepted",
            None,
            _policy_cap_may_have_truncated_candidate(
                sql,
                policy=policy,
                policy_limit_applied=decision.policy_limit_applied,
            ),
            "postgres_execution_error",
        )
    finally:
        connection.close()


def _validate(
    frame: pd.DataFrame, spec: TheLookV2QuerySpec, *, limit_applied: bool
) -> tuple[str, bool]:
    catalog = CatalogLoader(THELOOK_V2_WORKSPACE).load()
    constraints = {
        metric_id: dict(catalog.metrics_by_id[metric_id].result_value_constraints)
        for metric_id in spec.metric_ids
    }
    validation = ResultValidator(max_rows=200).validate(
        frame,
        required_columns=spec.required_result_columns,
        metric_columns=spec.metric_ids,
        time_column="time" if spec.result_shape == "time_series" else None,
        time_bucket_grain=spec.time.grain,
        requested_start=spec.time.start,
        requested_end=spec.time.end_exclusive,
        limit_applied=limit_applied,
        exact_columns=True,
        metric_value_constraints=constraints,
    )
    return validation.state, validation.safe_to_answer


def _candidate_outcome(
    case: FullCase, completion: str, policy: SqlPolicy
) -> CandidateOutcome:
    try:
        normalized_sql = unwrap_sql_completion(completion)
    except CandidateSqlGenerationError:
        return CandidateOutcome(
            CandidateEvaluationRecord(
                case.case_id,
                "answerable",
                "generated",
                None,
                None,
                "not_run",
                "not_run",
                None,
                False,
                "candidate_normalization_error",
            ),
            None,
            None,
        )
    policy_status, frame, limit_applied, failure = _execute_sql(normalized_sql, policy)
    if frame is None:
        return CandidateOutcome(
            CandidateEvaluationRecord(
                case.case_id,
                "answerable",
                "generated",
                None,
                None,
                policy_status,
                "not_run" if policy_status == "rejected" else "error",
                None,
                False,
                failure,
            ),
            None,
            normalized_sql,
        )
    validation_state, valid = _validate(
        frame, case.query_spec, limit_applied=limit_applied
    )
    return CandidateOutcome(
        CandidateEvaluationRecord(
            case.case_id,
            "answerable",
            "generated",
            None,
            None,
            policy_status,
            "executed",
            validation_state,
            valid,
            None if valid else "result_contract_rejected",
        ),
        frame,
        normalized_sql,
    )


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return result if result.is_finite() else None
    return None


def _value_equal(left: Any, right: Any) -> bool:
    if pd.isna(left) or pd.isna(right):
        return bool(pd.isna(left)) and bool(pd.isna(right))
    left_decimal, right_decimal = _decimal(left), _decimal(right)
    if left_decimal is not None and right_decimal is not None:
        return abs(left_decimal - right_decimal) <= ABSOLUTE_NUMERIC_TOLERANCE + (
            RELATIVE_NUMERIC_TOLERANCE * max(abs(left_decimal), abs(right_decimal))
        )
    return str(left) == str(right)


def denotation_state(candidate: pd.DataFrame, gold: pd.DataFrame) -> str:
    """Compare exact columns, rows and values; permit bag equality only after order fails."""

    if tuple(map(str, candidate.columns)) != tuple(map(str, gold.columns)):
        return "column_mismatch"
    candidate_rows = list(candidate.itertuples(index=False, name=None))
    gold_rows = list(gold.itertuples(index=False, name=None))
    if len(candidate_rows) != len(gold_rows):
        return "row_count_mismatch"
    if all(
        all(
            _value_equal(left, right)
            for left, right in zip(candidate_row, gold_row, strict=True)
        )
        for candidate_row, gold_row in zip(candidate_rows, gold_rows, strict=True)
    ):
        return "ordered_denotation_match"

    # Values are serialized only into an in-memory comparison key.  The report
    # emits the resulting state, never result rows or a representation of SQL.
    def normalized_row(row: tuple[Any, ...]) -> tuple[str, ...]:
        return tuple(
            "<NULL>" if pd.isna(value) else str(_decimal(value) or value)
            for value in row
        )

    if Counter(map(normalized_row, candidate_rows)) == Counter(
        map(normalized_row, gold_rows)
    ):
        return "bag_denotation_match"
    return "denotation_mismatch"


def _gold_frame(case: FullCase, policy: SqlPolicy) -> pd.DataFrame:
    policy_status, frame, limit_applied, failure = _execute_sql(case.gold_sql, policy)
    if policy_status != "accepted" or frame is None or failure is not None:
        raise TheLookV2MatchingError(f"Gold SQL did not execute for {case.case_id}")
    state, valid = _validate(frame, case.query_spec, limit_applied=limit_applied)
    if not valid:
        raise TheLookV2MatchingError(
            f"Gold SQL failed ResultValidator for {case.case_id}: {state}"
        )
    return frame


def _execution_report(
    label: str, outcomes: Sequence[CandidateOutcome]
) -> Mapping[str, Any]:
    return build_safe_report(
        report_metadata={
            "report_schema_version": "thelook-v2-execution-safe-report-v1",
            "run_label": label,
            "comparison_contract": {
                "execution_contract": "thelook-v2-policy-reader-result-v1"
            },
            "boundaries": {
                "gold_sql_read_for_generation": False,
                "production_default_unchanged": True,
                "raw_candidate_sql_in_repository": False,
                "raw_result_rows_in_repository": False,
            },
        },
        records=[outcome.record for outcome in outcomes],
    )


def _normalised_rows(outcomes: Sequence[CandidateOutcome]) -> list[dict[str, str]]:
    return [
        {"case_id": outcome.record.source_id, "candidate_sql": outcome.normalized_sql}
        for outcome in outcomes
        if outcome.normalized_sql is not None
    ]


def main() -> int:
    args = parse_args()
    for path in (
        args.cases_jsonl,
        args.manifest,
        args.base_safe_report,
        args.adapter_safe_report,
        args.base_completions,
        args.adapter_completions,
        args.matching_marker,
    ):
        ensure_path_outside_repository(path, ROOT)
    output_dir = ensure_path_outside_repository(args.output_dir, ROOT)
    if output_dir.exists():
        raise TheLookV2MatchingError("evaluation output directory must be new")

    # Generation-safe projection and matching checks happen before this command
    # accesses the protected Gold field below.
    generation_cases, _ = read_generation_cases(args.cases_jsonl, args.manifest)
    expected_ids = [case.case_id for case in generation_cases]
    base_report = _read_json(args.base_safe_report, "base safe report")
    adapter_report = _read_json(args.adapter_safe_report, "adapter safe report")
    verifier = verify_matching_generation
    verifier_kwargs: dict[str, Any] = {}
    if args.matching_profile in {"qwen35_2b", "qwen35_4b"}:
        if args.matching_profile == "qwen35_4b":
            from data_analysis_agent.qwen35_4b_thelook_v2_matching import (
                verify_matching_generation as verify_qwen35_matching_generation,
            )
        else:
            from data_analysis_agent.qwen35_thelook_v2_matching import (
                verify_matching_generation as verify_qwen35_matching_generation,
            )

        verifier = verify_qwen35_matching_generation
        frozen_manifest = _read_json(args.manifest, "TheLook v2 manifest")
        workspace = frozen_manifest.get("workspace")
        if not isinstance(workspace, Mapping):
            raise TheLookV2MatchingError("TheLook v2 manifest lacks workspace pin")
        verifier_kwargs["workspace"] = workspace
    expected_marker = verifier(
        base_report=base_report,
        adapter_report=adapter_report,
        base_completions=args.base_completions,
        adapter_completions=args.adapter_completions,
        expected_case_ids=expected_ids,
        expected_cases_sha256=sha256_file(args.cases_jsonl),
        expected_manifest_sha256=sha256_file(args.manifest),
        **verifier_kwargs,
    )
    actual_marker = _read_json(args.matching_marker, "matching marker")
    if (
        actual_marker.get("marker_schema_version") != MATCHING_MARKER_VERSION
        or dict(actual_marker) != expected_marker
    ):
        raise TheLookV2MatchingError(
            "matching marker differs from current paired generation inputs"
        )

    # Gold access begins here, and only after the validated marker above.
    cases = _load_full_cases_after_marker(
        args.cases_jsonl, expected_case_ids=expected_ids
    )
    base_completions = read_raw_completions(
        args.base_completions, expected_case_ids=expected_ids, label="base completions"
    )
    adapter_completions = read_raw_completions(
        args.adapter_completions,
        expected_case_ids=expected_ids,
        label="adapter completions",
    )
    policy = SqlPolicy(workspace=THELOOK_V2_WORKSPACE)
    base_outcomes = [
        _candidate_outcome(cases[case_id], base_completions[case_id], policy)
        for case_id in expected_ids
    ]
    adapter_outcomes = [
        _candidate_outcome(cases[case_id], adapter_completions[case_id], policy)
        for case_id in expected_ids
    ]

    base_by_id = {outcome.record.source_id: outcome for outcome in base_outcomes}
    adapter_by_id = {outcome.record.source_id: outcome for outcome in adapter_outcomes}
    gold_cache: dict[str, pd.DataFrame] = {}
    denotation: dict[str, dict[str, str]] = {"base": {}, "adapter": {}}
    for case_id in expected_ids:
        for label, outcome in (
            ("base", base_by_id[case_id]),
            ("adapter", adapter_by_id[case_id]),
        ):
            if not outcome.record.result_contract_satisfied or outcome.frame is None:
                denotation[label][case_id] = "not_result_contract_valid"
                continue
            gold = gold_cache.setdefault(case_id, _gold_frame(cases[case_id], policy))
            denotation[label][case_id] = denotation_state(outcome.frame, gold)

    base_execution = _execution_report("base", base_outcomes)
    adapter_execution = _execution_report("adapter", adapter_outcomes)
    execution_comparison = build_safe_comparison(base_execution, adapter_execution)
    ordered_transitions = Counter(
        f"{denotation['base'][case_id]} -> {denotation['adapter'][case_id]}"
        for case_id in expected_ids
    )
    output_dir.mkdir(parents=True)
    for label, outcomes in (("base", base_outcomes), ("adapter", adapter_outcomes)):
        with (output_dir / f"{label}-normalized-candidates.jsonl").open(
            "x", encoding="utf-8"
        ) as handle:
            for row in _normalised_rows(outcomes):
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    report = {
        "report_schema_version": "thelook-v2-matching-evaluation-v1",
        "dataset": "thelook-cross-schema-final-test-v2",
        "case_count": EXPECTED_CASES,
        "matching_generation_marker": actual_marker,
        "base_execution": base_execution,
        "adapter_execution": adapter_execution,
        "execution_comparison": execution_comparison,
        "denotation_summary": {
            "base": dict(sorted(Counter(denotation["base"].values()).items())),
            "adapter": dict(sorted(Counter(denotation["adapter"].values()).items())),
            "transitions": dict(sorted(ordered_transitions.items())),
        },
        "raw_artifacts": {
            "base_normalized_candidates_sha256": sha256_file(
                output_dir / "base-normalized-candidates.jsonl"
            ),
            "adapter_normalized_candidates_sha256": sha256_file(
                output_dir / "adapter-normalized-candidates.jsonl"
            ),
            "raw_artifacts_outside_repository": True,
        },
        "boundaries": {
            "gold_sql_read_only_after_matching_marker": True,
            "production_default_unchanged": True,
            "raw_candidate_sql_in_repository": False,
            "raw_result_rows_in_repository": False,
        },
    }
    (output_dir / "evaluation-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "execution": execution_comparison,
                "denotation": report["denotation_summary"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TheLookV2MatchingError, ValueError) as exc:
        print(f"TheLook v2 matching evaluation error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
