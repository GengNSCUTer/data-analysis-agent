"""Project an admitted multi-schema SFT row into a traceable SQL DPO seed."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .dpo_pair import DpoPairValidationError, canonical_sql, sha256_text


DPO_PAIR_SEED_SCHEMA_VERSION = "multi-schema-sql-dpo-pair-seed-v1"
MULTI_SCHEMA_PROMPT_VERSION = "multi-schema-sql-only-v1"
_ALLOWED_SPLITS = frozenset({"train", "validation"})
_ALLOWED_WORKSPACES = frozenset({"olist", "thelook"})
_ALLOWED_RESULT_SHAPES = frozenset(
    {"scalar", "state_grouped", "category_grouped", "dimension_grouped", "time_series"}
)


@dataclass(frozen=True)
class DpoPairSeed:
    """Chosen-side DPO data, prior to a parent-model rejected candidate."""

    pair_id: str
    split: str
    workspace_key: str
    prompt: str
    chosen_sql: str
    source: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DPO_PAIR_SEED_SCHEMA_VERSION,
            "pair_id": self.pair_id,
            "split": self.split,
            "source": self.source,
            "prompt": self.prompt,
            "chosen": {
                "sql": self.chosen_sql,
                "sql_sha256": sha256_text(self.chosen_sql),
            },
            "rejected": None,
        }


def _text(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DpoPairValidationError(f"SFT row lacks required text: {key}")
    return value


def _split_name(row: Mapping[str, Any]) -> str:
    split = row.get("split")
    if not isinstance(split, Mapping) or not isinstance(split.get("name"), str):
        raise DpoPairValidationError("SFT row lacks split.name")
    value = split["name"].strip()
    if value not in _ALLOWED_SPLITS:
        raise DpoPairValidationError("SFT row may only seed train or validation DPO data")
    return value


def _string_list(row: Mapping[str, Any], key: str) -> list[str]:
    value = row.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise DpoPairValidationError(f"SFT row lacks non-empty string list: {key}")
    return list(value)


def _result_shape(row: Mapping[str, Any]) -> str:
    """Read the shape from QuerySpec rather than trusting a duplicate field."""

    query_spec = row.get("query_spec")
    if not isinstance(query_spec, Mapping):
        raise DpoPairValidationError("SFT row lacks QuerySpec")
    shape = query_spec.get("result_shape")
    if not isinstance(shape, str) or shape not in _ALLOWED_RESULT_SHAPES:
        raise DpoPairValidationError("SFT row has unsupported QuerySpec result_shape")
    return shape


def build_dpo_pair_seed(
    row: Mapping[str, Any], *, release_version: str
) -> DpoPairSeed:
    """Validate one frozen SFT row and project its prompt/Gold SQL to a seed.

    This function deliberately cannot create a final pair: only the later
    collector may attach a parent-model completion and governance evidence as
    ``rejected``.
    """

    if not isinstance(release_version, str) or not release_version.strip():
        raise DpoPairValidationError("release version is required")
    if row.get("admission_status") != "admitted":
        raise DpoPairValidationError("only admitted SFT rows may seed DPO")
    if row.get("prompt_format_version") != MULTI_SCHEMA_PROMPT_VERSION:
        raise DpoPairValidationError("SFT row does not use the frozen multi-schema prompt")

    split = _split_name(row)
    workspace_key = _text(row, "workspace_key").strip()
    if workspace_key not in _ALLOWED_WORKSPACES:
        raise DpoPairValidationError("unsupported SFT workspace")
    prompt = _text(row, "rendered_prompt")
    chosen_sql = _text(row, "candidate_sql")
    if not prompt.endswith("### SQL"):
        raise DpoPairValidationError("SFT prompt lost its SQL-only boundary")
    if row.get("training_text") != f"{prompt}\n{chosen_sql.strip()}":
        raise DpoPairValidationError("SFT training text boundary does not bind prompt to Gold SQL")
    gold_hash = _text(row, "canonical_gold_sql_sha256").strip()
    if sha256_text(chosen_sql) != gold_hash:
        raise DpoPairValidationError("SFT candidate SQL does not match canonical Gold hash")
    # A later DPO pair must reject formatting-only completions. Parsing the
    # chosen side here establishes that the source Gold itself is canonical.
    canonical_sql(chosen_sql)

    sample_id = _text(row, "sample_id").strip()
    query_spec_id = _text(row, "query_spec_id").strip()
    source = {
        "release_version": release_version.strip(),
        "release_split": split,
        "workspace_key": workspace_key,
        "sample_id": sample_id,
        "query_spec_id": query_spec_id,
        "scenario_family_id": _text(row, "scenario_family_id").strip(),
        "program_id": _text(row, "program_id").strip(),
        "risk_tags": _string_list(row, "risk_tags"),
        "risk_tag_derivation": str(row.get("dpo_risk_tag_derivation", "frozen_source")),
        # This comes from the pinned QuerySpec, so a later pair-quality audit
        # can prove its shape stratum without parsing prompt or SQL text.
        "result_shape": _result_shape(row),
        "prompt_format_version": MULTI_SCHEMA_PROMPT_VERSION,
        "prompt_sha256": sha256_text(prompt),
        "canonical_gold_sql_sha256": gold_hash,
    }
    pair_fingerprint = sha256_text(
        "|".join((release_version.strip(), split, workspace_key, sample_id, source["prompt_sha256"]))
    )
    return DpoPairSeed(
        pair_id=f"dpo-seed-{workspace_key}-{pair_fingerprint[:16]}",
        split=split,
        workspace_key=workspace_key,
        prompt=prompt,
        chosen_sql=chosen_sql,
        source=source,
    )
