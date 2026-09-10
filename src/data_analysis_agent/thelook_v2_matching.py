"""Pure contracts shared by the protected TheLook v2 matching evaluation.

This module deliberately has no GPU, database, or model-framework dependency.
It is responsible for three things only:

* projecting a protected final-test case into the fields generation may read;
* deterministically rendering the v2 candidate-SQL prompt;
* proving that two completed Base/Adapter generation runs are a matching pair
  before a later command is allowed to read Gold SQL.

Questions, model completions, Gold SQL, and result rows remain in external
artifacts.  The returned reports contain only hashes, case IDs, and aggregate
safe metadata.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from .semantic_catalog import CatalogLoader
from .thelook_v2_context import THELOOK_V2_WORKSPACE
from .thelook_v2_queryspec import TheLookV2QuerySpec, validate_thelook_v2_query_spec


EVALUATION_VERSION = "thelook-cross-schema-final-test-v2"
PROMPT_VERSION = "thelook-candidate-sql-v2"
MATCHING_CONTRACT_VERSION = "thelook-v2-base-adapter-matching-v1"
MATCHING_MARKER_VERSION = "thelook-v2-matching-generation-marker-v1"
EXPECTED_CASES = 600
EXPECTED_MODEL_ID = "Qwen/Qwen2.5-Coder-1.5B"
EXPECTED_MODEL_REVISION = "df3ce67c0e24480f20468b6ef2894622d69eb73b"
EXPECTED_BASE_WEIGHT_MODE = "bf16_lora"
EXPECTED_MAX_INPUT_TOKENS = 4096
EXPECTED_MAX_NEW_TOKENS = 768
EXPECTED_SEED = 20260910
# Frozen by the tokenizer-only preflight on the protected 600-case set.  These
# values bind the textual Catalog/QuerySpec/result-contract prompt itself, not
# merely its version string, and ensure no runner silently evaluates a changed
# prompt condition under the same Base/Adapter label.
EXPECTED_PROMPT_BUNDLE_SHA256 = (
    "48bdc6763f8a7a5ead870d1ae9b25ba4dede6c99cf39fea0c5294cd4223ab123"
)
EXPECTED_PROMPT_TOKEN_MIN = 1948
EXPECTED_PROMPT_TOKEN_MAX = 2075


class TheLookV2MatchingError(ValueError):
    """A protected v2 generation or comparison contract was violated."""


@dataclass(frozen=True)
class TheLookV2GenerationCase:
    """The complete protected input a generator is permitted to observe."""

    case_id: str
    question: str
    query_spec: Mapping[str, Any]
    required_result_columns: tuple[str, ...]


def sha256_file(path: Path) -> str:
    """Hash an external artifact without copying it into the repository."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TheLookV2MatchingError(f"{label} is unavailable or invalid") from exc
    if not isinstance(value, Mapping):
        raise TheLookV2MatchingError(f"{label} must be an object")
    return value


def _read_jsonl(path: Path, label: str) -> list[Mapping[str, Any]]:
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise TheLookV2MatchingError(f"{label} is unavailable or invalid") from exc
    if not rows or not all(isinstance(row, Mapping) for row in rows):
        raise TheLookV2MatchingError(f"{label} must contain non-empty object rows")
    return rows


def read_generation_cases(
    cases_jsonl: Path, manifest_path: Path
) -> tuple[list[TheLookV2GenerationCase], Mapping[str, Any]]:
    """Load only generation-safe final-test fields; deliberately never read Gold.

    Do not replace the explicit per-field projection below with ``dict(row)``.
    The narrow projection is both an access boundary and executable evidence
    that the generator cannot receive ``gold_sql`` or its hash from this
    function.
    """

    manifest = _read_json(manifest_path, "TheLook v2 manifest")
    output = manifest.get("output")
    if (
        manifest.get("evaluation_version") != EVALUATION_VERSION
        or not isinstance(output, Mapping)
        or not isinstance(output.get("cases_jsonl"), Mapping)
    ):
        raise TheLookV2MatchingError("unexpected TheLook v2 evaluation manifest")
    cases_meta = output["cases_jsonl"]
    if cases_meta.get("rows") != EXPECTED_CASES or cases_meta.get(
        "sha256"
    ) != sha256_file(cases_jsonl):
        raise TheLookV2MatchingError(
            "final-test case hash or row count differs from manifest"
        )
    checks = manifest.get("checks")
    if (
        not isinstance(checks, Mapping)
        or checks.get("base_or_adapter_generation_run") is not False
    ):
        raise TheLookV2MatchingError(
            "final-test manifest does not prove pre-generation isolation"
        )

    catalog = CatalogLoader(THELOOK_V2_WORKSPACE).load()
    cases = [
        project_generation_case(row, row_number=row_number, catalog=catalog)
        for row_number, row in enumerate(
            _read_jsonl(cases_jsonl, "TheLook v2 cases"), 1
        )
    ]

    ordered_ids = [case.case_id for case in cases]
    if len(cases) != EXPECTED_CASES or ordered_ids != sorted(ordered_ids):
        raise TheLookV2MatchingError("final-test case order or count is not frozen")
    if len(set(ordered_ids)) != len(ordered_ids):
        raise TheLookV2MatchingError("final-test has duplicate case IDs")
    return cases, manifest


def project_generation_case(
    row: Mapping[str, Any], *, row_number: int, catalog: Any | None = None
) -> TheLookV2GenerationCase:
    """Project one row without touching its Gold-only fields.

    Keeping this small helper separately testable prevents a future refactor
    from accidentally adding ``gold_sql`` to generation input handling.
    """

    required = {"case_id", "question", "query_spec", "required_result_columns"}
    if not required.issubset(row):
        raise TheLookV2MatchingError(f"case {row_number} lacks generation fields")
    case_id = row["case_id"]
    question = row["question"]
    query_spec = row["query_spec"]
    result_columns = row["required_result_columns"]
    if not isinstance(case_id, str) or not case_id:
        raise TheLookV2MatchingError(f"case {row_number} has invalid case_id")
    if not isinstance(question, str) or not question.strip():
        raise TheLookV2MatchingError(f"case {case_id} has invalid question")
    if not isinstance(query_spec, Mapping) or not isinstance(result_columns, list):
        raise TheLookV2MatchingError(f"case {case_id} has invalid generation metadata")
    if any(not isinstance(column, str) or not column for column in result_columns):
        raise TheLookV2MatchingError(f"case {case_id} has invalid result columns")
    spec = validate_thelook_v2_query_spec(
        TheLookV2QuerySpec.from_mapping(query_spec),
        catalog or CatalogLoader(THELOOK_V2_WORKSPACE).load(),
    )
    if tuple(result_columns) != spec.required_result_columns:
        raise TheLookV2MatchingError(
            f"case {case_id} result contract differs from QuerySpec"
        )
    return TheLookV2GenerationCase(
        case_id=case_id,
        question=question,
        query_spec=spec.as_dict(),
        required_result_columns=tuple(result_columns),
    )


def render_catalog_prompt() -> str:
    """Render the complete server-owned v2 semantic Catalog deterministically."""

    catalog = CatalogLoader(THELOOK_V2_WORKSPACE).load()
    lines = [
        "Catalog version: "
        f"{catalog.catalog_version}; dataset: {catalog.dataset_version}; dialect: PostgreSQL",
        "Use only analytics views and the listed columns. IDs are join keys, not output columns.",
        "Tables:",
    ]
    for table in catalog.tables:
        columns = ", ".join(column.name for column in table.columns)
        lines.append(f"- analytics.{table.physical_name} ({table.grain}): {columns}")
    lines.append("Metrics:")
    for metric in catalog.metrics:
        lines.append(
            f"- {metric.metric_id}: {metric.description}; "
            f"filters={'; '.join(metric.default_filters)}; "
            f"allowed_dimensions={', '.join(metric.allowed_dimensions)}"
        )
    lines.append("Joins:")
    for join in catalog.joins:
        lines.append(
            f"- {join.join_id}: {join.from_table} -> {join.to_table} ON {join.on}"
        )
    return "\n".join(lines)


def render_generation_prompt(
    case: TheLookV2GenerationCase, *, catalog_prompt: str | None = None
) -> str:
    """Render the exact v2 SQL-only candidate prompt for one protected case."""

    server_catalog = catalog_prompt or render_catalog_prompt()
    result_contract = {
        "required_result_columns": list(case.required_result_columns),
        "exact_result_columns": True,
    }
    return "\n".join(
        [
            "### Task",
            "Generate exactly one read-only SQL query for the supplied Chinese business question.",
            "### SQL dialect\nPostgreSQL",
            "### Candidate contract",
            "- Return SQL only: no Markdown, explanation, tool call, or prose.",
            "- Generate one SELECT or WITH ... SELECT statement only.",
            "- Use only tables, columns, joins, metrics, and filters in the server-provided Catalog.",
            "- Do not invent a metric definition, join, attribution rule, filter, table, or column.",
            "- The final top-level SELECT must return exactly the server-required result columns.",
            "- The server independently enforces AST policy, readonly access, and the result contract.",
            "### Server-provided Semantic Catalog",
            server_catalog,
            "### Server-provided QuerySpec",
            json.dumps(case.query_spec, ensure_ascii=False, sort_keys=True),
            "### Server-provided Result Contract",
            json.dumps(result_contract, ensure_ascii=False, sort_keys=True),
            "### Question",
            case.question,
            "### SQL",
        ]
    )


def prompt_bundle_sha256(prompts: Sequence[str]) -> str:
    """Hash ordered prompts without persisting protected text in a safe report."""

    if not prompts or any(
        not isinstance(prompt, str) or not prompt for prompt in prompts
    ):
        raise TheLookV2MatchingError("prompt bundle must contain non-empty strings")
    return hashlib.sha256("\n\x1e\n".join(prompts).encode("utf-8")).hexdigest()


def read_raw_completions(
    path: Path, *, expected_case_ids: Sequence[str], label: str
) -> dict[str, str]:
    """Read external raw completions and prove exact case coverage, without Gold."""

    completions: dict[str, str] = {}
    for row_number, row in enumerate(_read_jsonl(path, label), 1):
        case_id = row.get("case_id")
        completion = row.get("completion")
        if (
            not isinstance(case_id, str)
            or not isinstance(completion, str)
            or not completion.strip()
        ):
            raise TheLookV2MatchingError(f"{label} row {row_number} is invalid")
        if case_id in completions:
            raise TheLookV2MatchingError(f"{label} contains duplicate case IDs")
        completions[case_id] = completion
    if list(completions) != list(expected_case_ids):
        raise TheLookV2MatchingError(f"{label} case IDs differ from frozen final test")
    return completions


def _safe_report_records(
    report: Mapping[str, Any], label: str
) -> list[Mapping[str, Any]]:
    if report.get("run_label") != label:
        raise TheLookV2MatchingError(f"{label} report has mismatched run label")
    boundaries = report.get("boundaries")
    contract = report.get("comparison_contract")
    records = report.get("records")
    if (
        not isinstance(boundaries, Mapping)
        or boundaries.get("gold_sql_read_for_generation") is not False
        or boundaries.get("production_default_unchanged") is not True
        or not isinstance(contract, Mapping)
        or not isinstance(records, list)
    ):
        raise TheLookV2MatchingError(
            f"{label} safe report violates generation isolation"
        )
    if contract.get("matching_contract_version") != MATCHING_CONTRACT_VERSION:
        raise TheLookV2MatchingError(
            f"{label} safe report has unexpected matching contract"
        )
    return records


def verify_matching_generation(
    *,
    base_report: Mapping[str, Any],
    adapter_report: Mapping[str, Any],
    base_completions: Path,
    adapter_completions: Path,
    expected_case_ids: Sequence[str],
    expected_cases_sha256: str,
    expected_manifest_sha256: str,
) -> dict[str, Any]:
    """Prove two raw completion sets form one frozen Base/Adapter pair.

    This function must run before the evaluation command is allowed to read
    Gold SQL.  It intentionally knows no database, QuerySpec, or Gold fields.
    """

    base_records = _safe_report_records(base_report, "base")
    adapter_records = _safe_report_records(adapter_report, "adapter")
    base_contract = base_report["comparison_contract"]
    adapter_contract = adapter_report["comparison_contract"]
    assert isinstance(base_contract, Mapping) and isinstance(adapter_contract, Mapping)
    if dict(base_contract) != dict(adapter_contract):
        raise TheLookV2MatchingError("Base and Adapter comparison contracts differ")
    expected_contract_fields = {
        "matching_contract_version": MATCHING_CONTRACT_VERSION,
        "dataset": EVALUATION_VERSION,
        "case_count": EXPECTED_CASES,
        "cases_jsonl_sha256": expected_cases_sha256,
        "manifest_sha256": expected_manifest_sha256,
        "prompt_version": PROMPT_VERSION,
        "model_id": EXPECTED_MODEL_ID,
        "model_revision": EXPECTED_MODEL_REVISION,
        "base_weight_mode": EXPECTED_BASE_WEIGHT_MODE,
        "gold_sql_read_for_generation": False,
        "database_rows_read_for_generation": False,
    }
    for key, expected_value in expected_contract_fields.items():
        if base_contract.get(key) != expected_value:
            raise TheLookV2MatchingError(
                f"matching contract field differs from frozen value: {key}"
            )
    expected_decode = {
        "do_sample": False,
        "num_beams": 1,
        "max_input_tokens": EXPECTED_MAX_INPUT_TOKENS,
        "max_new_tokens": EXPECTED_MAX_NEW_TOKENS,
        "seed": EXPECTED_SEED,
    }
    if base_contract.get("decode") != expected_decode:
        raise TheLookV2MatchingError(
            "matching decode parameters differ from frozen value"
        )
    if base_contract.get("prompt_bundle_sha256") != EXPECTED_PROMPT_BUNDLE_SHA256:
        raise TheLookV2MatchingError(
            "matching prompt bundle differs from frozen v2 prompt"
        )
    expected_prompt_preflight = {
        "count": EXPECTED_CASES,
        "min": EXPECTED_PROMPT_TOKEN_MIN,
        "max": EXPECTED_PROMPT_TOKEN_MAX,
        "at_input_limit": 0,
    }
    if base_contract.get("prompt_token_preflight") != expected_prompt_preflight:
        raise TheLookV2MatchingError(
            "matching prompt token preflight differs from frozen value"
        )
    base_model = base_report.get("model")
    adapter_model = adapter_report.get("model")
    if not isinstance(base_model, Mapping) or not isinstance(adapter_model, Mapping):
        raise TheLookV2MatchingError("matching safe report lacks model metadata")
    base_adapter = base_model.get("adapter")
    adapter_adapter = adapter_model.get("adapter")
    if (
        not isinstance(base_adapter, Mapping)
        or base_adapter.get("enabled") is not False
    ):
        raise TheLookV2MatchingError("base report unexpectedly has an adapter")
    if (
        not isinstance(adapter_adapter, Mapping)
        or adapter_adapter.get("enabled") is not True
    ):
        raise TheLookV2MatchingError("adapter report does not prove adapter loading")

    expected = list(expected_case_ids)
    for records, label in ((base_records, "base"), (adapter_records, "adapter")):
        record_ids = [record.get("source_id") for record in records]
        if record_ids != expected:
            raise TheLookV2MatchingError(
                f"{label} safe report case IDs differ from final test"
            )
        if any(record.get("route_state") != "answerable" for record in records):
            raise TheLookV2MatchingError(
                f"{label} safe report has a non-answerable route"
            )

    base_raw = read_raw_completions(
        base_completions, expected_case_ids=expected, label="base completions"
    )
    adapter_raw = read_raw_completions(
        adapter_completions, expected_case_ids=expected, label="adapter completions"
    )
    for report, path, raw, label in (
        (base_report, base_completions, base_raw, "base"),
        (adapter_report, adapter_completions, adapter_raw, "adapter"),
    ):
        artifacts = report.get("raw_artifacts")
        if not isinstance(artifacts, Mapping) or artifacts.get(
            "raw_completions_sha256"
        ) != sha256_file(path):
            raise TheLookV2MatchingError(
                f"{label} raw completion hash differs from its safe report"
            )
        if artifacts.get("raw_completions_outside_repository") is not True:
            raise TheLookV2MatchingError(
                f"{label} raw completion boundary is not recorded"
            )
        if len(raw) != EXPECTED_CASES:
            raise TheLookV2MatchingError(
                f"{label} completion count differs from contract"
            )

    return {
        "marker_schema_version": MATCHING_MARKER_VERSION,
        "comparison_contract": dict(base_contract),
        "case_count": EXPECTED_CASES,
        "base_safe_report_sha256": sha256_bytes(base_report),
        "adapter_safe_report_sha256": sha256_bytes(adapter_report),
        "base_raw_completions_sha256": sha256_file(base_completions),
        "adapter_raw_completions_sha256": sha256_file(adapter_completions),
        "matching_generation_verified_before_gold": True,
        "gold_sql_read_for_generation": False,
        "production_default_unchanged": True,
    }


def sha256_bytes(value: Mapping[str, Any]) -> str:
    """Return the stable digest of a JSON-safe report object."""

    rendered = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
