"""Safe aggregation helpers for the offline Olist Base/Adapter comparison."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


SAFE_RECORD_FIELDS = frozenset(
    {
        "source_id",
        "route_state",
        "generation_status",
        "generated_tokens",
        "generation_elapsed_ms",
        "policy_status",
        "execution_status",
        "result_validation_state",
        "result_contract_satisfied",
        "failure_category",
    }
)
FORBIDDEN_REPORT_FIELDS = frozenset(
    {
        "question",
        "raw_question",
        "raw_user_text",
        "candidate_sql",
        "final_sql",
        "gold_sql",
        "result_rows",
        "raw_result_rows",
        "connection_string",
        "password",
        "api_key",
        "access_token",
        "cookie",
    }
)


class OlistCandidateEvaluationError(ValueError):
    """A report or manifest violates the frozen comparison contract."""


@dataclass(frozen=True)
class CandidateEvaluationRecord:
    """One redacted candidate-generation and trusted-execution outcome."""

    source_id: str
    route_state: str
    generation_status: str
    generated_tokens: int | None
    generation_elapsed_ms: int | None
    policy_status: str
    execution_status: str
    result_validation_state: str | None
    result_contract_satisfied: bool
    failure_category: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "route_state": self.route_state,
            "generation_status": self.generation_status,
            "generated_tokens": self.generated_tokens,
            "generation_elapsed_ms": self.generation_elapsed_ms,
            "policy_status": self.policy_status,
            "execution_status": self.execution_status,
            "result_validation_state": self.result_validation_state,
            "result_contract_satisfied": self.result_contract_satisfied,
            "failure_category": self.failure_category,
        }


def validate_manifest_cases(
    manifest: Mapping[str, Any],
    source_cases: Sequence[Mapping[str, Any]],
    holdout_manifest: Mapping[str, Any],
) -> tuple[str, ...]:
    """Validate that selected Olist cases are DB-only permanent holdout cases."""
    if manifest.get("forbidden_for_training") is not True:
        raise OlistCandidateEvaluationError("manifest must forbid training use")
    if manifest.get("production_default_unchanged") is not True:
        raise OlistCandidateEvaluationError("manifest must preserve the production default")
    raw_ids = manifest.get("source_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise OlistCandidateEvaluationError("manifest source_ids must be a non-empty list")
    source_ids = tuple(str(case_id) for case_id in raw_ids)
    if len(set(source_ids)) != len(source_ids):
        raise OlistCandidateEvaluationError("manifest source_ids must be unique")

    source_by_id = {
        str(case.get("id")): case
        for case in source_cases
        if isinstance(case, Mapping) and isinstance(case.get("id"), str)
    }
    holdout_ids = {
        str(case.get("case_id"))
        for case in holdout_manifest.get("cases", ())
        if isinstance(case, Mapping) and case.get("forbidden_for_training") is True
    }
    for source_id in source_ids:
        case = source_by_id.get(source_id)
        if case is None:
            raise OlistCandidateEvaluationError(f"manifest source_id is not in the source suite: {source_id}")
        if source_id not in holdout_ids:
            raise OlistCandidateEvaluationError(f"manifest source_id is not a protected holdout: {source_id}")
        if case.get("requires_database") is not True or case.get("expected_state") != "answerable":
            raise OlistCandidateEvaluationError(
                f"manifest source_id is not an answerable database case: {source_id}"
            )
    return source_ids


def build_safe_report(
    *,
    report_metadata: Mapping[str, Any],
    records: Sequence[CandidateEvaluationRecord],
) -> dict[str, Any]:
    """Build a repository-safe report that contains no SQL, questions, or rows."""
    if not records:
        raise OlistCandidateEvaluationError("safe report requires at least one record")
    source_ids = [record.source_id for record in records]
    if len(set(source_ids)) != len(source_ids):
        raise OlistCandidateEvaluationError("safe report contains duplicate source IDs")
    report = {
        "report_schema_version": "1",
        **dict(report_metadata),
        "summary": {
            "case_count": len(records),
            "generation_success": sum(record.generation_status == "generated" for record in records),
            "policy_accepted": sum(record.policy_status == "accepted" for record in records),
            "postgres_executed": sum(record.execution_status == "executed" for record in records),
            "result_contract_valid": sum(record.result_contract_satisfied for record in records),
        },
        "records": [record.as_dict() for record in records],
    }
    _validate_safe_value(report)
    return report


def build_safe_comparison(
    base_report: Mapping[str, Any], adapter_report: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare matching Base/Adapter safe reports without exposing raw artifacts."""
    _validate_safe_value(base_report)
    _validate_safe_value(adapter_report)
    _require_matching_contract(base_report, adapter_report)
    base_records = _records_by_source_id(base_report)
    adapter_records = _records_by_source_id(adapter_report)
    if base_records.keys() != adapter_records.keys():
        raise OlistCandidateEvaluationError("base and adapter reports select different source IDs")

    transitions: Counter[str] = Counter()
    non_valid_to_valid = 0
    valid_to_non_valid = 0
    for source_id in sorted(base_records):
        base = base_records[source_id]
        adapter = adapter_records[source_id]
        base_outcome = _outcome(base)
        adapter_outcome = _outcome(adapter)
        transitions[f"{base_outcome} -> {adapter_outcome}"] += 1
        if not bool(base["result_contract_satisfied"]) and bool(adapter["result_contract_satisfied"]):
            non_valid_to_valid += 1
        if bool(base["result_contract_satisfied"]) and not bool(adapter["result_contract_satisfied"]):
            valid_to_non_valid += 1

    metrics = (
        "generation_success",
        "policy_accepted",
        "postgres_executed",
        "result_contract_valid",
    )
    base_summary = _summary(base_report)
    adapter_summary = _summary(adapter_report)
    return {
        "comparison_schema_version": "1",
        "comparison_contract": dict(base_report["comparison_contract"]),
        "case_count": len(base_records),
        "base_summary": {metric: int(base_summary[metric]) for metric in metrics},
        "adapter_summary": {metric: int(adapter_summary[metric]) for metric in metrics},
        "adapter_minus_base": {
            metric: int(adapter_summary[metric]) - int(base_summary[metric])
            for metric in metrics
        },
        "non_valid_to_valid": non_valid_to_valid,
        "valid_to_non_valid": valid_to_non_valid,
        "outcome_transitions": dict(sorted(transitions.items())),
        "source_ids": sorted(base_records),
    }


def build_safe_locale_comparison(
    source_report: Mapping[str, Any], target_report: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare one model across prompt-language overlays without raw artifacts.

    This is intentionally separate from :func:`build_safe_comparison`: Base vs
    Adapter requires identical contracts, while a locale experiment must differ
    only in the candidate-question overlay contract.
    """
    _validate_safe_value(source_report)
    _validate_safe_value(target_report)
    if source_report.get("run_label") != target_report.get("run_label"):
        raise OlistCandidateEvaluationError("locale reports must use the same run_label")
    source_contract = _locale_comparison_contract(source_report)
    target_contract = _locale_comparison_contract(target_report)
    if source_contract["invariant"] != target_contract["invariant"]:
        raise OlistCandidateEvaluationError("locale reports differ beyond their question overlay")
    source_records = _records_by_source_id(source_report)
    target_records = _records_by_source_id(target_report)
    if source_records.keys() != target_records.keys():
        raise OlistCandidateEvaluationError("locale reports select different source IDs")

    transitions: Counter[str] = Counter()
    for source_id in sorted(source_records):
        transitions[
            f"{_outcome(source_records[source_id])} -> {_outcome(target_records[source_id])}"
        ] += 1
    metrics = (
        "generation_success",
        "policy_accepted",
        "postgres_executed",
        "result_contract_valid",
    )
    source_summary = _summary(source_report)
    target_summary = _summary(target_report)
    return {
        "locale_comparison_schema_version": "1",
        "run_label": source_report["run_label"],
        "invariant_contract": source_contract["invariant"],
        "source_question_condition": source_contract["overlay"],
        "target_question_condition": target_contract["overlay"],
        "case_count": len(source_records),
        "source_summary": {metric: int(source_summary[metric]) for metric in metrics},
        "target_summary": {metric: int(target_summary[metric]) for metric in metrics},
        "target_minus_source": {
            metric: int(target_summary[metric]) - int(source_summary[metric])
            for metric in metrics
        },
        "outcome_transitions": dict(sorted(transitions.items())),
        "source_ids": sorted(source_records),
    }


def _validate_safe_value(value: Any) -> None:
    if isinstance(value, Mapping):
        forbidden = FORBIDDEN_REPORT_FIELDS.intersection(value)
        if forbidden:
            raise OlistCandidateEvaluationError(
                f"unsafe report field(s): {', '.join(sorted(forbidden))}"
            )
        for nested in value.values():
            _validate_safe_value(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _validate_safe_value(nested)


def _require_matching_contract(base: Mapping[str, Any], adapter: Mapping[str, Any]) -> None:
    base_contract = base.get("comparison_contract")
    adapter_contract = adapter.get("comparison_contract")
    if not isinstance(base_contract, Mapping) or not isinstance(adapter_contract, Mapping):
        raise OlistCandidateEvaluationError("both reports require a comparison_contract")
    if dict(base_contract) != dict(adapter_contract):
        raise OlistCandidateEvaluationError("base and adapter comparison contracts differ")
    if base.get("run_label") != "base" or adapter.get("run_label") != "adapter":
        raise OlistCandidateEvaluationError("reports must be labeled base and adapter")


def _locale_comparison_contract(report: Mapping[str, Any]) -> dict[str, Any]:
    contract = report.get("comparison_contract")
    if not isinstance(contract, Mapping):
        raise OlistCandidateEvaluationError("locale report requires a comparison_contract")
    invariant = dict(contract)
    overlay = invariant.pop("candidate_question_overlay", None)
    # The pre-overlay Chinese transfer report predates this explicit field.
    if overlay is None:
        overlay = {"mode": "source_question", "language": "zh", "overlay_sha256": None}
    if not isinstance(overlay, Mapping):
        raise OlistCandidateEvaluationError("candidate question overlay must be a mapping")
    invariant.pop("manifest_id", None)
    invariant.pop("manifest_sha256", None)
    return {"invariant": invariant, "overlay": dict(overlay)}


def _records_by_source_id(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw_records = report.get("records")
    if not isinstance(raw_records, list) or not raw_records:
        raise OlistCandidateEvaluationError("report records must be a non-empty list")
    records: dict[str, Mapping[str, Any]] = {}
    for record in raw_records:
        if not isinstance(record, Mapping):
            raise OlistCandidateEvaluationError("report record must be an object")
        if set(record) != SAFE_RECORD_FIELDS:
            raise OlistCandidateEvaluationError("report record fields do not match the safe schema")
        source_id = record.get("source_id")
        if not isinstance(source_id, str) or not source_id or source_id in records:
            raise OlistCandidateEvaluationError("report records contain invalid source IDs")
        records[source_id] = record
    return records


def _summary(report: Mapping[str, Any]) -> Mapping[str, Any]:
    summary = report.get("summary")
    if not isinstance(summary, Mapping):
        raise OlistCandidateEvaluationError("report requires a summary")
    return summary


def _outcome(record: Mapping[str, Any]) -> str:
    return "result_contract_valid" if record["result_contract_satisfied"] else str(
        record["failure_category"] or "not_valid"
    )
