#!/usr/bin/env python3
"""Materialize the Olist + TheLook multi-schema SQL-only SFT release.

Only admitted Olist/TheLook train and validation assets are accepted.  The
script does not load either protected final test as a training source, does not
render Gold SQL, and does not access PostgreSQL.  It re-renders a shared outer
prompt, chooses exactly one of eight controlled Chinese questions for every
new TheLook semantic instance, and refuses silent tokenizer truncation.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from typing import Any
import unicodedata
import uuid

ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from data_analysis_agent.metric_context import OLIST_V3_WORKSPACE  # noqa: E402
from data_analysis_agent.multi_schema_sql_only_sft import (  # noqa: E402
    MULTI_SCHEMA_SQL_ONLY_PROMPT_VERSION,
    build_result_contract_payload,
    render_catalog_payload,
    render_multi_schema_prompt,
    training_text,
)
from data_analysis_agent.olist_queryspec import QuerySpec, validate_query_spec  # noqa: E402
from data_analysis_agent.semantic_catalog import Catalog, CatalogLoader  # noqa: E402
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE  # noqa: E402
from data_analysis_agent.thelook_v2_queryspec import (  # noqa: E402
    TheLookV2QuerySpec,
    validate_thelook_v2_query_spec,
)


RELEASE_VERSION = "multi-schema-ecommerce-sql-only-sft-v1-r1"
QUESTION_VARIANT_VERSION = "thelook-sft-controlled-surface-v1"
MAX_SEQUENCE_LENGTH = 3072
VARIANT_IDS = tuple(f"v{index}" for index in range(1, 9))
DEFAULT_OLIST_ROOT = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/olist-v3-balanced-release-v1.1"
)
DEFAULT_THELOOK_ROOT = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/thelook-sql-only-sft-v1-r4"
)
DEFAULT_OUTPUT = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/multi-schema-ecommerce-sql-only-v1-r1"
)
DEFAULT_TOKENIZER = Path(
    "/disk2/gengnan/data-analysis-agent-data/models/"
    "qwen2.5-coder-1.5b-base-df3ce67c0e24480f20468b6ef2894622d69eb73b"
)

_QUESTION_SQL_LEAKAGE = re.compile(
    r"\b(?:select|from|with|join|where|group\s+by|order\s+by|limit|analytics)\b",
    flags=re.IGNORECASE,
)
_LATIN = re.compile(r"[A-Za-z]")
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
    "customer_state": "客户州地区", "customer_city": "客户城市",
    "customer_traffic_source": "客户注册流量来源", "product_category": "商品品类",
    "product_brand": "商品品牌", "product_department": "商品部门",
    "distribution_center": "配送中心", "event_type": "事件类型",
    "event_browser": "浏览器", "event_traffic_source": "事件流量来源",
    "user_country": "用户国家", "user_traffic_source": "用户注册流量来源",
    "user_state": "用户注册州地区",
}
_GRAIN_LABELS = {"day": "日", "week": "周", "month": "月", "quarter": "季度", "year": "年"}


class MaterializationError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalized_question(question: str) -> str:
    return "".join(
        character for character in unicodedata.normalize("NFKC", question).casefold()
        if not character.isspace() and not unicodedata.category(character).startswith("P")
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaterializationError(f"cannot read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise MaterializationError(f"JSON object required: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise MaterializationError(f"cannot read JSONL: {path}") from exc
    for line_no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MaterializationError(f"invalid JSONL at {path}:{line_no}") from exc
        if not isinstance(value, dict):
            raise MaterializationError(f"JSON object required at {path}:{line_no}")
        rows.append(value)
    return rows


def _require_external_new(path: Path) -> Path:
    result = path.resolve()
    if result.is_relative_to(ROOT):
        raise MaterializationError("multi-schema SFT output must stay outside the repository")
    if result.exists():
        raise FileExistsError(result)
    result.parent.mkdir(parents=True, exist_ok=True)
    return result


def _period(spec: Mapping[str, Any]) -> str:
    time = spec["time"]
    if time["mode"] == "all_time":
        return "全部可用时间"
    return f"{time['start']}至{time['end_exclusive']}不含结束日"


def thelook_question_variants(spec: Mapping[str, Any]) -> dict[str, str]:
    """Render eight pure-Chinese forms without changing QuerySpec semantics."""

    metrics = "、".join(_METRIC_LABELS[metric] for metric in spec["metric_ids"])
    period = _period(spec)
    shape = spec["result_shape"]
    if shape == "scalar":
        forms = (
            f"请汇总{period}的{metrics}。", f"想了解{period}的{metrics}情况。",
            f"作为经营概览，查看{period}的{metrics}。", f"{period}的{metrics}是多少？",
            f"请输出{metrics}在{period}的汇总结果。", f"先看时间范围：{period}，统计{metrics}。",
            f"从整体经营切面分析{period}的{metrics}。", f"按已冻结口径计算{period}的{metrics}。",
        )
    elif shape == "dimension_grouped":
        dimension = _DIMENSION_LABELS[spec["dimension"]]
        forms = (
            f"请按{dimension}统计{period}的{metrics}。", f"想比较不同{dimension}在{period}的{metrics}。",
            f"作为经营概览，查看{period}各{dimension}的{metrics}。", f"{period}按{dimension}拆分后的{metrics}分别是多少？",
            f"请输出{period}各{dimension}的{metrics}汇总结果。", f"先看时间范围：{period}，按{dimension}统计{metrics}。",
            f"从{dimension}这个分析切面查看{period}的{metrics}。", f"按已冻结口径，给出{period}各{dimension}的{metrics}。",
        )
    elif shape == "time_series":
        grain = _GRAIN_LABELS[spec["time"]["grain"]]
        forms = (
            f"请按{grain}展示{period}的{metrics}。", f"想查看{period}内{metrics}的{grain}度变化。",
            f"作为经营概览，给出{period}{metrics}的{grain}度时间序列。", f"{period}的{metrics}按{grain}如何分布？",
            f"请输出{period}内{metrics}的{grain}度汇总结果。", f"先看时间范围：{period}，按{grain}统计{metrics}。",
            f"从时间变化切面分析{period}的{metrics}。", f"按已冻结口径生成{period}{metrics}的{grain}度序列。",
        )
    else:
        raise MaterializationError(f"unsupported TheLook result shape: {shape}")
    variants = {variant_id: f"数据查询：{form}" for variant_id, form in zip(VARIANT_IDS, forms, strict=True)}
    if len(set(variants.values())) != len(VARIANT_IDS) or any(_LATIN.search(value) for value in variants.values()):
        raise MaterializationError("TheLook question variants must be unique pure-Chinese forms")
    return variants


def _exact_variant_quotas(total: int) -> dict[str, int]:
    base, extra = divmod(total, len(VARIANT_IDS))
    return {variant: base + int(index < extra) for index, variant in enumerate(VARIANT_IDS)}


def _canonical_variant_id(value: Any) -> str:
    """Reduce a source-specific variant identifier to the shared v1-v8 contract."""

    if not isinstance(value, str):
        raise MaterializationError("missing primary variant")
    match = re.search(r"(v[1-8])$", value)
    if match is None:
        raise MaterializationError(f"invalid primary variant: {value!r}")
    return match.group(1)


def select_thelook_primary_variants(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Allocate exactly balanced forms by split while balancing family×shape."""

    by_split: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_split[str(row["split"])].append(row)
    output: dict[str, str] = {}
    for split, split_rows in sorted(by_split.items()):
        buckets: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for row in split_rows:
            buckets[(str(row["family_id"]), str(row["query_spec"]["result_shape"]))].append(row)
        quota = _exact_variant_quotas(len(split_rows))
        baseline = Counter()
        remainder_by_bucket: dict[tuple[str, str], int] = {}
        for bucket, items in buckets.items():
            base, remainder = divmod(len(items), len(VARIANT_IDS))
            baseline.update(dict.fromkeys(VARIANT_IDS, base))
            remainder_by_bucket[bucket] = remainder
        remaining = {variant: quota[variant] - baseline[variant] for variant in VARIANT_IDS}
        if any(value < 0 for value in remaining.values()):
            raise MaterializationError("invalid TheLook primary-form baseline")
        for bucket in sorted(buckets):
            extras = sorted(
                VARIANT_IDS,
                key=lambda variant: (-remaining[variant], sha256_text(f"{split}|{bucket}|{variant}")),
            )[:remainder_by_bucket[bucket]]
            for variant in extras:
                if remaining[variant] <= 0:
                    raise MaterializationError("exhausted TheLook surface quota")
                remaining[variant] -= 1
            slots = [*extras]
            for variant in VARIANT_IDS:
                slots.extend([variant] * (len(buckets[bucket]) // len(VARIANT_IDS)))
            ordered = sorted(buckets[bucket], key=lambda row: sha256_text(str(row["seed_id"])))
            if len(slots) != len(ordered):
                raise MaterializationError("TheLook surface slot allocation drift")
            for row, variant in zip(ordered, slots, strict=True):
                output[str(row["seed_id"])] = variant
        if any(remaining.values()) or Counter(output[str(row["seed_id"])] for row in split_rows) != Counter(quota):
            raise MaterializationError(f"TheLook surface quota drifted for {split}")
    return output


def _validate_question(question: str, sql: str, sql_hash: str) -> None:
    normalized = normalized_question(question)
    if not normalized or _LATIN.search(question) or _QUESTION_SQL_LEAKAGE.search(question):
        raise MaterializationError("question violates pure-Chinese or SQL-leakage contract")
    if sql_hash.casefold() in question.casefold() or sql.casefold() in question.casefold():
        raise MaterializationError("question leaks Gold SQL or its hash")
    # Do not reject legitimate domain metrics such as "已完成商品行数".  The
    # forbidden phrases identify runtime evidence, not the Chinese word for a
    # business item count.
    forbidden = (
        "返回行数", "实际返回", "执行结果", "执行耗时", "展示截断",
        "reader_role", "daa_thelook_reader",
    )
    if any(token.casefold() in question.casefold() for token in forbidden):
        raise MaterializationError("question leaks execution evidence")


def _question_from_olist_prompt(prompt: Any) -> str:
    """Read the primary question from the frozen Olist runtime-prompt envelope."""

    if not isinstance(prompt, str):
        raise MaterializationError("Olist source row lacks rendered prompt")
    marker = "### Question\n"
    suffix = "\n### SQL"
    if prompt.count(marker) != 1 or not prompt.endswith(suffix):
        raise MaterializationError("Olist source prompt has no unambiguous Question section")
    question = prompt.split(marker, 1)[1][:-len(suffix)].strip()
    if not question:
        raise MaterializationError("Olist source Question section is empty")
    return question


def _load_olist_rows(olist_root: Path) -> list[dict[str, Any]]:
    structural = olist_root / "structural-20260914-v3_1"
    sft = olist_root / "sft-release-v1.1"
    split_audit = _read_json(sft / "split_audit.json")
    if split_audit.get("checks", {}).get("status") != "pass":
        raise MaterializationError("Olist source release audit is not passing")
    rows: list[dict[str, Any]] = []
    for split, expected in (("train", 3000), ("validation", 750)):
        specs = {str(row["seed_id"]): row for row in _read_jsonl(structural / "query_specs" / f"{split}.jsonl")}
        gold = {str(row["seed_id"]): row for row in _read_jsonl(structural / "gold_sql" / f"{split}.jsonl")}
        sft_rows = _read_jsonl(sft / f"{split}.jsonl")
        if len(specs) != expected or len(gold) != expected or len(sft_rows) != expected:
            raise MaterializationError(f"Olist {split} source count drift")
        for sft_row in sft_rows:
            seed_id = str(sft_row.get("seed_id"))
            source_spec = specs.get(seed_id)
            source_gold = gold.get(seed_id)
            question = _question_from_olist_prompt(sft_row.get("rendered_prompt"))
            if source_spec is None or source_gold is None:
                raise MaterializationError("Olist source fields are incomplete")
            artifact = source_gold.get("gold_artifact", {})
            sql = artifact.get("sql")
            sql_hash = artifact.get("sql_sha256")
            spec = source_spec.get("query_spec")
            if not isinstance(spec, dict) or not isinstance(sql, str) or not isinstance(sql_hash, str):
                raise MaterializationError("Olist QuerySpec or Gold evidence is invalid")
            if sha256_text(sql) != sql_hash or sft_row.get("candidate_sql") != sql:
                raise MaterializationError("Olist canonical Gold SQL evidence drifted")
            if sft_row.get("query_spec_id") != spec.get("query_spec_id"):
                raise MaterializationError("Olist QuerySpec identity drifted")
            _validate_question(question, sql, sql_hash)
            raw_primary_variant_id = sft_row.get("primary_variant_id")
            rows.append({
                "workspace_key": "olist", "split": split, "seed_id": seed_id,
                "query_spec": spec, "gold_sql": sql, "gold_sql_sha256": sql_hash,
                "family_id": source_spec.get("family_id"), "program_id": source_spec.get("sql_program_id"),
                "risk_tags": source_spec.get("risk_tags", []),
                "primary_variant_id": _canonical_variant_id(raw_primary_variant_id),
                "source_primary_variant_id": raw_primary_variant_id,
                "question": question, "source_prompt_version": sft_row.get("prompt_format_version"),
                "source_release": "olist-v3-balanced-release-v1.1/sft-release-v1.1",
                "surface_variant_count": sft_row.get("surface_variant_count"),
            })
    return rows


def _load_thelook_rows(thelook_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    manifest = _read_json(thelook_root / "admission" / "manifest.json")
    if manifest.get("checks", {}).get("all_selected_rows_admitted") is not True:
        raise MaterializationError("TheLook admission release is not fully admitted")
    gold: dict[str, dict[str, Any]] = {}
    for split, expected in (("train", 3000), ("validation", 750)):
        split_rows = _read_jsonl(thelook_root / "gold" / f"{split}_gold.jsonl")
        if len(split_rows) != expected:
            raise MaterializationError(f"TheLook {split} Gold count drift")
        gold.update({str(row["seed_id"]): row for row in split_rows})
    admitted = _read_jsonl(thelook_root / "admission" / "admission_records.jsonl")
    if len(admitted) != 3750:
        raise MaterializationError("TheLook admission row count drift")
    selected: list[dict[str, Any]] = []
    for row in admitted:
        seed_id = str(row.get("seed_id"))
        source = gold.get(seed_id)
        if source is None or row.get("admission_status") != "admitted" or row.get("database_executed") is not True:
            raise MaterializationError("TheLook row is not a complete admitted Gold record")
        if row.get("runtime_policy_row_limit") != 1000 or row.get("execution_evidence", {}).get("reader_role") != "daa_thelook_reader":
            raise MaterializationError("TheLook row does not use the frozen 1000-row reader contract")
        if row.get("gold_sql") != source.get("gold_sql") or row.get("gold_sql_sha256") != source.get("gold_sql_sha256"):
            raise MaterializationError("TheLook Gold/admission hash binding drifted")
        selected.append({
            "workspace_key": "thelook", "split": row["split"], "seed_id": seed_id,
            "query_spec": row["query_spec"], "gold_sql": row["gold_sql"], "gold_sql_sha256": row["gold_sql_sha256"],
            "family_id": row["scenario_family_id"], "program_id": row["program_id"],
            "risk_tags": row.get("risk_tags", []), "source_prompt_version": row["query_spec"]["workspace"]["prompt_version"],
            "source_release": "thelook-sql-only-sft-v1-r4",
        })
    primary = select_thelook_primary_variants(selected)
    overlay: list[dict[str, Any]] = []
    for row in selected:
        variants = thelook_question_variants(row["query_spec"])
        variant_id = primary[row["seed_id"]]
        question = variants[variant_id]
        _validate_question(question, row["gold_sql"], row["gold_sql_sha256"])
        row["primary_variant_id"] = variant_id
        row["source_primary_variant_id"] = variant_id
        row["question"] = question
        row["surface_variant_count"] = len(variants)
        overlay.append({"seed_id": row["seed_id"], "split": row["split"], "primary_variant_id": variant_id, "question_variants": variants})
    return selected, overlay


def _validate_spec_and_catalog(row: Mapping[str, Any]) -> tuple[Catalog, dict[str, Any]]:
    raw = row["query_spec"]
    if row["workspace_key"] == "olist":
        catalog = CatalogLoader(OLIST_V3_WORKSPACE).load()
        spec = validate_query_spec(QuerySpec.from_mapping(raw), catalog).as_dict()
    elif row["workspace_key"] == "thelook":
        catalog = CatalogLoader(THELOOK_V2_WORKSPACE).load()
        spec = validate_thelook_v2_query_spec(TheLookV2QuerySpec.from_mapping(raw), catalog).as_dict()
    else:
        raise MaterializationError("unknown workspace")
    if spec != raw:
        raise MaterializationError("validated QuerySpec representation drifted")
    return catalog, spec


def _token_length(tokenizer: Any, prompt: str, sql: str) -> dict[str, int]:
    prompt_tokens = len(tokenizer(prompt + "\n", add_special_tokens=False)["input_ids"])
    target_plus_eos = len(tokenizer(sql.strip(), add_special_tokens=False)["input_ids"]) + 1
    return {"prompt_tokens": prompt_tokens, "target_plus_eos_tokens": target_plus_eos, "sequence_tokens": prompt_tokens + target_plus_eos}


def _protected_thelook_audit(rows: Sequence[Mapping[str, Any]], protected_cases: Path) -> dict[str, Any]:
    protected = _read_jsonl(protected_cases)
    protected_spec = {str(row["query_spec"]["query_spec_id"]) for row in protected}
    protected_gold = {str(row["gold_sql_sha256"]) for row in protected}
    protected_questions = {normalized_question(str(row["question"])) for row in protected}
    current_spec = {str(row["query_spec"]["query_spec_id"]) for row in rows}
    current_gold = {str(row["gold_sql_sha256"]) for row in rows}
    current_questions = {normalized_question(str(row["question"])) for row in rows}
    result = {
        "protected_cases_read_for_collision_audit_only": True,
        "query_spec_overlap": len(current_spec & protected_spec),
        "gold_sql_sha256_overlap": len(current_gold & protected_gold),
        "normalized_question_overlap": len(current_questions & protected_questions),
    }
    if any(value for key, value in result.items() if key.endswith("overlap")):
        raise MaterializationError(f"TheLook protected collision: {result}")
    return result


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _stats(values: Sequence[int]) -> dict[str, int]:
    ordered = sorted(values)
    return {"min": ordered[0], "p50": ordered[(len(ordered) - 1) // 2], "p95": ordered[round((len(ordered) - 1) * .95)], "max": ordered[-1]}


def materialize(
    *, olist_root: Path, thelook_root: Path, tokenizer_dir: Path, output_dir: Path,
    protected_thelook_cases: Path, max_seq_length: int = MAX_SEQUENCE_LENGTH,
) -> dict[str, Any]:
    output = _require_external_new(output_dir)
    if max_seq_length <= 0:
        raise MaterializationError("max_seq_length must be positive")
    olist_rows = _load_olist_rows(olist_root)
    thelook_rows, thelook_overlay = _load_thelook_rows(thelook_root)
    if Counter(row["split"] for row in olist_rows) != Counter(train=3000, validation=750):
        raise MaterializationError("Olist source split count drift")
    if Counter(row["split"] for row in thelook_rows) != Counter(train=3000, validation=750):
        raise MaterializationError("TheLook source split count drift")
    protected_audit = _protected_thelook_audit(thelook_rows, protected_thelook_cases)

    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_dir, local_files_only=True)
    release_rows: dict[str, list[dict[str, Any]]] = {"train": [], "validation": []}
    exclusions: list[dict[str, Any]] = []
    seen_by_split: dict[str, set[str]] = {"train": set(), "validation": set()}
    for index, source in enumerate([*olist_rows, *thelook_rows], 1):
        catalog, spec = _validate_spec_and_catalog(source)
        question = str(source["question"])
        sql = str(source["gold_sql"])
        sql_hash = str(source["gold_sql_sha256"])
        _validate_question(question, sql, sql_hash)
        question_hash = sha256_text(normalized_question(question))
        if question_hash in seen_by_split[str(source["split"])]:
            raise MaterializationError("duplicate normalized primary question within split")
        seen_by_split[str(source["split"])].add(question_hash)
        catalog_payload = render_catalog_payload(catalog, spec)
        result_contract = build_result_contract_payload(catalog, spec)
        prompt = render_multi_schema_prompt(catalog_payload=catalog_payload, query_spec=spec, result_contract=result_contract, question=question)
        lengths = _token_length(tokenizer, prompt, sql)
        sample_id = f"multi-schema-{source['workspace_key']}-{index:04d}"
        if lengths["sequence_tokens"] > max_seq_length:
            exclusions.append({"sample_id": sample_id, "split": source["split"], "workspace_id": spec["workspace"]["workspace_id"], "query_spec_id": spec["query_spec_id"], "token_length": lengths, "reason": "sequence_exceeds_frozen_contract"})
            continue
        release_rows[str(source["split"])].append({
            "sample_id": sample_id, "split": {"name": source["split"]},
            "workspace_id": spec["workspace"]["workspace_id"], "workspace_key": source["workspace_key"],
            "source_release": source["source_release"], "source_prompt_version": source["source_prompt_version"],
            "prompt_format_version": MULTI_SCHEMA_SQL_ONLY_PROMPT_VERSION,
            "seed_id": source["seed_id"], "query_spec_id": spec["query_spec_id"], "query_spec": spec,
            "scenario_family_id": source["family_id"], "program_id": source["program_id"], "risk_tags": source["risk_tags"],
            "source_primary_variant_id": source.get("source_primary_variant_id"),
            "primary_variant_id": source["primary_variant_id"], "surface_variant_count": source["surface_variant_count"],
            "question": question, "question_normalized_sha256": question_hash,
            "catalog": catalog_payload, "result_contract": result_contract,
            "rendered_prompt": prompt, "candidate_sql": sql, "canonical_gold_sql_sha256": sql_hash,
            "training_text": training_text(prompt, sql), "token_length": lengths,
            "admission_status": "admitted",
        })
    if exclusions:
        raise MaterializationError(f"{len(exclusions)} examples exceed {max_seq_length}; release not materialized")
    if {name: len(rows) for name, rows in release_rows.items()} != {"train": 6000, "validation": 1500}:
        raise MaterializationError("combined split counts drifted")
    for identity in ("query_spec_id", "canonical_gold_sql_sha256", "question_normalized_sha256"):
        train_values = {str(row[identity]) for row in release_rows["train"]}
        validation_values = {str(row[identity]) for row in release_rows["validation"]}
        if train_values & validation_values:
            raise MaterializationError(f"cross-split {identity} collision")

    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        _write_jsonl(staging / "train.jsonl", release_rows["train"])
        _write_jsonl(staging / "validation.jsonl", release_rows["validation"])
        (staging / "thelook_question_variants.json").write_text(json.dumps({"version": QUESTION_VARIANT_VERSION, "language": "zh", "policy": "eight_controlled_forms_one_semantic_instance", "rows": thelook_overlay}, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        _write_jsonl(staging / "exclusions.jsonl", exclusions)
        split_stats = {}
        for split, rows in release_rows.items():
            split_stats[split] = {
                "rows": len(rows), "by_workspace": dict(sorted(Counter(row["workspace_key"] for row in rows).items())),
                "by_primary_variant": dict(sorted(Counter(row["primary_variant_id"] for row in rows).items())),
                "sequence_tokens": _stats([int(row["token_length"]["sequence_tokens"]) for row in rows]),
                "family_count": len({str(row["scenario_family_id"]) for row in rows}),
                "program_count": len({str(row["program_id"]) for row in rows}),
            }
        manifest = {
            "release_version": RELEASE_VERSION,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "prompt_format_version": MULTI_SCHEMA_SQL_ONLY_PROMPT_VERSION,
            "execution_contract": {"analyst_execution_row_cap": 1000, "display_preview_row_budget": 200},
            "sources": {
                "olist": {"root": str(olist_root.resolve()), "train_rows": 3000, "validation_rows": 750},
                "thelook": {"root": str(thelook_root.resolve()), "train_rows": 3000, "validation_rows": 750},
            },
            "tokenizer": {"path": str(tokenizer_dir.resolve()), "max_seq_length": max_seq_length, "eos_token_id": tokenizer.eos_token_id, "silent_truncation": False},
            "outputs": {name: {"rows": len(rows), "sha256": sha256_file(staging / f"{name}.jsonl")} for name, rows in release_rows.items()},
            "thelook_question_variants": {"rows": len(thelook_overlay), "sha256": sha256_file(staging / "thelook_question_variants.json")},
            "split_audit": split_stats,
            "protected_collision_audit": {"thelook_v3_r3": protected_audit, "olist_v3_1_final_test_read": False},
            "checks": {
                "status": "pass", "all_rows_admitted_before_question_materialization": True,
                "eight_variants_one_primary_question": True, "questions_have_no_sql_hash_or_result_leakage": True,
                "query_spec_gold_question_cross_split_overlap": False, "tokenizer_length_audited": True,
                "silent_truncation": False, "protected_tests_not_training_sources": True,
            },
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--olist-root", type=Path, default=DEFAULT_OLIST_ROOT)
    parser.add_argument("--thelook-root", type=Path, default=DEFAULT_THELOOK_ROOT)
    parser.add_argument("--tokenizer-dir", type=Path, default=DEFAULT_TOKENIZER)
    parser.add_argument("--protected-thelook-cases", type=Path, default=Path("/disk2/gengnan/data-analysis-agent-data/evals/thelook-cross-schema-final-test-v3-20260915-r3/cases.jsonl"))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-seq-length", type=int, default=MAX_SEQUENCE_LENGTH)
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    print(json.dumps(materialize(olist_root=args.olist_root, thelook_root=args.thelook_root, tokenizer_dir=args.tokenizer_dir, output_dir=args.output_dir, protected_thelook_cases=args.protected_thelook_cases, max_seq_length=args.max_seq_length), ensure_ascii=False, indent=2, sort_keys=True))
