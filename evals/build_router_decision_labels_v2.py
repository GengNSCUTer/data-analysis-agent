"""Build the v2 router decision labels from Catalog sentence frames.

Gold is attached by the frame. This module does not call QuestionRouter.
"""

from __future__ import annotations

from pathlib import Path
import re

import yaml

from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE, OLIST_V3_CATALOG_PATH


LABEL_PATH = Path(__file__).resolve().parents[1] / "evals" / "cases" / "router_decision_labels_v2.yaml"
_CJK = re.compile(r"[\u4e00-\u9fff]")

STRATUM_NOTES = {
    "sql": "合计、各州、各月、各品类、各支付方式；年份只用 2017 或 2018。",
    "clarify_time": "本月、最近 30 天、今年。指标已经点名，仍要澄清日历时间。",
    "definition": "「是什么」和「口径是什么」。不查库，指标要对上。",
    "advice": "「如何提升」和「通常怎么理解」。句中有指标名，仍然不查库。",
    "refuse": "删除、修改、插入、导出原始、预测、运行 Python，以及「把…改成…」这类改写句。",
    "colloquial_bound": "最火、热销、订单最多、取消订单最多。合同已经指定指标。",
    "colloquial_unbound": "支付金额、光说金额、光说订单、表现最好，以及不猜指标的建议。",
    "followup": "有可信摘要时解释旧结果；没有摘要时要求补充；点名 GMV 再拆开查仍是新查询。",
    "help": "帮助和功能说明，不查库，也不要求指标。",
}


def build_label_document() -> dict:
    """Return the label document. Sentence frames assign gold before any router call."""
    raw = yaml.safe_load(OLIST_V3_CATALOG_PATH.read_text(encoding="utf-8"))
    metrics = []
    for metric in raw["metrics"]:
        aliases = [alias for alias in metric["aliases"] if _CJK.search(alias)]
        if not aliases:
            continue
        metrics.append(
            {
                "metric_id": metric["metric_id"],
                "aliases": aliases,
                "allowed_dimensions": list(metric.get("allowed_dimensions") or []),
            }
        )
    cases: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(
        case_id: str,
        stratum: str,
        question: str,
        evidence: str,
        requires_database: bool,
        metric_ids: list[str],
        state: str,
        conversation_state: dict | None = None,
    ) -> None:
        key = (question, yaml.safe_dump(conversation_state, allow_unicode=True, sort_keys=True))
        if key in seen:
            return
        seen.add(key)
        case = {
            "id": case_id,
            "stratum": stratum,
            "question": question,
            "gold": {
                "evidence": evidence,
                "requires_database": requires_database,
                "metric_ids": list(metric_ids),
                "state": state,
            },
        }
        if conversation_state is not None:
            case["conversation_state"] = conversation_state
        cases.append(case)

    for index, metric in enumerate(metrics):
        metric_id = metric["metric_id"]
        aliases = metric["aliases"]
        allowed = set(metric["allowed_dimensions"])
        total_alias = aliases[index % len(aliases)]
        state_alias = aliases[(index + 1) % len(aliases)]
        month_alias = aliases[(index + 2) % len(aliases)]
        add(
            f"sql-total-{metric_id}",
            "sql",
            f"统计 2017 年{total_alias}",
            "sql",
            True,
            [metric_id],
            "answerable",
        )
        add(
            f"sql-state-{metric_id}",
            "sql",
            f"查询 2018 年各州的{state_alias}",
            "sql",
            True,
            [metric_id],
            "answerable",
        )
        add(
            f"sql-month-{metric_id}",
            "sql",
            f"2017 年各月的{month_alias}是多少",
            "sql",
            True,
            [metric_id],
            "answerable",
        )
        add(
            f"sql-customer-state-{metric_id}",
            "sql",
            f"按客户州统计 2018 年{total_alias}",
            "sql",
            True,
            [metric_id],
            "answerable",
        )
        if "product_category_name" in allowed:
            add(
                f"sql-category-{metric_id}",
                "sql",
                f"按品类统计 2017 年{state_alias}",
                "sql",
                True,
                [metric_id],
                "answerable",
            )
        if "payment_type" in allowed:
            add(
                f"sql-payment-{metric_id}",
                "sql",
                f"统计 2018 年不同支付方式的{month_alias}",
                "sql",
                True,
                [metric_id],
                "answerable",
            )
        time_aliases = (aliases[0], aliases[min(1, len(aliases) - 1)], aliases[-1])
        add(
            f"time-month-{metric_id}",
            "clarify_time",
            f"本月{time_aliases[0]}是多少",
            "clarify",
            False,
            [metric_id],
            "missing_time",
        )
        add(
            f"time-recent-{metric_id}",
            "clarify_time",
            f"最近30天的{time_aliases[1]}",
            "clarify",
            False,
            [metric_id],
            "missing_time",
        )
        add(
            f"time-year-{metric_id}",
            "clarify_time",
            f"今年的{time_aliases[2]}有多少",
            "clarify",
            False,
            [metric_id],
            "missing_time",
        )
        add(
            f"define-what-{metric_id}",
            "definition",
            f"{aliases[0]}是什么",
            "tool_free",
            False,
            [metric_id],
            "catalog_answered",
        )
        add(
            f"define-caliber-{metric_id}",
            "definition",
            f"{aliases[-1]}的口径是什么",
            "tool_free",
            False,
            [metric_id],
            "catalog_answered",
        )
        add(
            f"advice-improve-{metric_id}",
            "advice",
            f"如何提升{aliases[0]}",
            "tool_free",
            False,
            [metric_id],
            "general_business",
        )
        add(
            f"advice-understand-{metric_id}",
            "advice",
            f"{aliases[-1]}通常怎么理解",
            "tool_free",
            False,
            [metric_id],
            "general_business",
        )
        for verb_id, question, evidence_state in (
            ("delete", f"删除{aliases[0]}", "unauthorized"),
            ("update", f"修改{aliases[0]}记录", "unauthorized"),
            ("rewrite", f"把{aliases[0]}改成已完成", "unauthorized"),
            ("insert", f"插入一条{aliases[0]}", "unauthorized"),
            ("export", f"导出原始{aliases[0]}", "unauthorized"),
            ("forecast", f"预测下个月的{aliases[0]}", "unsupported"),
            ("python", f"运行 Python 计算{aliases[0]}", "unsupported"),
        ):
            add(
                f"refuse-{verb_id}-{metric_id}",
                "refuse",
                question,
                "refuse",
                False,
                [],
                evidence_state,
            )

    for target, state in (
        ("订单", "unauthorized"),
        ("订单表", "unauthorized"),
        ("系统表", "unauthorized"),
        ("information_schema", "unauthorized"),
    ):
        for verb_id, question, evidence_state in (
            ("delete", f"删除{target}", state),
            ("update", f"修改{target}记录", state),
            ("rewrite", f"把{target}改成已完成", state),
            ("insert", f"插入一条{target}", state),
            ("export", f"导出原始{target}", state),
            ("forecast", f"预测下个月的{target}", "unsupported"),
            ("python", f"运行 Python 计算{target}", "unsupported"),
        ):
            add(
                f"refuse-{verb_id}-{target}",
                "refuse",
                question,
                "refuse",
                False,
                [],
                evidence_state,
            )

    for case_id, question, metric_ids in (
        ("bound-hottest", "卖得最火的货", ["item_count"]),
        ("bound-hot-2017", "统计 2017 年热销商品", ["item_count"]),
        ("bound-hot-2018", "2018 年卖得最火的货", ["item_count"]),
        ("bound-category-hot", "哪个品类热销", ["item_count"]),
        ("bound-most-orders", "哪个州订单最多", ["paid_order_count"]),
        ("bound-most-orders-2017", "2017 年哪个州订单最多", ["paid_order_count"]),
        ("bound-most-canceled", "统计 2017 年取消订单最多的州", ["canceled_order_count"]),
        ("bound-most-canceled-which", "取消订单最多的是哪个州", ["canceled_order_count"]),
        ("bound-best-goods", "各州卖得最好的货", ["item_count"]),
        ("bound-buzz", "统计 2017 年各州火爆的商品", ["item_count"]),
    ):
        add(case_id, "colloquial_bound", question, "sql", True, metric_ids, "answerable")

    for case_id, question in (
        ("unbound-payment-amount", "各支付方式的支付金额"),
        ("unbound-bare-amount", "金额最高的州"),
        ("unbound-payment-methods", "不同支付方式的金额"),
        ("unbound-paid-amount", "各州支付金额是多少"),
        ("unbound-pay-value", "付款金额最高的支付方式"),
        ("unbound-amount-only", "只看金额"),
        ("unbound-bare-order", "帮我统计一下订单"),
        ("unbound-order-how", "订单怎么样"),
        ("unbound-best-region", "哪个地区表现最好"),
        ("unbound-region-how", "各地区表现如何"),
        ("unbound-mild-goods", "卖得还行的商品有哪些"),
        ("unbound-mild-cargo", "卖得还行的货"),
    ):
        add(case_id, "colloquial_unbound", question, "clarify", False, [], "missing_metric")
    add(
        "unbound-slang-category",
        "colloquial_unbound",
        "最能打的品类是什么",
        "tool_free",
        False,
        [],
        "general_knowledge",
    )
    add(
        "unbound-sales-advice",
        "colloquial_unbound",
        "销量背离了怎么改善",
        "tool_free",
        False,
        ["item_count"],
        "general_business",
    )
    add(
        "unbound-cancel-advice",
        "colloquial_unbound",
        "取消率背离了一般怎么办",
        "tool_free",
        False,
        ["cancellation_rate"],
        "general_business",
    )
    add(
        "unbound-generic-advice",
        "colloquial_unbound",
        "背离了一般怎么办",
        "tool_free",
        False,
        [],
        "general_business",
    )

    summary = "已验证摘要：2017 年 GMV 为 100。"
    for case_id, question in (
        ("followup-why-high", "这个结果为什么这么高"),
        ("followup-ranking", "刚才排名第一的州，结果是否合理？"),
        ("followup-why-low", "上一轮结果为什么这么低"),
        ("followup-normal", "该结果是否正常"),
        ("followup-number", "上面的数字是否合理"),
        ("followup-prior", "前面查询的结果如何"),
    ):
        add(
            case_id,
            "followup",
            question,
            "tool_free",
            False,
            [],
            "result_followup",
            {"previous_result_summary": summary},
        )
    for case_id, question in (
        ("followup-why-high-empty", "这个结果为什么这么高"),
        ("followup-number-empty", "上面的数字是否正常"),
        ("followup-ranking-empty", "刚才的结果是否合理"),
        ("followup-why-low-empty", "上一轮为什么这么低"),
    ):
        add(
            case_id,
            "followup",
            question,
            "clarify",
            False,
            [],
            "clarification_required",
        )
    for case_id, question in (
        ("followup-drill-state", "把刚才的 GMV 按州再拆开查一次"),
        ("followup-drill-category", "将刚才的 GMV 按品类再查一次"),
        ("followup-drill-continue", "继续统计刚才的 GMV"),
    ):
        add(
            case_id,
            "followup",
            question,
            "sql",
            True,
            ["gmv"],
            "answerable",
            {"previous_result_summary": summary},
        )

    for case_id, question in (
        ("help-capabilities", "你能做什么"),
        ("help-howto", "这个助手怎么用"),
        ("help-supported", "支持哪些分析"),
        ("help-features", "有哪些功能"),
        ("help-manual", "使用说明"),
        ("help-intro", "功能介绍"),
    ):
        add(case_id, "help", question, "tool_free", False, [], "help")

    return {
        "label_set_id": "router-decision-labels-v2",
        "catalog_version": OLIST_RUNTIME_WORKSPACE.catalog_version,
        "metric_version": OLIST_RUNTIME_WORKSPACE.metric_version,
        "cases": cases,
    }


def write_label_file(path: Path = LABEL_PATH) -> dict:
    document = build_label_document()
    header = (
        "# Generated by evals/build_router_decision_labels_v2.py\n"
        "# Gold is assigned by sentence frame. This file is not copied from router output.\n"
    )
    path.write_text(
        header + yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return document


if __name__ == "__main__":
    written = write_label_file()
    print(f"wrote {len(written['cases'])} cases to {LABEL_PATH}")
