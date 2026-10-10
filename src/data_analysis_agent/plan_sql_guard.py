"""Reject candidate SQL that drops a server-owned query-plan rule.

The SQL model still writes the statement. This check only verifies that the
statement contains the rule the plan already froze: a customer-state filter,
a top-20 city ranking, payment-share allocation, or a multi-membership group.
"""

from __future__ import annotations

import re
from typing import Any, Mapping


_LIMIT = re.compile(r"\blimit\s+(\d+)\b", re.IGNORECASE)
_TOP_N_DIMENSIONS = {"customer_city": 20, "seller_city": 20}


def sql_plan_mismatch(sql: str, plan: Mapping[str, Any] | None) -> str | None:
    """Return a user-safe reason when SQL omits a required plan rule."""

    if not isinstance(sql, str) or not sql.strip() or not isinstance(plan, Mapping):
        return None
    folded = sql.casefold()
    for warning in plan.get("warnings") or []:
        if not isinstance(warning, str) or not warning.startswith("filter_customer_state:"):
            continue
        code = warning.split(":", 1)[1].casefold()
        if "customer_state" not in folded or code not in folded:
            return f"SQL 必须按客户州筛选 customer_state = '{code.upper()}'，且不要把该州做成分组列。"
    limits = dict(_TOP_N_DIMENSIONS)
    raw_top_n = plan.get("top_n")
    if isinstance(raw_top_n, Mapping):
        for key, value in raw_top_n.items():
            try:
                limits[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
    for dimension in plan.get("dimensions") or []:
        limit = limits.get(str(dimension))
        if limit is None:
            continue
        match = _LIMIT.search(sql)
        if match is None or int(match.group(1)) > limit:
            return f"按 {dimension} 汇总时 SQL 必须 LIMIT {limit} 或更少，只保留该指标最高的分组。"
    for requirement in plan.get("attribution_requirements") or []:
        if not isinstance(requirement, Mapping):
            continue
        mode = requirement.get("policy_mode")
        dimension = str(requirement.get("dimension") or "")
        if mode == "payment_share" and (
            "payment_value" not in folded or "payment_type" not in folded
        ):
            return "按支付方式拆金额时，SQL 必须用 payment_value 占比分摊，并按 payment_type 汇总。"
        if mode == "multi_membership" and dimension and dimension.casefold() not in folded:
            return f"SQL 必须包含分组列 {dimension}，并允许同一订单进入多个分组。"
    return None
