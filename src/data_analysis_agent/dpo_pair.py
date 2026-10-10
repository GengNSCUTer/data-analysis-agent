"""Fail-closed schema validation for offline SQL DPO preference pairs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from typing import Any

from sqlglot import parse

from .candidate_sql_generator import CandidateSqlGenerationError, unwrap_sql_completion


DPO_PAIR_SCHEMA_VERSION = "multi-schema-sql-dpo-pair-v1"
_TRAINING_SPLITS = frozenset({"train", "validation"})
_WORKSPACES = frozenset({"olist", "thelook"})
_TRAINING_TIERS = frozenset({"A", "B", "C"})
_ALL_TIERS = _TRAINING_TIERS | {"D"}
_RESULT_SHAPES = frozenset(
    {"scalar", "state_grouped", "category_grouped", "dimension_grouped", "time_series"}
)
_RISK_TAG_DERIVATIONS = frozenset(
    {"frozen_source", "olist-dpo-risk-tag-backfill-v1"}
)


class DpoPairValidationError(ValueError):
    """A preference pair lacks a pinned source or coherent error evidence."""


@dataclass(frozen=True)
class ValidatedDpoPair:
    """Minimal verified projection used by a later collector or trainer."""

    pair_id: str
    split: str
    workspace_key: str
    tier: str
    prompt: str
    chosen_sql: str
    rejected_sql: str
    prompt_sha256: str
    chosen_sql_sha256: str
    rejected_sql_sha256: str


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_sql(sql: str) -> str:
    """Produce a parser-normalized identity solely to reject formatting-only pairs."""

    try:
        normalized = unwrap_sql_completion(sql)
    except CandidateSqlGenerationError as exc:
        raise DpoPairValidationError("SQL completion must be non-empty") from exc
    try:
        statements = parse(normalized, read="postgres")
    except Exception as exc:  # sqlglot exposes several parse exception types.
        raise DpoPairValidationError("SQL completion is not parseable PostgreSQL") from exc
    if len(statements) != 1:
        raise DpoPairValidationError("SQL completion must contain exactly one statement")
    return statements[0].sql(dialect="postgres", normalize=True, pretty=False)


def _required_string(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DpoPairValidationError(f"missing required string: {key}")
    return value.strip()


def _required_text(mapping: Mapping[str, Any], key: str) -> str:
    """Require non-empty text while preserving bytes bound by a source hash."""

    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DpoPairValidationError(f"missing required text: {key}")
    return value


def _required_mapping(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise DpoPairValidationError(f"missing required object: {key}")
    return value


def _validate_rejected_evidence(tier: str, evidence: Mapping[str, Any]) -> None:
    policy = evidence.get("policy_status")
    execution = evidence.get("execution_status")
    result_contract = evidence.get("result_contract_valid")
    denotation = evidence.get("gold_denotation_state")
    expected = {
        "A": ("accepted", "executed", True, {"column_mismatch", "row_count_mismatch", "denotation_mismatch"}),
        "B": ("accepted", "executed", False, {"not_evaluated"}),
        "C": ("accepted", "error", False, {"not_evaluated"}),
        "D": ("rejected", "not_run", False, {"not_evaluated"}),
    }[tier]
    if (
        policy != expected[0]
        or execution != expected[1]
        or result_contract is not expected[2]
        or denotation not in expected[3]
    ):
        raise DpoPairValidationError("rejected evidence does not match its declared tier")


def validate_dpo_pair(
    pair: Mapping[str, Any],
    *,
    allow_diagnostic_tier_d: bool = False,
) -> ValidatedDpoPair:
    """Validate a complete DPO v1 pair without loading a model or database.

    The caller remains responsible for proving the candidate evidence came from
    the frozen parent Adapter.  This validator ensures that evidence cannot be
    relabelled into an incompatible preference tier or mixed across splits.
    """

    if pair.get("schema_version") != DPO_PAIR_SCHEMA_VERSION:
        raise DpoPairValidationError("unsupported DPO pair schema version")
    pair_id = _required_string(pair, "pair_id")
    split = _required_string(pair, "split")
    if split not in _TRAINING_SPLITS:
        raise DpoPairValidationError("DPO pairs may only originate from train or validation")

    source = _required_mapping(pair, "source")
    if _required_string(source, "release_split") != split:
        raise DpoPairValidationError("source release split must equal pair split")
    workspace_key = _required_string(source, "workspace_key")
    if workspace_key not in _WORKSPACES:
        raise DpoPairValidationError("unsupported workspace")
    _required_string(source, "sample_id")
    _required_string(source, "query_spec_id")
    _required_string(source, "release_version")
    _required_string(source, "scenario_family_id")
    _required_string(source, "program_id")
    if _required_string(source, "result_shape") not in _RESULT_SHAPES:
        raise DpoPairValidationError("source has unsupported result_shape")
    if _required_string(source, "risk_tag_derivation") not in _RISK_TAG_DERIVATIONS:
        raise DpoPairValidationError("source has unsupported risk_tag_derivation")
    risk_tags = source.get("risk_tags")
    if (
        not isinstance(risk_tags, list)
        or not risk_tags
        or any(not isinstance(tag, str) or not tag.strip() for tag in risk_tags)
        or len(set(risk_tags)) != len(risk_tags)
    ):
        raise DpoPairValidationError("source must retain non-empty unique risk_tags")

    prompt = _required_text(pair, "prompt")
    if not prompt.endswith("### SQL"):
        raise DpoPairValidationError("prompt must preserve the SQL-only boundary")
    if _required_string(source, "prompt_sha256") != sha256_text(prompt):
        raise DpoPairValidationError("prompt hash does not bind the pair source")

    chosen = _required_mapping(pair, "chosen")
    chosen_sql = _required_text(chosen, "sql")
    chosen_sql_sha256 = sha256_text(chosen_sql)
    if _required_string(source, "canonical_gold_sql_sha256") != chosen_sql_sha256:
        raise DpoPairValidationError("chosen SQL does not match the source Gold hash")
    if _required_string(chosen, "sql_sha256") != chosen_sql_sha256:
        raise DpoPairValidationError("chosen SQL hash mismatch")

    rejected = _required_mapping(pair, "rejected")
    rejected_sql = _required_text(rejected, "sql")
    rejected_sql_sha256 = sha256_text(rejected_sql)
    if _required_string(rejected, "sql_sha256") != rejected_sql_sha256:
        raise DpoPairValidationError("rejected SQL hash mismatch")
    if canonical_sql(chosen_sql) == canonical_sql(rejected_sql):
        raise DpoPairValidationError("chosen and rejected SQL differ only in presentation")

    tier = _required_string(rejected, "tier")
    if tier not in _ALL_TIERS or (tier == "D" and not allow_diagnostic_tier_d):
        raise DpoPairValidationError("rejected tier is not permitted for DPO training")
    _validate_rejected_evidence(tier, _required_mapping(rejected, "evidence"))

    provenance = _required_mapping(rejected, "provenance")
    _required_string(provenance, "model_id")
    _required_string(provenance, "model_revision")
    _required_string(provenance, "parent_adapter_model_sha256")
    if not isinstance(provenance.get("decode"), Mapping):
        raise DpoPairValidationError("rejected provenance lacks decode settings")

    return ValidatedDpoPair(
        pair_id=pair_id,
        split=split,
        workspace_key=workspace_key,
        tier=tier,
        prompt=prompt,
        chosen_sql=chosen_sql,
        rejected_sql=rejected_sql,
        prompt_sha256=sha256_text(prompt),
        chosen_sql_sha256=chosen_sql_sha256,
        rejected_sql_sha256=rejected_sql_sha256,
    )
