"""Privacy-preserving diagnostics for paired offline Text-to-SQL runs.

The source SQLite reports contain raw model completions and SQLite error
messages. This module reads those external artifacts to compare a frozen base
model with one adapter, but only emits aggregate counts, distributions and
stable case IDs for bounded follow-up. Questions, SQL text, database/table/
column identifiers, result rows and raw error strings never enter its output.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
import hashlib
from math import ceil
from pathlib import Path
import re
from typing import Any

from data_analysis_agent.text_to_sql_output import normalize_text_to_sql_candidate


COMPARISON_REPORT_VERSION = "1"


class ComparisonInputError(ValueError):
    """A paired diagnostic report is incomplete or violates the contract."""


def sha256_file(path: Path) -> str:
    """Return a content hash without copying an artifact into the repository."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_sqlite_diagnostic_report(path: Path) -> dict[str, Any]:
    """Load one external SQLite diagnostic report with a minimal schema check."""

    try:
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ComparisonInputError(f"diagnostic report does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ComparisonInputError(f"diagnostic report is not valid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ComparisonInputError("diagnostic report must be a JSON object")
    records = raw.get("records")
    if not isinstance(records, list) or not records:
        raise ComparisonInputError("diagnostic report must contain non-empty records")
    if not all(isinstance(record, Mapping) for record in records):
        raise ComparisonInputError("diagnostic records must be JSON objects")
    return raw


def classify_execution(execution: Mapping[str, Any]) -> str:
    """Map raw policy and SQLite details to stable, non-sensitive categories."""

    status = execution.get("status")
    if not isinstance(status, str):
        raise ComparisonInputError("execution status must be a string")
    if status in {"executed", "timeout", "missing_prediction"}:
        return status

    message = execution.get("error_message")
    normalized_message = message.lower() if isinstance(message, str) else ""
    if status == "policy_rejected":
        if "parse failed" in normalized_message:
            return "policy_parse_failure"
        if "exactly one sql statement" in normalized_message:
            return "policy_multi_statement"
        if "sql must not be empty" in normalized_message:
            return "policy_empty_candidate"
        if "only select" in normalized_message or "forbidden" in normalized_message:
            return "policy_non_readonly"
        if "limit" in normalized_message:
            return "policy_limit"
        return "policy_other"
    if status != "execution_error":
        return f"unexpected_status_{status}"
    if "no such column" in normalized_message:
        return "no_such_column"
    if "no such table" in normalized_message:
        return "no_such_table"
    if "ambiguous column" in normalized_message:
        return "ambiguous_column"
    if "syntax error" in normalized_message:
        return "sqlite_syntax_error"
    if "incomplete input" in normalized_message:
        return "sqlite_incomplete_input"
    if "misuse of aggregate" in normalized_message:
        return "sqlite_aggregate_misuse"
    if "wrong number of arguments" in normalized_message:
        return "sqlite_function_arity"
    return "sqlite_other_execution_error"


def classify_execution_detail(execution: Mapping[str, Any]) -> str:
    """Expose useful error structure while retaining no identifier or SQL text."""

    category = classify_execution(execution)
    message = execution.get("error_message")
    normalized_message = message.lower() if isinstance(message, str) else ""
    if category == "no_such_column":
        if re.search(r"no such column:\s*[a-z_][a-z0-9_]*\.", normalized_message):
            return "no_such_column_qualified_reference"
        return "no_such_column_unqualified_or_other"
    if category == "no_such_table":
        if re.search(r"no such table:\s*t\d+\s*$", normalized_message):
            return "no_such_table_alias_placeholder"
        return "no_such_table_other_reference"
    if category == "policy_parse_failure":
        if "required keyword" in normalized_message or "expecting" in normalized_message:
            return "policy_parse_missing_expression"
        if "token" in normalized_message or "unterminated" in normalized_message:
            return "policy_parse_tokenization_or_quote"
        return "policy_parse_other"
    return category


def classify_presentation_pattern(completion: str) -> str:
    """Classify completion formatting without retaining its content."""

    lines = completion.strip().splitlines()
    first_content = next((line.strip() for line in lines if line.strip()), "")
    if first_content.startswith("```"):
        return "opening_code_fence"
    lowered = first_content.lower()
    if lowered.startswith("sqlquery:") or lowered.startswith("sql:"):
        return "sql_prefix"
    if any(line.strip().startswith("###") for line in lines[1:]):
        return "section_continuation"
    if lowered.startswith(("select", "with")):
        return "direct_query"
    return "other"


def _required_string(record: Mapping[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ComparisonInputError(f"record {key} must be a non-empty string")
    return value


def _required_non_negative_int(record: Mapping[str, Any], key: str) -> int:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ComparisonInputError(f"record {key} must be a non-negative integer")
    return value


def _record_map(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    records = report["records"]
    if not isinstance(records, Sequence):
        raise ComparisonInputError("diagnostic report records must be a sequence")
    mapped: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise ComparisonInputError("diagnostic record must be an object")
        case_id = _required_string(record, "case_id")
        candidate_index = record.get("candidate_index")
        if candidate_index != 0:
            raise ComparisonInputError("paired analysis requires candidate_index=0 only")
        if case_id in mapped:
            raise ComparisonInputError("paired analysis requires unique case IDs")
        execution = record.get("execution")
        if not isinstance(execution, Mapping):
            raise ComparisonInputError("diagnostic record has no execution object")
        _required_non_negative_int(record, "generated_tokens")
        _required_string(execution, "original_sql")
        mapped[case_id] = record
    return mapped


def _distribution(values: Sequence[int]) -> dict[str, int | float]:
    if not values:
        raise ComparisonInputError("distribution cannot be empty")
    ordered = sorted(values)

    def percentile(percent: float) -> int:
        return ordered[ceil(percent * len(ordered)) - 1]

    return {
        "count": len(ordered),
        "sum": sum(ordered),
        "mean": round(sum(ordered) / len(ordered), 2),
        "min": ordered[0],
        "p10": percentile(0.10),
        "p25": percentile(0.25),
        "p50": percentile(0.50),
        "p75": percentile(0.75),
        "p90": percentile(0.90),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "max": ordered[-1],
    }


def _run_profile(
    records: Mapping[str, Mapping[str, Any]], max_new_tokens: int
) -> dict[str, Any]:
    status_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    detail_counts: Counter[str] = Counter()
    presentation_counts: Counter[str] = Counter()
    generated_tokens: list[int] = []
    normalized_lengths: list[int] = []
    for record in records.values():
        execution = record["execution"]
        assert isinstance(execution, Mapping)
        status = _required_string(execution, "status")
        completion = _required_string(execution, "original_sql")
        status_counts[status] += 1
        category_counts[classify_execution(execution)] += 1
        detail_counts[classify_execution_detail(execution)] += 1
        presentation_counts[classify_presentation_pattern(completion)] += 1
        generated_tokens.append(_required_non_negative_int(record, "generated_tokens"))
        normalized_lengths.append(len(normalize_text_to_sql_candidate(completion)))

    return {
        "status_counts": dict(sorted(status_counts.items())),
        "error_category_counts": dict(sorted(category_counts.items())),
        "error_detail_counts": dict(sorted(detail_counts.items())),
        "completion_presentation_counts": dict(sorted(presentation_counts.items())),
        "generated_token_distribution": {
            **_distribution(generated_tokens),
            "at_generation_cap": sum(token == max_new_tokens for token in generated_tokens),
            "below_generation_cap": sum(token < max_new_tokens for token in generated_tokens),
        },
        "normalized_completion_character_distribution": _distribution(normalized_lengths),
    }


def analyze_paired_sqlite_diagnostics(
    *,
    base_report: Mapping[str, Any],
    adapter_report: Mapping[str, Any],
    max_new_tokens: int,
    sample_limit: int,
) -> dict[str, Any]:
    """Compare paired reports while emitting no SQL, question, or error text."""

    if max_new_tokens <= 0:
        raise ComparisonInputError("max_new_tokens must be positive")
    if sample_limit <= 0:
        raise ComparisonInputError("sample_limit must be positive")
    base_records = _record_map(base_report)
    adapter_records = _record_map(adapter_report)
    if set(base_records) != set(adapter_records):
        raise ComparisonInputError("base and adapter reports must have identical case IDs")

    status_transitions: Counter[str] = Counter()
    category_transitions: Counter[str] = Counter()
    samples: dict[str, list[str]] = defaultdict(list)
    database_execution: dict[str, dict[str, int]] = defaultdict(
        lambda: {"base": 0, "adapter": 0}
    )
    for case_id in sorted(base_records):
        base = base_records[case_id]
        adapter = adapter_records[case_id]
        base_execution = base["execution"]
        adapter_execution = adapter["execution"]
        assert isinstance(base_execution, Mapping)
        assert isinstance(adapter_execution, Mapping)
        base_status = _required_string(base_execution, "status")
        adapter_status = _required_string(adapter_execution, "status")
        status_transition = f"{base_status} -> {adapter_status}"
        category_transition = (
            f"{classify_execution(base_execution)} -> "
            f"{classify_execution(adapter_execution)}"
        )
        status_transitions[status_transition] += 1
        category_transitions[category_transition] += 1
        if status_transition != "executed -> executed" and len(samples[category_transition]) < sample_limit:
            samples[category_transition].append(case_id)

        database_id = _required_string(base, "database_id")
        if database_id != _required_string(adapter, "database_id"):
            raise ComparisonInputError("paired reports disagree on a case database ID")
        database_execution[database_id]["base"] += int(base_status == "executed")
        database_execution[database_id]["adapter"] += int(adapter_status == "executed")

    database_deltas = [
        values["adapter"] - values["base"] for values in database_execution.values()
    ]
    return {
        "report_version": COMPARISON_REPORT_VERSION,
        "analysis_scope": {
            "raw_question_or_sql_written": False,
            "raw_error_message_written": False,
            "result_rows_written": False,
            "case_ids_only_for_bounded_follow_up": True,
            "max_new_tokens": max_new_tokens,
            "sample_limit_per_changed_category": sample_limit,
        },
        "base": _run_profile(base_records, max_new_tokens),
        "adapter": _run_profile(adapter_records, max_new_tokens),
        "status_transitions": dict(sorted(status_transitions.items())),
        "error_category_transitions": dict(sorted(category_transitions.items())),
        "changed_case_samples": dict(sorted(samples.items())),
        "per_database_execution_delta": {
            "database_count": len(database_deltas),
            "improved_database_count": sum(delta > 0 for delta in database_deltas),
            "unchanged_database_count": sum(delta == 0 for delta in database_deltas),
            "regressed_database_count": sum(delta < 0 for delta in database_deltas),
            "distribution": _distribution(database_deltas),
        },
    }
