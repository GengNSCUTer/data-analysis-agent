"""Map an unmatched question onto the frozen Catalog.

Alias lookup stays the fast path. This step runs only when that lookup finds
no metric. The model may choose metric and dimension ids from the menu below;
anything outside the menu is discarded. It cannot invent a metric, write SQL,
or bypass later attribution and result checks.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Awaitable, Callable

from vanna.core.user import User

from .semantic_catalog import Catalog


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_GROUNDABLE_DIMENSIONS = frozenset(
    {
        "customer_state",
        "seller_state",
        "seller_city",
        "customer_city",
        "product_category_name",
        "payment_type",
    }
)


@dataclass(frozen=True)
class CatalogGrounding:
    metric_ids: tuple[str, ...]
    dimensions: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "metric_ids": list(self.metric_ids),
            "dimensions": list(self.dimensions),
        }


class ConstrainedCatalogGrounder:
    """One short completion, validated against the current Catalog."""

    def __init__(
        self,
        catalog: Catalog,
        complete: Callable[[str, User], Awaitable[str]],
        *,
        max_metrics: int = 4,
    ):
        self.catalog = catalog
        self.complete = complete
        self.max_metrics = max_metrics

    def prompt(self, question: str, user: User | None = None) -> str:
        role = "admin" if user and "admin" in user.group_memberships else "analyst"
        lines = [
            "把用户问题对齐到下面的指标和分组维度。",
            "只能使用列出的 id。无法判断指标时 metric_ids 为空，不要猜测。",
            "用户只问一个指标时只返回一个 id。",
            "dimensions 只在用户要求按该维度汇总或排名时填写。",
            "点名某个州或地区是筛选，不是按州分组。",
            "只输出一个 JSON 对象，不要解释。",
            "",
            "指标：",
        ]
        for metric in self.catalog.metrics:
            if role not in metric.role_visibility:
                continue
            aliases = "、".join(metric.aliases[:6])
            lines.append(
                f"- {metric.metric_id} | {metric.name} | {aliases} | {metric.description[:80]}"
            )
        lines.extend(
            [
                "",
                "维度：",
                "- customer_state | 按客户州汇总",
                "- customer_city | 按客户城市汇总，只取前 20",
                "- seller_state | 按卖家州汇总",
                "- seller_city | 按卖家城市汇总，只取前 20；不要返回卖家编号",
                "- product_category_name | 按商品品类汇总",
                "- payment_type | 按支付方式汇总",
                "",
                f"问题：{question.strip()[:500]}",
                'JSON 格式：{"metric_ids":[],"dimensions":[]}',
            ]
        )
        return "\n".join(lines)

    async def ground(self, question: str, user: User | None = None) -> CatalogGrounding | None:
        text = await self.complete(self.prompt(question, user), user or User(id="grounding"))
        return parse_catalog_grounding(
            text,
            self.catalog,
            user=user,
            max_metrics=self.max_metrics,
        )


def parse_catalog_grounding(
    text: str,
    catalog: Catalog,
    *,
    user: User | None = None,
    max_metrics: int = 4,
) -> CatalogGrounding | None:
    if not isinstance(text, str) or not text.strip():
        return None
    match = _JSON_OBJECT.search(text)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    role = "admin" if user and "admin" in user.group_memberships else "analyst"
    visible = {
        metric.metric_id: metric
        for metric in catalog.metrics
        if role in metric.role_visibility
    }
    raw_metrics = payload.get("metric_ids")
    if not isinstance(raw_metrics, list):
        return None
    metric_ids = tuple(
        dict.fromkeys(
            metric_id
            for metric_id in raw_metrics
            if isinstance(metric_id, str) and metric_id in visible
        )
    )[:max_metrics]
    if not metric_ids:
        return None
    allowed = set.intersection(
        *(set(visible[metric_id].allowed_dimensions) for metric_id in metric_ids)
    )
    raw_dimensions = payload.get("dimensions") or []
    dimensions: list[str] = []
    if isinstance(raw_dimensions, list):
        for dimension in raw_dimensions:
            if (
                isinstance(dimension, str)
                and dimension in _GROUNDABLE_DIMENSIONS
                and dimension in allowed
                and dimension not in dimensions
            ):
                dimensions.append(dimension)
    return CatalogGrounding(metric_ids=metric_ids, dimensions=tuple(dimensions))


def grounded_retrieval_question(question: str, grounding: CatalogGrounding, catalog: Catalog) -> str:
    """Append official metric names so deterministic retrieval can select them."""

    names = [
        catalog.metrics_by_id[metric_id].name
        for metric_id in grounding.metric_ids
        if metric_id in catalog.metrics_by_id
    ]
    return f"{question.strip()}\n{' '.join(names)}"
