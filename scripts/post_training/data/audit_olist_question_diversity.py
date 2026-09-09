#!/usr/bin/env python3
"""Audit Chinese Olist question surface-form diversity without changing data.

The input is the external runtime-candidate JSONL.  The report intentionally
contains aggregate distributions and IDs only; raw questions and SQL remain
outside the Git worktree.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[3]

METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "gmv": ("gmv", "成交额", "成交金额", "销售额", "营收", "交易额"),
    "paid_order_count": ("有效订单数", "有效订单量", "订单数", "订单量", "成交单量", "完成了多少单"),
    "average_delivery_days": ("平均履约天数", "履约天数", "平均配送天数", "平均交付天数"),
    "positive_review_rate": ("好评率", "正面评价率", "好评比例"),
    "item_count": ("商品件数", "商品数量", "件数", "销量"),
    "average_order_value": ("平均订单商品金额", "平均订单金额", "客单价", "平均成交金额"),
    "average_review_score": ("平均评价分", "平均评分", "评价均分"),
    "on_time_delivery_rate": ("准时率", "准时送达率", "按时送达率"),
    "cancellation_rate": ("取消率", "取消比例", "订单取消率"),
    "freight_amount": ("运费金额", "运费", "配送费", "物流费用"),
}

DIMENSION_ALIASES: dict[str, tuple[str, ...]] = {
    "customer_state": ("客户州", "用户州", "州", "地区", "省州"),
    "customer_city": ("客户城市", "用户城市", "城市"),
    "product_category_name": ("商品类目", "商品类别", "品类", "类别"),
    "payment_type": ("支付方式", "付款方式", "支付类型"),
}

_DATE_RANGE = re.compile(r"\d{4}[-年/]\d{1,2}[-月/]\d{1,2}日?\s*(?:至|到|—|-|~|～)\s*\d{4}[-年/]\d{1,2}[-月/]\d{1,2}日?")
_DATE = re.compile(r"\d{4}[-年/]\d{1,2}[-月/]\d{1,2}日?")
_YEAR = re.compile(r"(?<!\d)\d{4}(?!\d)")
_NUMBER = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?(?![A-Za-z])")


def _replace_terms(text: str, terms: Iterable[str], replacement: str) -> str:
    for term in sorted(set(terms), key=len, reverse=True):
        text = text.replace(term, replacement)
    return text


def normalize_question(question: str) -> str:
    """Replace literal values and known semantic words with stable markers."""
    text = unicodedata.normalize("NFKC", question).strip().lower()
    text = _DATE_RANGE.sub("<DATE_RANGE>", text)
    text = _DATE.sub("<DATE>", text)
    text = _YEAR.sub("<YEAR>", text)
    text = _replace_terms(text, (term for values in METRIC_ALIASES.values() for term in values), "<METRIC>")
    text = _replace_terms(text, (term for values in DIMENSION_ALIASES.values() for term in values), "<DIMENSION>")
    text = _NUMBER.sub("<N>", text)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[，。！？、；：,:;!?]+$", "", text)
    return text


def _question_id(row: dict[str, Any], index: int) -> str:
    # A surface-form pilot has five rows sharing one seed.  Prefer the
    # per-question variant ID so duplicate-template examples remain directly
    # reviewable instead of collapsing five distinct forms onto one seed ID.
    value = row.get("case_id") or row.get("sample_id") or row.get("variant_id") or row.get("seed_id")
    return str(value) if value is not None else f"row-{index:04d}"


def _opening(question: str) -> str:
    text = unicodedata.normalize("NFKC", question).strip()
    prefixes = (
        "请按季度", "请按月", "请按年", "请按日", "按季度", "按月", "按年", "按日",
        "请统计", "统计", "请汇总", "汇总", "请给出", "请展示", "展示", "看一下",
        "帮我", "查询", "了解一下", "想看", "给我",
    )
    for prefix in prefixes:
        if text.startswith(prefix):
            return prefix
    return "other"


def _time_style(row: dict[str, Any], question: str) -> str:
    plan = row.get("query_plan") or row.get("query_spec") or {}
    if not isinstance(plan, dict):
        plan = {}
    time = plan.get("time") if isinstance(plan.get("time"), dict) else plan.get("time_range")
    if not isinstance(time, dict):
        time = {}
    mode = time.get("mode")
    if mode == "all_time" or ("start" not in time and "end" not in time and not plan.get("time_grain")):
        return "all_time"
    grain = plan.get("time_grain") or time.get("grain")
    if mode == "series" or grain:
        return f"series_{grain}"
    if mode == "absolute_range" or (time.get("start") and time.get("end")):
        return "absolute_range"
    if re.search(r"去年|今年|本月|最近|上个月|截至", question):
        return "relative_or_open"
    return "other"


def _matched_aliases(question: str, aliases: dict[str, tuple[str, ...]]) -> set[str]:
    normalized = unicodedata.normalize("NFKC", question).casefold()
    return {key for key, terms in aliases.items() if any(term.casefold() in normalized for term in terms)}


def _signature(row: dict[str, Any]) -> str:
    spec = row.get("query_plan") or row.get("query_spec") or {}
    if not isinstance(spec, dict):
        spec = {}
    value = {
        "metric_ids": spec.get("metric_ids", row.get("metric_ids", [])),
        "result_shape": spec.get("result_shape", spec.get("plan_type", row.get("result_shape"))),
        "dimension": spec.get("dimension", spec.get("dimensions", row.get("dimension"))),
        "time": spec.get("time", spec.get("time_range", row.get("time"))),
        "time_grain": spec.get("time_grain"),
        "join_program_id": spec.get("join_program_id", spec.get("execution_strategy", row.get("join_program_id"))),
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def audit_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise ValueError("input JSONL is empty")
    questions = [str(row.get("question", "")) for row in rows]
    if any(not question.strip() for question in questions):
        raise ValueError("every row must contain a non-empty question")

    normalized = [normalize_question(question) for question in questions]
    exact = Counter(questions)
    templates = Counter(normalized)
    family_rows: dict[str, list[int]] = defaultdict(list)
    family_signatures: dict[str, set[str]] = defaultdict(set)
    for index, row in enumerate(rows):
        family = str(row.get("family_id") or row.get("query_spec_id") or "unknown")
        family_rows[family].append(index)
        family_signatures[family].add(_signature(row))

    variant_counts = Counter(len(indices) for indices in family_rows.values())
    template_counts_by_family = Counter()
    for indices in family_rows.values():
        template_counts_by_family[len({normalized[index] for index in indices})] += 1

    metric_alias_hits = Counter()
    dimension_alias_hits = Counter()
    split_counts = Counter(str(row.get("split", "unknown")) for row in rows)
    result_shape_counts = Counter()
    metric_count_counts = Counter()
    for question in questions:
        metric_alias_hits.update(_matched_aliases(question, METRIC_ALIASES))
        dimension_alias_hits.update(_matched_aliases(question, DIMENSION_ALIASES))
    for row in rows:
        plan = row.get("query_plan") or row.get("query_spec") or {}
        if not isinstance(plan, dict):
            plan = {}
        result_shape_counts[str(plan.get("result_shape") or plan.get("plan_type") or "unknown")] += 1
        metric_count_counts[str(len(plan.get("metric_ids", [])))] += 1

    duplicate_template_records = []
    for template, count in templates.most_common(20):
        if count > 1:
            indices = [index for index, value in enumerate(normalized) if value == template]
            duplicate_template_records.append({
                "normalized_template": template,
                "rows": count,
                "sample_ids": [_question_id(rows[index], index) for index in indices[:10]],
            })

    cross_family_template_collisions = sum(
        1 for template in templates
        if len({str(rows[index].get("family_id") or rows[index].get("query_spec_id") or "unknown") for index, value in enumerate(normalized) if value == template}) > 1
    )
    inconsistent_families = {
        family: len(signatures)
        for family, signatures in family_signatures.items()
        if len(signatures) > 1
    }
    return {
        "report_schema_version": "olist-question-diversity-audit-v1",
        "row_count": len(rows),
        "unique_question_count": len(exact),
        "exact_duplicate_rows": len(rows) - len(exact),
        "exact_duplicate_rate": round((len(rows) - len(exact)) / len(rows), 6),
        "normalized_template_count": len(templates),
        "normalized_template_duplicate_rows": len(rows) - len(templates),
        "normalized_template_duplicate_rate": round((len(rows) - len(templates)) / len(rows), 6),
        "top_normalized_templates": duplicate_template_records,
        "cross_family_template_collision_count": cross_family_template_collisions,
        "family_count": len(family_rows),
        "family_variant_count_distribution": dict(sorted(variant_counts.items())),
        "family_unique_template_count_distribution": dict(sorted(template_counts_by_family.items())),
        "family_signature_inconsistency_count": len(inconsistent_families),
        "family_signature_inconsistency_examples": dict(list(inconsistent_families.items())[:20]),
        "split_distribution": dict(split_counts.most_common()),
        "result_shape_distribution": dict(result_shape_counts.most_common()),
        "metric_count_distribution": dict(sorted(metric_count_counts.items(), key=lambda item: int(item[0]))),
        "opening_phrase_distribution": dict(Counter(_opening(question) for question in questions).most_common()),
        "time_expression_distribution": dict(Counter(_time_style(row, question) for row, question in zip(rows, questions)).most_common()),
        "metric_alias_hit_distribution": dict(metric_alias_hits.most_common()),
        "dimension_alias_hit_distribution": dict(dimension_alias_hits.most_common()),
        "question_length_chars": {
            "min": min(map(len, questions)),
            "p50": sorted(map(len, questions))[len(questions) // 2],
            "p95": sorted(map(len, questions))[max(0, int(len(questions) * 0.95) - 1)],
            "max": max(map(len, questions)),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-jsonl", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    return parser.parse_args()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"line {line_number} is not an object")
            rows.append(value)
    return rows


def main() -> int:
    args = parse_args()
    input_path = args.input_jsonl.resolve()
    output_path = args.output_json.resolve()
    if input_path.is_relative_to(ROOT):
        raise ValueError("input runtime data must stay outside the Git worktree")
    if output_path.is_relative_to(ROOT):
        raise ValueError("audit report must stay outside the Git worktree")
    report = audit_rows(_read_jsonl(input_path))
    report["input_sha256"] = hashlib.sha256(input_path.read_bytes()).hexdigest()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"rows": report["row_count"], "templates": report["normalized_template_count"], "output": str(output_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
