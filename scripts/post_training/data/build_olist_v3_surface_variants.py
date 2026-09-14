#!/usr/bin/env python3
"""Build catalog-grounded eight-form pure-Chinese overlays for admitted Olist v3.1 rows.

This is deliberately a surface-only stage: it consumes an already passing
Gold-admission release and writes no SQL, Prompt, model output, or training
examples.  Eight controlled forms remain overlays of one QuerySpec instance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any
import uuid


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.candidate_sql_generator import (
    OLIST_CANDIDATE_SQL_PROMPT_VERSION,
)  # noqa: E402
from data_analysis_agent.metric_context import OLIST_V3_WORKSPACE  # noqa: E402
from data_analysis_agent.olist_queryspec import (
    QuerySpec,
    WorkspacePin,
    validate_query_spec,
)  # noqa: E402
from data_analysis_agent.olist_surface_contract import (  # noqa: E402
    OLIST_V3_1_SURFACE_VERSION,
    OLIST_V3_1_VARIANT_IDS,
    OLIST_V3_1_VARIANT_KIND_BY_ID,
    OLIST_V3_1_VARIANT_POLICY,
    OLIST_V3_1_VARIANT_SCHEMA_VERSION,
    OLIST_V3_1_VARIANTS_PER_SEED,
    contains_latin_token,
    is_pure_zh_catalog_label,
)
from data_analysis_agent.semantic_catalog import Catalog, CatalogLoader  # noqa: E402
from scripts.post_training.data.materialize_olist_queryspecs import sha256_file  # noqa: E402


SURFACE_VERSION = OLIST_V3_1_SURFACE_VERSION
VARIANT_SCHEMA_VERSION = OLIST_V3_1_VARIANT_SCHEMA_VERSION
VARIANTS_PER_SEED = OLIST_V3_1_VARIANTS_PER_SEED
EXPECTED_ROWS = 4500
DIMENSION_LABELS = {"customer_state": "客户州", "product_category_name": "商品品类"}
GRAIN_LABELS = {
    "day": "按日",
    "week": "按周",
    "month": "按月",
    "quarter": "按季度",
    "year": "按年",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admission-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args()


def _external_dir(path: Path, *, new: bool, label: str) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError(f"{label} must stay outside the Git worktree")
    if new and resolved.exists():
        raise FileExistsError(resolved)
    if not new and not resolved.is_dir():
        raise FileNotFoundError(resolved)
    return resolved


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at {path}:{number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"JSON object required at {path}:{number}")
        rows.append(value)
    if not rows:
        raise ValueError(f"empty JSONL: {path}")
    return rows


def load_admitted_records(
    admission_dir: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Require one complete and hash-bound v3 Gold admission release."""
    admission_dir = _external_dir(admission_dir, new=False, label="Gold admission")
    manifest_path = admission_dir / "admission_assembly_manifest.json"
    records_path = admission_dir / "admitted_records.jsonl"
    manifest = _read_json(manifest_path)
    output = manifest.get("output", {}).get("admitted_records_jsonl", {})
    if (
        manifest.get("checks", {}).get("status") != "pass"
        or manifest.get("workspace")
        != WorkspacePin.current(OLIST_V3_WORKSPACE).as_dict()
        or output.get("rows") != EXPECTED_ROWS
        or output.get("sha256") != sha256_file(records_path)
    ):
        raise ValueError("admission is not a passing hash-bound 4,500-row v3 release")
    records = _read_jsonl(records_path)
    if (
        len(records) != EXPECTED_ROWS
        or len({row.get("seed_id") for row in records}) != EXPECTED_ROWS
        or any(row.get("admission_status") != "admitted" for row in records)
    ):
        raise ValueError("admitted records are incomplete, duplicate, or non-admitted")
    return manifest, records


def _metric_label(
    metric_id: str, seed_id: str, variant_index: int, catalog: Catalog
) -> str:
    metric = catalog.metrics_by_id[metric_id]
    # Only names/aliases registered in the frozen Catalog are usable language.
    # The primary v3.1 release is Chinese by contract: English aliases remain
    # valid Catalog retrieval vocabulary but belong to a future multilingual
    # overlay, not to this training surface.
    options = list(
        dict.fromkeys(
            value
            for value in (metric.name, *metric.aliases)
            if is_pure_zh_catalog_label(value)
        )
    )
    if not options:
        raise ValueError(f"metric {metric_id} has no pure-Chinese Catalog label")
    digest = hashlib.sha256(
        f"{SURFACE_VERSION}:{seed_id}:{variant_index}:{metric_id}".encode()
    ).digest()
    return options[digest[0] % len(options)]


def _period(spec: QuerySpec) -> str:
    return (
        "全部可用数据范围"
        if spec.time.mode == "all_time"
        else f"{spec.time.start}至{spec.time.end_exclusive}"
    )


def render_variants(
    spec: QuerySpec, seed_id: str, catalog: Catalog
) -> list[dict[str, str]]:
    """Return eight non-duplicated questions without changing QuerySpec meaning."""
    labels = [
        "、".join(
            _metric_label(metric, seed_id, index, catalog) for metric in spec.metric_ids
        )
        for index in range(1, VARIANTS_PER_SEED + 1)
    ]
    period = _period(spec)
    dimension = DIMENSION_LABELS.get(spec.dimension or "")
    if spec.dimension and dimension is None:
        raise ValueError(
            "unsupported QuerySpec dimension for controlled Chinese surface"
        )
    if spec.time.mode == "series":
        grain = GRAIN_LABELS.get(spec.time.grain or "")
        if grain is None:
            raise ValueError(
                "unsupported QuerySpec grain for controlled Chinese surface"
            )
        questions = [
            f"请{grain}统计{period}的{labels[0]}。",
            f"帮我看下{period}{grain}的{labels[1]}。",
            f"我想了解{period}{grain}汇总的{labels[2]}。",
            f"{period}{grain}{labels[3]}。",
            f"请按时间列出{period}{grain}的{labels[4]}，方便并列查看。",
            f"{period}内，{grain}查看{labels[5]}。",
            f"{grain}这个统计粒度下，汇总{period}的{labels[6]}。",
            f"看一下{period}{grain}的{labels[7]}。",
        ]
    elif dimension:
        questions = [
            f"请统计{period}各{dimension}的{labels[0]}。",
            f"帮我看下{period}不同{dimension}的{labels[1]}。",
            f"我想了解{period}各{dimension}汇总的{labels[2]}。",
            f"{period}各{dimension}{labels[3]}。",
            f"请按{dimension}列出{period}的{labels[4]}，方便并列查看。",
            f"{period}期间，统计各{dimension}的{labels[5]}。",
            f"各{dimension}分别汇总{period}的{labels[6]}。",
            f"看一下{period}各{dimension}的{labels[7]}。",
        ]
    else:
        questions = [
            f"请统计{period}的{labels[0]}。",
            f"帮我看下{period}的{labels[1]}。",
            f"我想了解{period}整体的{labels[2]}汇总。",
            f"{period}{labels[3]}。",
            f"请给出{period}的{labels[4]}，用于经营概览。",
            f"{period}这段时间，统计{labels[5]}。",
            f"整体口径下，汇总{period}的{labels[6]}。",
            f"看一下{period}的{labels[7]}。",
        ]
    if len(set(questions)) != VARIANTS_PER_SEED:
        raise ValueError(f"duplicate surface form: {seed_id}")
    if any(contains_latin_token(question) for question in questions):
        raise ValueError(f"pure-Chinese surface contains a Latin token: {seed_id}")
    return [
        {
            "variant_id": f"{seed_id}-{variant_id}",
            "variant_kind": OLIST_V3_1_VARIANT_KIND_BY_ID[variant_id],
            "seed_id": seed_id,
            "question": question,
        }
        for variant_id, question in zip(OLIST_V3_1_VARIANT_IDS, questions, strict=True)
    ]


def build(
    admission_dir: Path, output_dir: Path, *, generated_at: str | None = None
) -> dict[str, Any]:
    output_dir = _external_dir(output_dir, new=True, label="surface overlay output")
    admission_manifest, records = load_admitted_records(admission_dir)
    catalog = CatalogLoader(workspace=OLIST_V3_WORKSPACE).load()
    cases: list[dict[str, str]] = []
    seen_questions: set[str] = set()
    for record in sorted(records, key=lambda row: str(row["seed_id"])):
        spec = QuerySpec.from_mapping(record["query_spec"])
        validate_query_spec(spec, catalog)
        variants = render_variants(spec, str(record["seed_id"]), catalog)
        normalized = {variant["question"].strip() for variant in variants}
        if len(normalized) != VARIANTS_PER_SEED or seen_questions & normalized:
            raise ValueError("duplicate normalized question in surface release")
        seen_questions.update(normalized)
        cases.extend(variants)
    if len(cases) != EXPECTED_ROWS * VARIANTS_PER_SEED:
        raise AssertionError("surface count drifted")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.parent / f".{output_dir.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        variants_path = staging / "question_variants.json"
        payload = {
            "schema_version": VARIANT_SCHEMA_VERSION,
            "language": "zh",
            "prompt_version": OLIST_CANDIDATE_SQL_PROMPT_VERSION,
            "variant_policy": OLIST_V3_1_VARIANT_POLICY,
            "cases": cases,
        }
        variants_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result = {
            "surface_version": SURFACE_VERSION,
            "generated_at": generated_at or "unfixed-runtime-time",
            "workspace": WorkspacePin.current(OLIST_V3_WORKSPACE).as_dict(),
            "source": {
                "admission_manifest_sha256": sha256_file(
                    admission_dir / "admission_assembly_manifest.json"
                ),
                "admitted_records_sha256": admission_manifest["output"][
                    "admitted_records_jsonl"
                ]["sha256"],
            },
            "output": {
                "question_variants_json": {
                    "cases": len(cases),
                    "sha256": sha256_file(variants_path),
                }
            },
            "counts": {
                "query_instances": len(records),
                "variants_per_instance": VARIANTS_PER_SEED,
                "variants": len(cases),
                "unique_normalized_questions": len(seen_questions),
            },
            "checks": {
                "all_input_gold_admitted": True,
                "catalog_grounded_metric_labels_only": True,
                "pure_chinese_primary_surface": True,
                "eight_variants_not_counted_as_instances": True,
                "sql_generated": False,
                "model_called": False,
                "gpu_used": False,
            },
        }
        (staging / "surface_manifest.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return result


def main() -> int:
    args = parse_args()
    print(
        json.dumps(
            build(args.admission_dir, args.output_dir, generated_at=args.generated_at),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
