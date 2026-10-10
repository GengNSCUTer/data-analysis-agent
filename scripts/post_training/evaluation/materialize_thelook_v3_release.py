#!/usr/bin/env python3
"""Materialize TheLook v3 only after deterministic Gold admission.

The command makes no model call.  It processes every static seed through the
same trusted chain used by the admission pilot, records exclusions rather than
silently substituting date or wording variants, and writes raw SQL/questions
only to a newly created directory outside this repository.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any, Mapping, Sequence
import unicodedata
import uuid

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE  # noqa: E402
from data_analysis_agent.thelook_v3_coverage import TheLookV3CoverageSeed  # noqa: E402
from scripts.post_training.evaluation.admit_thelook_v3_coverage_batch import (  # noqa: E402
    admit_seed,
    execute_as_reader,
    load_seeds,
    sha256_file,
)
from data_analysis_agent.semantic_catalog import CatalogLoader  # noqa: E402
from data_analysis_agent.sql_policy import SqlPolicy  # noqa: E402


MATERIALIZATION_VERSION = "thelook-cross-schema-final-test-v3-materialization-v1"
QUESTION_VARIANT_VERSION = "thelook-question-variants-v3"
DEFAULT_SEEDS = ROOT / "data" / "fixtures" / "thelook_v3_coverage_seeds_v2.jsonl"

_METRIC_LABELS = {
    "completed_sale_amount": "已完成成交额",
    "completed_item_count": "已完成商品行数",
    "completed_order_count": "已完成订单数",
    "average_order_value": "平均完成订单金额",
    "average_items_per_completed_order": "平均每完成订单商品行数",
    "cancelled_order_count": "已取消订单数",
    "returned_order_count": "已退货订单数",
    "average_fulfillment_days": "平均履约天数",
    "average_dispatch_days": "平均发货时长",
    "average_transit_days": "平均运输时长",
    "average_post_delivery_return_days": "平均送达后退货等待时长",
    "return_rate": "退货率",
    "completed_customer_count": "已完成客户数",
    "received_inventory_unit_count": "入库库存单元数",
    "sold_inventory_unit_count": "已售库存单元数",
    "current_unsold_inventory_unit_count": "当前未售库存单元数",
    "average_days_to_sale": "平均售出周期",
    "event_count": "事件数",
    "unique_session_count": "去重会话数",
    "registered_user_count": "注册用户数",
}
_DIMENSION_LABELS = {
    "customer_state": "客户州地区",
    "customer_city": "客户城市",
    "customer_traffic_source": "客户注册流量来源",
    "product_category": "商品品类",
    "product_brand": "商品品牌",
    "product_department": "商品部门",
    "distribution_center": "配送中心",
    "event_type": "事件类型",
    "event_browser": "浏览器",
    "event_traffic_source": "事件流量来源",
    "user_country": "用户国家",
    "user_traffic_source": "用户注册流量来源",
    "user_state": "用户注册州地区",
}
_GRAIN_LABELS = {"day": "日", "week": "周", "month": "月", "quarter": "季度", "year": "年"}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds-jsonl", type=Path, default=DEFAULT_SEEDS)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args(argv)


def _external_new_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError("TheLook v3 materialization output must stay outside the Git worktree")
    if resolved.exists():
        raise FileExistsError(resolved)
    return resolved


def _period_text(seed: TheLookV3CoverageSeed) -> str:
    query_time = seed.query_spec.time
    if query_time.mode == "all_time":
        return "全部可用时间"
    return f"{query_time.start}至{query_time.end_exclusive}不含结束日"


def question_variants(seed: TheLookV3CoverageSeed) -> dict[str, str]:
    """Return eight controlled Chinese forms for one approved QuerySpec.

    Surface wording never affects the QuerySpec, Gold SQL, program signature
    or case count.  All labels come from the frozen metric/dimension registry.
    """

    spec = seed.query_spec
    metrics = "、".join(_METRIC_LABELS[metric] for metric in spec.metric_ids)
    period = _period_text(seed)
    if spec.result_shape == "scalar":
        forms = (
            f"请汇总{period}的{metrics}。",
            f"想了解{period}的{metrics}情况。",
            f"作为经营概览，查看{period}的{metrics}。",
            f"{period}的{metrics}是多少？",
            f"请输出{metrics}在{period}的汇总结果。",
            f"先看时间范围：{period}，统计{metrics}。",
            f"从整体经营切面分析{period}的{metrics}。",
            f"按已冻结口径计算{period}的{metrics}。",
        )
    elif spec.result_shape == "dimension_grouped":
        dimension = _DIMENSION_LABELS[spec.dimension or ""]
        forms = (
            f"请按{dimension}统计{period}的{metrics}。",
            f"想比较不同{dimension}在{period}的{metrics}。",
            f"作为经营概览，查看{period}各{dimension}的{metrics}。",
            f"{period}按{dimension}拆分后的{metrics}分别是多少？",
            f"请输出{period}各{dimension}的{metrics}汇总结果。",
            f"先看时间范围：{period}，按{dimension}统计{metrics}。",
            f"从{dimension}这个分析切面查看{period}的{metrics}。",
            f"按已冻结口径，给出{period}各{dimension}的{metrics}。",
        )
    else:
        grain = _GRAIN_LABELS[spec.time.grain or ""]
        forms = (
            f"请按{grain}展示{period}的{metrics}。",
            f"想查看{period}内{metrics}的{grain}度变化。",
            f"作为经营概览，给出{period}{metrics}的{grain}度时间序列。",
            f"{period}的{metrics}按{grain}如何分布？",
            f"请输出{period}内{metrics}的{grain}度汇总结果。",
            f"先看时间范围：{period}，按{grain}统计{metrics}。",
            f"从时间变化切面分析{period}的{metrics}。",
            f"按已冻结口径生成{period}{metrics}的{grain}度序列。",
        )
    # Keep v3 surface identities independent from the historical v2 wording.
    # This prefix is presentation-only and does not add a filter, ranking,
    # causal claim, or any SQL behavior; it prevents a reused QuerySpec from
    # accidentally reusing a v2 primary natural-language question.
    return {
        f"v{index}": f"业务分析请求：{text}"
        for index, text in enumerate(forms, 1)
    }


def normalized_question(question: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", question).casefold()
        if not character.isspace() and not unicodedata.category(character).startswith("P")
    )


def select_primary_variants(
    seeds: Sequence[TheLookV3CoverageSeed],
    quota: Mapping[str, Any],
) -> dict[str, str]:
    """Allocate exact global variant quotas while balancing family×shape buckets.

    Each bucket gives every variant ``floor(n/8)`` rows.  Its remaining slots
    are allocated to variants with the largest remaining global demand, with a
    hash only as deterministic tie breaker.  Thus per-bucket counts differ by
    at most one and the requested global quota is exact.
    """

    variants = tuple(sorted(quota))
    if len(variants) != 8 or sum(int(quota[item]) for item in variants) != len(seeds):
        raise ValueError("surface quota does not match the admitted case count")
    buckets: dict[tuple[str, str], list[TheLookV3CoverageSeed]] = defaultdict(list)
    for seed in seeds:
        buckets[(seed.scenario_family_id, seed.query_spec.result_shape)].append(seed)
    baseline = Counter()
    extras: dict[tuple[str, str], int] = {}
    for bucket, rows in buckets.items():
        base, remainder = divmod(len(rows), len(variants))
        for variant in variants:
            baseline[variant] += base
        extras[bucket] = remainder
    remaining = {variant: int(quota[variant]) - baseline[variant] for variant in variants}
    if any(value < 0 for value in remaining.values()) or sum(remaining.values()) != sum(extras.values()):
        raise ValueError("surface quota cannot satisfy stratified bucket baseline")
    selected_extras: dict[tuple[str, str], set[str]] = {}
    for bucket in sorted(buckets):
        amount = extras[bucket]
        ranking = sorted(
            variants,
            key=lambda variant: (
                -remaining[variant],
                hashlib.sha256(f"{bucket[0]}|{bucket[1]}|{variant}".encode()).hexdigest(),
            ),
        )
        chosen = ranking[:amount]
        if any(remaining[variant] <= 0 for variant in chosen):
            raise ValueError("surface quota allocation exhausted an unavailable variant")
        for variant in chosen:
            remaining[variant] -= 1
        selected_extras[bucket] = set(chosen)
    if any(remaining.values()):
        raise ValueError("surface quota allocation did not meet exact global target")
    assignment: dict[str, str] = {}
    for bucket in sorted(buckets):
        rows = sorted(
            buckets[bucket],
            key=lambda seed: hashlib.sha256(seed.seed_id.encode()).hexdigest(),
        )
        ordered_variants = [
            *sorted(selected_extras[bucket]),
            *[variant for variant in variants if variant not in selected_extras[bucket]],
        ]
        base, _ = divmod(len(rows), len(variants))
        cursor = 0
        for variant in ordered_variants:
            count = base + int(variant in selected_extras[bucket])
            for seed in rows[cursor : cursor + count]:
                assignment[seed.seed_id] = variant
            cursor += count
        if cursor != len(rows):
            raise AssertionError("surface assignment failed to consume a bucket")
    if Counter(assignment.values()) != Counter({key: int(value) for key, value in quota.items()}):
        raise AssertionError("surface assignment drifted from the exact global quota")
    return assignment


def materialize(
    seeds_path: Path,
    output_dir: Path,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Execute all candidates; a non-passing family is reported, never hidden."""

    output_path = _external_new_dir(output_dir)
    seeds = load_seeds(seeds_path)
    catalog = CatalogLoader(THELOOK_V2_WORKSPACE).load()
    policy = SqlPolicy(workspace=THELOOK_V2_WORKSPACE)
    admission_records = [
        admit_seed(seed, catalog=catalog, policy=policy, execute=execute_as_reader)
        for seed in seeds
    ]
    admitted_ids = {
        str(record["seed_id"])
        for record in admission_records
        if record["admission_status"] == "admitted"
    }
    admitted_seeds = [seed for seed in seeds if seed.seed_id in admitted_ids]
    contract = json.loads(
        (ROOT / "data" / "fixtures" / "thelook_v3_coverage_contract_v2.json").read_text(
            encoding="utf-8"
        )
    )
    variants_by_seed: dict[str, str] = {}
    if len(admitted_seeds) == len(seeds):
        variants_by_seed = select_primary_variants(
            admitted_seeds, contract["surface_contract"]["primary_variant_global_quota"]
        )
    record_by_seed = {str(record["seed_id"]): record for record in admission_records}
    cases: list[dict[str, Any]] = []
    for index, seed in enumerate(admitted_seeds, 1):
        record = record_by_seed[seed.seed_id]
        variants = question_variants(seed)
        variant_id = variants_by_seed.get(seed.seed_id)
        if variant_id is None:
            # An incomplete candidate release must not pretend to satisfy the
            # final 8-way surface quota.  Keep its rows reviewable, but mark it
            # as non-release material in the manifest below.
            variant_id = "v1"
        cases.append(
            {
                "case_id": f"thelook-final-v3-{index:03d}",
                "split": "cross_schema_final_test",
                "language": "zh",
                "question_variant_version": QUESTION_VARIANT_VERSION,
                "primary_variant_id": variant_id,
                "question": variants[variant_id],
                "question_variants": variants,
                "seed_id": seed.seed_id,
                "scenario_family_id": seed.scenario_family_id,
                "program_signature": seed.program_signature,
                "risk_tags": list(seed.risk_tags),
                "window_candidate": seed.window_candidate,
                "query_spec": record["query_spec"],
                "gold_sql": record["gold_sql"],
                "gold_sql_sha256": record["gold_sql_sha256"],
                "renderer_version": record["gold_renderer_version"],
                "required_result_columns": record["query_spec"]["required_result_columns"],
                "execution": {
                    "policy_status": record["policy_status"],
                    "policy_final_sql_sha256": record["policy_final_sql_sha256"],
                    "policy_limit_applied": record["policy_limit_applied"],
                    "policy_row_limit": record["policy_row_limit"],
                    "reader_role": record["reader_role"],
                    "result_contract": record["result_contract"],
                    "result_validation": record["result_validation"],
                },
            }
        )
    family_target = {
        item["id"]: int(item["target_cases"])
        for item in contract["scenario_families"]
    }
    family_actual = Counter(seed.scenario_family_id for seed in admitted_seeds)
    family_reduction = {
        family: {
            "target": target,
            "admitted": family_actual[family],
            "reduced_by": target - family_actual[family],
        }
        for family, target in sorted(family_target.items())
        if family_actual[family] != target
    }
    exclusions = [
        record for record in admission_records if record["admission_status"] != "admitted"
    ]
    generated = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = output_path.parent / f".{output_path.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        admission_path = staging / "admission_records.jsonl"
        cases_path = staging / "cases.jsonl"
        exclusion_path = staging / "exclusions.jsonl"
        for path, rows in ((admission_path, admission_records), (cases_path, cases), (exclusion_path, exclusions)):
            path.write_text(
                "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
                encoding="utf-8",
            )
        manifest = {
            "materialization_version": MATERIALIZATION_VERSION,
            "evaluation_version": contract["release"]["evaluation_version"],
            "generated_at": generated,
            "workspace": {
                "workspace_id": THELOOK_V2_WORKSPACE.workspace_id,
                "catalog_version": THELOOK_V2_WORKSPACE.catalog_version,
                "dataset_version": THELOOK_V2_WORKSPACE.dataset_version,
                "metric_version": THELOOK_V2_WORKSPACE.metric_version,
                "policy_version": THELOOK_V2_WORKSPACE.policy_version,
            },
            "source": {
                "seed_fixture": str(seeds_path.resolve()),
                "seed_fixture_sha256": sha256_file(seeds_path),
                "target_cases": len(seeds),
            },
            "output": {
                "admission_records_jsonl": {"rows": len(admission_records), "sha256": sha256_file(admission_path)},
                "cases_jsonl": {"rows": len(cases), "sha256": sha256_file(cases_path)},
                "exclusions_jsonl": {"rows": len(exclusions), "sha256": sha256_file(exclusion_path)},
            },
            "counts": {
                "admitted": len(cases),
                "rejected": len(exclusions),
                "admitted_by_family": dict(sorted(family_actual.items())),
                "family_reductions": family_reduction,
                "failure_types": dict(
                    sorted(Counter(str(row.get("failure_type", "unknown")) for row in exclusions).items())
                ),
            },
            "checks": {
                "status": "pass" if len(cases) == len(seeds) else "blocked_by_admission",
                "all_queryspec_renderer_policy_reader_result_contract": not exclusions,
                "all_rows_within_200_display_budget": not exclusions,
                "all_scenario_family_quotas_met": not family_reduction,
                "surface_quota_assigned": bool(variants_by_seed),
                "questions_are_generated_only_after_admission": True,
                "model_called": False,
                "protected_after_release": not exclusions,
            },
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(output_path)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = materialize(args.seeds_jsonl, args.output_dir, generated_at=args.generated_at)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if manifest["checks"]["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
