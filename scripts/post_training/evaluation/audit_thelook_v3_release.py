#!/usr/bin/env python3
"""Audit a materialized TheLook v3 release against its frozen contract."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence
import unicodedata

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE  # noqa: E402
from data_analysis_agent.thelook_v3_coverage import (  # noqa: E402
    derive_thelook_v3_program_signature,
    derive_thelook_v3_risk_tags,
    load_thelook_v3_coverage_contract,
    validate_thelook_v3_coverage_seed,
)
from data_analysis_agent.thelook_v3_release import (  # noqa: E402
    assert_thelook_v3_gold_uses_analytics_views_only,
    build_thelook_v3_result_contract,
)
from data_analysis_agent.thelook_v2_queryspec import (  # noqa: E402
    METRIC_FACT_DOMAINS,
    TheLookV2QuerySpec,
    validate_thelook_v2_query_spec,
)
from data_analysis_agent.thelook_v2_renderer import render_thelook_v2_gold_sql  # noqa: E402
from data_analysis_agent.semantic_catalog import CatalogLoader  # noqa: E402
from data_analysis_agent.sql_policy import PolicyViolation, SqlPolicy  # noqa: E402


class TheLookV3ReleaseAuditError(ValueError):
    """A materialized v3 release does not satisfy a release contract."""


DEFAULT_SEEDS = ROOT / "data" / "fixtures" / "thelook_v3_coverage_seeds_v2.jsonl"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument(
        "--v2-cases",
        type=Path,
        default=Path(
            "/disk2/gengnan/data-analysis-agent-data/evals/"
            "thelook-cross-schema-final-test-v2-20260910/cases.jsonl"
        ),
    )
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TheLookV3ReleaseAuditError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise TheLookV3ReleaseAuditError(f"expected an object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TheLookV3ReleaseAuditError(f"missing JSONL: {path}") from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TheLookV3ReleaseAuditError(f"invalid JSON at {path}:{line_number}") from exc
        if not isinstance(row, dict):
            raise TheLookV3ReleaseAuditError(f"row at {path}:{line_number} is not an object")
        rows.append(row)
    return rows


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise TheLookV3ReleaseAuditError(f"missing file for hash audit: {path}") from exc
    return digest.hexdigest()


def _normalized_question(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if not character.isspace() and not unicodedata.category(character).startswith("P")
    )


def _assert_pure_chinese_sql_free(question: str) -> None:
    if not isinstance(question, str) or not question.strip():
        raise TheLookV3ReleaseAuditError("primary question is empty")
    if re.search(r"[A-Za-z]", question):
        raise TheLookV3ReleaseAuditError(f"primary question contains ASCII text: {question}")
    if re.search(r"\b(select|from|where|join|group|order|limit|analytics)\b", question, re.I):
        raise TheLookV3ReleaseAuditError("primary question contains SQL leakage")


def _v2_identity(path: Path) -> tuple[set[str], set[str], set[str]]:
    rows = _read_jsonl(path)
    query_ids: set[str] = set()
    sql_hashes: set[str] = set()
    questions: set[str] = set()
    for row in rows:
        spec = row.get("query_spec")
        if not isinstance(spec, Mapping) or not isinstance(spec.get("query_spec_id"), str):
            raise TheLookV3ReleaseAuditError("v2 case lacks QuerySpec identity")
        query_ids.add(str(spec["query_spec_id"]))
        if isinstance(row.get("gold_sql_sha256"), str):
            sql_hashes.add(str(row["gold_sql_sha256"]))
        if isinstance(row.get("question"), str):
            questions.add(_normalized_question(row["question"]))
    return query_ids, sql_hashes, questions


def _seed_index(path: Path) -> dict[str, Any]:
    """Load the exact static source and reject any release-side substitution."""

    index: dict[str, Any] = {}
    for raw in _read_jsonl(path):
        try:
            seed = validate_thelook_v3_coverage_seed(raw)
        except ValueError as exc:
            raise TheLookV3ReleaseAuditError("TheLook v3 seed fixture is invalid") from exc
        if seed.seed_id in index:
            raise TheLookV3ReleaseAuditError("TheLook v3 seed fixture has duplicate seed IDs")
        index[seed.seed_id] = seed
    return index


def _assert_manifest_hashes(
    manifest: Mapping[str, Any], release_dir: Path, *, expected_cases: int
) -> None:
    """Recompute source/output hashes pinned by the materialization manifest."""

    source = manifest.get("source")
    if not isinstance(source, Mapping):
        raise TheLookV3ReleaseAuditError("manifest lacks source evidence")
    if source.get("seed_fixture") != str(DEFAULT_SEEDS.resolve()):
        raise TheLookV3ReleaseAuditError("manifest seed fixture path drifted")
    if source.get("seed_fixture_sha256") != _sha256_file(DEFAULT_SEEDS):
        raise TheLookV3ReleaseAuditError("manifest seed fixture hash drifted")
    if source.get("target_cases") != expected_cases:
        raise TheLookV3ReleaseAuditError("manifest target case count drifted")

    output = manifest.get("output")
    if not isinstance(output, Mapping):
        raise TheLookV3ReleaseAuditError("manifest lacks output hash evidence")
    expected_files = {
        "admission_records_jsonl": ("admission_records.jsonl", expected_cases),
        "cases_jsonl": ("cases.jsonl", expected_cases),
        "exclusions_jsonl": ("exclusions.jsonl", 0),
    }
    for key, (filename, rows) in expected_files.items():
        evidence = output.get(key)
        if not isinstance(evidence, Mapping):
            raise TheLookV3ReleaseAuditError(f"manifest lacks hash evidence for {filename}")
        path = release_dir / filename
        if evidence.get("rows") != rows or evidence.get("sha256") != _sha256_file(path):
            raise TheLookV3ReleaseAuditError(f"manifest hash or row count drifted for {filename}")


def audit_release(release_dir: Path, v2_cases: Path) -> dict[str, Any]:
    contract = load_thelook_v3_coverage_contract()
    manifest = _read_json(release_dir / "manifest.json")
    cases = _read_jsonl(release_dir / "cases.jsonl")
    exclusions = _read_jsonl(release_dir / "exclusions.jsonl")
    expected = int(contract["release"]["target_cases"])
    if len(cases) != expected or exclusions:
        raise TheLookV3ReleaseAuditError(
            f"release must contain {expected} admitted cases and no exclusions"
        )
    _assert_manifest_hashes(manifest, release_dir, expected_cases=expected)
    seeds = _seed_index(DEFAULT_SEEDS)
    if len(seeds) != expected:
        raise TheLookV3ReleaseAuditError("seed fixture row count does not match release target")
    catalog = CatalogLoader(THELOOK_V2_WORKSPACE).load()
    policy = SqlPolicy(workspace=THELOOK_V2_WORKSPACE)
    query_ids: list[str] = []
    sql_hashes: list[str] = []
    case_ids: list[str] = []
    primary_questions: list[str] = []
    family_counts: Counter[str] = Counter()
    fact_counts: Counter[str] = Counter()
    shape_counts: Counter[str] = Counter()
    grain_counts: Counter[str] = Counter()
    dimension_counts: Counter[str] = Counter()
    multiplicity_counts: Counter[int] = Counter()
    risk_counts: Counter[str] = Counter()
    variant_counts: Counter[str] = Counter()
    bucket_counts: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for row in cases:
        case_id = row.get("case_id")
        spec_payload = row.get("query_spec")
        sql = row.get("gold_sql")
        sql_hash = row.get("gold_sql_sha256")
        if not isinstance(case_id, str) or not isinstance(spec_payload, Mapping) or not isinstance(sql, str):
            raise TheLookV3ReleaseAuditError("case identity or Gold SQL is malformed")
        spec = validate_thelook_v2_query_spec(TheLookV2QuerySpec.from_mapping(spec_payload), catalog)
        seed_id = row.get("seed_id")
        if not isinstance(seed_id, str) or seed_id not in seeds:
            raise TheLookV3ReleaseAuditError(f"case has an unknown seed: {case_id}")
        seed = seeds[seed_id]
        if (
            spec_payload != seed.query_spec.as_dict()
            or row.get("scenario_family_id") != seed.scenario_family_id
            or row.get("program_signature") != derive_thelook_v3_program_signature(spec)
            or row.get("risk_tags") != list(derive_thelook_v3_risk_tags(spec, contract))
            or row.get("window_candidate") != seed.window_candidate
        ):
            raise TheLookV3ReleaseAuditError(f"release semantics drifted from seed: {case_id}")
        if not isinstance(sql_hash, str) or sql_hash != _sha256_text(sql):
            raise TheLookV3ReleaseAuditError(f"Gold SQL hash drifted for {case_id}")
        if spec.query_spec_id != spec_payload.get("query_spec_id"):
            raise TheLookV3ReleaseAuditError(f"QuerySpec identity drifted for {case_id}")
        artifact = render_thelook_v2_gold_sql(spec, catalog)
        if (
            sql != artifact.sql
            or sql_hash != artifact.sql_sha256
            or row.get("renderer_version") != artifact.renderer_version
        ):
            raise TheLookV3ReleaseAuditError(f"renderer evidence drifted for {case_id}")
        assert_thelook_v3_gold_uses_analytics_views_only(sql)
        execution = row.get("execution")
        if not isinstance(execution, Mapping) or execution.get("reader_role") != THELOOK_V2_WORKSPACE.reader_role:
            raise TheLookV3ReleaseAuditError(f"reader-role evidence missing for {case_id}")
        try:
            decision = policy.evaluate(sql, role="analyst")
        except PolicyViolation as exc:
            raise TheLookV3ReleaseAuditError(f"stored Gold SQL no longer passes SqlPolicy: {case_id}") from exc
        if (
            execution.get("policy_status") != decision.status
            or execution.get("policy_final_sql_sha256") != _sha256_text(decision.final_sql)
            or execution.get("policy_limit_applied") != decision.policy_limit_applied
            or execution.get("policy_row_limit") != decision.row_limit
        ):
            raise TheLookV3ReleaseAuditError(f"SqlPolicy evidence drifted for {case_id}")
        if row.get("required_result_columns") != list(spec.required_result_columns):
            raise TheLookV3ReleaseAuditError(f"result column identity drifted for {case_id}")
        if execution.get("result_contract") != build_thelook_v3_result_contract(spec, catalog).as_evidence():
            raise TheLookV3ReleaseAuditError(f"result contract evidence drifted for {case_id}")
        validation = execution.get("result_validation")
        if not isinstance(validation, Mapping) or validation.get("state") != "valid" or validation.get("truncated"):
            raise TheLookV3ReleaseAuditError(f"result contract evidence is not valid for {case_id}")
        row_count = validation.get("row_count")
        if not isinstance(row_count, int) or row_count < 0 or row_count > 200:
            raise TheLookV3ReleaseAuditError(f"display row budget evidence drifted for {case_id}")
        variants = row.get("question_variants")
        if not isinstance(variants, Mapping) or set(variants) != set(contract["surface_contract"]["variant_ids"]):
            raise TheLookV3ReleaseAuditError(f"eight surface variants missing for {case_id}")
        variant_id = row.get("primary_variant_id")
        question = row.get("question")
        if variant_id not in variants or question != variants[variant_id]:
            raise TheLookV3ReleaseAuditError(f"primary surface mismatch for {case_id}")
        _assert_pure_chinese_sql_free(str(question))
        case_ids.append(case_id)
        query_ids.append(spec.query_spec_id)
        sql_hashes.append(sql_hash)
        primary_questions.append(_normalized_question(str(question)))
        family = str(row.get("scenario_family_id"))
        family_counts[family] += 1
        shape_counts[spec.result_shape] += 1
        fact_counts[METRIC_FACT_DOMAINS[spec.metric_ids[0]]] += 1
        multiplicity_counts[len(spec.metric_ids)] += 1
        if spec.result_shape == "time_series":
            grain_counts[str(spec.time.grain)] += 1
        if spec.result_shape == "dimension_grouped":
            dimension_counts[str(spec.dimension)] += 1
        risk_counts.update(row.get("risk_tags", []))
        variant_counts[str(variant_id)] += 1
        bucket_counts[(family, spec.result_shape)][str(variant_id)] += 1
    if len(set(case_ids)) != expected or len(set(query_ids)) != expected or len(set(sql_hashes)) != expected:
        raise TheLookV3ReleaseAuditError("case, QuerySpec and canonical Gold identities must be unique")
    if len(set(primary_questions)) != expected:
        raise TheLookV3ReleaseAuditError("primary questions must be unique within v3")
    quotas = contract["case_quotas"]
    checks = {
        "case_ids": dict(case_count=len(cases), unique=len(set(case_ids)) == expected),
        "fact_domains": dict(sorted(fact_counts.items())),
        "result_shapes": dict(sorted(shape_counts.items())),
        "time_series_grains": dict(sorted(grain_counts.items())),
        "dimensions": dict(sorted(dimension_counts.items())),
        "metric_multiplicity": dict(sorted((str(key), value) for key, value in multiplicity_counts.items())),
        "scenario_families": dict(sorted(family_counts.items())),
        "risk_tags": dict(sorted(risk_counts.items())),
        "primary_variants": dict(sorted(variant_counts.items())),
    }
    if dict(fact_counts) != {key: int(value) for key, value in quotas["fact_domains"].items()}:
        raise TheLookV3ReleaseAuditError("fact-domain quotas drifted")
    if dict(shape_counts) != {key: int(value) for key, value in quotas["result_shapes"].items()}:
        raise TheLookV3ReleaseAuditError("result-shape quotas drifted")
    if dict(grain_counts) != {key: int(value) for key, value in quotas["time_series_grains"].items()}:
        raise TheLookV3ReleaseAuditError("time-series grain quotas drifted")
    if dict(dimension_counts) != {key: int(value) for key, value in quotas["dimension_grouped"].items()}:
        raise TheLookV3ReleaseAuditError("dimension quotas drifted")
    expected_mult = {
        "1": int(quotas["metric_multiplicity"]["one_metric"]),
        "2": int(quotas["metric_multiplicity"]["two_metrics"]),
        "3": int(quotas["metric_multiplicity"]["three_metrics"]),
        "4": int(quotas["metric_multiplicity"]["four_metrics"]),
    }
    if dict(sorted((str(key), value) for key, value in multiplicity_counts.items())) != expected_mult:
        raise TheLookV3ReleaseAuditError("metric multiplicity quotas drifted")
    expected_families = {item["id"]: int(item["target_cases"]) for item in contract["scenario_families"]}
    if dict(family_counts) != expected_families:
        raise TheLookV3ReleaseAuditError("scenario-family quotas drifted")
    for tag, minimum in contract["risk_tag_minimum_case_counts"].items():
        if risk_counts[tag] < int(minimum):
            raise TheLookV3ReleaseAuditError(f"risk-tag quota drifted: {tag}")
    surface = contract["surface_contract"]
    expected_variants = {key: int(value) for key, value in surface["primary_variant_global_quota"].items()}
    if dict(variant_counts) != expected_variants:
        raise TheLookV3ReleaseAuditError("primary surface quotas drifted")
    for bucket, counts in bucket_counts.items():
        values = [counts.get(variant, 0) for variant in contract["surface_contract"]["variant_ids"]]
        if max(values) - min(values) > 1:
            raise TheLookV3ReleaseAuditError(f"surface bucket is imbalanced: {bucket}")
    v2_query_ids, v2_sql_hashes, v2_questions = _v2_identity(v2_cases)
    query_overlap = set(query_ids) & v2_query_ids
    sql_overlap = set(sql_hashes) & v2_sql_hashes
    question_overlap = set(primary_questions) & v2_questions
    allowed = set(contract["identity_and_reuse"]["allowed_v2_reuse_scenario_families"])
    overlapping_families = {
        str(row["scenario_family_id"])
        for row in cases
        if row["query_spec"]["query_spec_id"] in query_overlap
    }
    if len(query_overlap) != int(contract["identity_and_reuse"]["expected_v2_query_spec_id_overlap"]):
        raise TheLookV3ReleaseAuditError("v2 QuerySpec overlap count drifted")
    if len(sql_overlap) != int(contract["identity_and_reuse"]["expected_v2_canonical_gold_sql_sha256_overlap"]):
        raise TheLookV3ReleaseAuditError("v2 canonical Gold SQL overlap count drifted")
    if question_overlap:
        raise TheLookV3ReleaseAuditError("v2/v3 primary question overlap is not allowed")
    if overlapping_families != allowed:
        raise TheLookV3ReleaseAuditError("v2 overlap escaped approved reuse families")
    result = {
        "audit_version": "thelook-v3-release-audit-v1",
        "status": "pass",
        "release_dir": str(release_dir.resolve()),
        "manifest_file_sha256": _sha256_file(release_dir / "manifest.json"),
        "manifest_canonical_json_sha256": _sha256_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        ),
        "hash_integrity": {
            "seed_fixture": True,
            "materialized_files": True,
            "query_spec_ids": True,
            "renderer_gold_sql": True,
            "sql_policy_final_sql": True,
        },
        "release_eligible": True,
        "checks": checks,
        "v2_overlap": {
            "query_spec_ids": len(query_overlap),
            "canonical_gold_sql_sha256": len(sql_overlap),
            "primary_questions": len(question_overlap),
            "families": sorted(overlapping_families),
        },
        "gold_sql_access": {"analytics_only": True, "raw_schema_access": False},
    }
    return result


def audit_blocked_attempt(release_dir: Path, v2_cases: Path, reason: str) -> dict[str, Any]:
    """Record a useful audit when admission correctly blocks a release.

    A failed quality gate must still leave enough evidence to decide whether
    the issue is a hash/identity fault, a coverage shortfall, or a runtime
    display constraint.  This is explicitly *not* a protected-release pass.
    """

    cases = _read_jsonl(release_dir / "cases.jsonl")
    admissions = _read_jsonl(release_dir / "admission_records.jsonl")
    exclusions = _read_jsonl(release_dir / "exclusions.jsonl")
    v2_query_ids, v2_sql_hashes, v2_questions = _v2_identity(v2_cases)
    case_ids: set[str] = set()
    query_ids: set[str] = set()
    sql_hashes: set[str] = set()
    question_values: set[str] = set()
    fact_counts: Counter[str] = Counter()
    shape_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    variant_counts: Counter[str] = Counter()
    hash_failures: list[str] = []
    sql_access_failures: list[str] = []
    for row in cases:
        case_id = row.get("case_id")
        spec_payload = row.get("query_spec")
        sql = row.get("gold_sql")
        if not isinstance(case_id, str) or not isinstance(spec_payload, Mapping) or not isinstance(sql, str):
            hash_failures.append(str(case_id))
            continue
        try:
            spec = TheLookV2QuerySpec.from_mapping(spec_payload)
            if spec.query_spec_id != spec.expected_query_spec_id():
                hash_failures.append(case_id)
            if row.get("gold_sql_sha256") != _sha256_text(sql):
                hash_failures.append(case_id)
            assert_thelook_v3_gold_uses_analytics_views_only(sql)
        except (ValueError, TheLookV3ReleaseAuditError):
            sql_access_failures.append(case_id)
            continue
        case_ids.add(case_id)
        query_ids.add(spec.query_spec_id)
        sql_hashes.add(str(row["gold_sql_sha256"]))
        question = str(row.get("question", ""))
        try:
            _assert_pure_chinese_sql_free(question)
        except TheLookV3ReleaseAuditError:
            hash_failures.append(case_id)
        question_values.add(_normalized_question(question))
        fact_counts[METRIC_FACT_DOMAINS[spec.metric_ids[0]]] += 1
        shape_counts[spec.result_shape] += 1
        family_counts[str(row.get("scenario_family_id"))] += 1
        variant_counts[str(row.get("primary_variant_id"))] += 1
    admission_hash_failures = [
        str(row.get("seed_id"))
        for row in admissions
        if row.get("admission_status") == "admitted"
        and row.get("gold_sql_sha256") != _sha256_text(str(row.get("gold_sql", "")))
    ]
    return {
        "audit_version": "thelook-v3-release-audit-v1",
        "status": "blocked_by_admission",
        "release_dir": str(release_dir.resolve()),
        "release_blocker": reason,
        "recomputed_identity": {
            "case_ids_unique": len(case_ids) == len(cases),
            "query_spec_ids_unique": len(query_ids) == len(cases),
            "canonical_gold_sql_sha256_unique": len(sql_hashes) == len(cases),
            "primary_questions_unique_within_attempt": len(question_values) == len(cases),
            "case_or_gold_hash_failures": hash_failures,
            "admission_gold_hash_failures": admission_hash_failures,
        },
        "actual_coverage": {
            "admitted_cases": len(cases),
            "excluded_candidates": len(exclusions),
            "fact_domains": dict(sorted(fact_counts.items())),
            "result_shapes": dict(sorted(shape_counts.items())),
            "scenario_families": dict(sorted(family_counts.items())),
            "primary_variants": dict(sorted(variant_counts.items())),
            "exclusion_reasons": dict(
                sorted(Counter(str(row.get("failure_type", "unknown")) for row in exclusions).items())
            ),
            "exclusion_dimensions": dict(
                sorted(
                    Counter(
                        str(row.get("query_spec", {}).get("dimension"))
                        for row in exclusions
                    ).items()
                )
            ),
        },
        "v2_overlap": {
            "query_spec_ids": len(query_ids & v2_query_ids),
            "canonical_gold_sql_sha256": len(sql_hashes & v2_sql_hashes),
            "primary_questions": len(question_values & v2_questions),
        },
        "gold_sql_access": {
            "analytics_only": not sql_access_failures,
            "raw_schema_access": bool(sql_access_failures),
            "failed_case_ids": sql_access_failures,
        },
        "release_eligible": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = audit_release(args.release_dir, args.v2_cases)
    except TheLookV3ReleaseAuditError as exc:
        result = audit_blocked_attempt(args.release_dir, args.v2_cases, str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    (args.release_dir / "release_audit.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
