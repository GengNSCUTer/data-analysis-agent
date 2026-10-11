"""Frozen workspace identity for TheLook v2 cross-schema evaluation.

TheLook v2 deliberately shares only the physical read-only database with v1.
Its Catalog, QuerySpec and renderer versions are independent so the historical
206-case v1 evidence remains reproducible while v2 adds new fact domains.
"""

from __future__ import annotations

from pathlib import Path
from datetime import date, timedelta
import json

from .thelook_context import (
    THELOOK_ANALYTICS_COLUMNS,
    THELOOK_DATASET_VERSION,
    THELOOK_SENSITIVE_PROJECTION_COLUMNS,
)
from .workspace import DimensionBinding, WorkspaceProfile


THELOOK_V2_CATALOG_VERSION = "thelook-catalog-v2"
THELOOK_V2_METRIC_VERSION = "0.2-frozen"
THELOOK_V2_POLICY_VERSION = "sql-policy-v1"
THELOOK_V2_CATALOG_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "catalog" / "thelook_catalog_v2.yaml"
)

# These aliases are workspace configuration, not answers copied from QuerySpec.
# Logical result names can differ from physical columns (users.state, etc.).
_DIMENSION_CONFIG = {
    "customer_state": (("客户州", "各州", "按州", "每个州", "地区", "省份"), (("users", "state"),)),
    "customer_city": (("客户城市", "各城市", "按城市", "城市"), (("users", "city"),)),
    "customer_traffic_source": (("客户注册流量来源", "客户流量来源"), (("users", "traffic_source"),)),
    "product_category": (("商品品类", "商品类目", "品类", "类目"), (("products", "category"), ("inventory_items", "product_category"))),
    "product_brand": (("商品品牌", "品牌"), (("products", "brand"), ("inventory_items", "product_brand"))),
    "product_department": (("商品部门", "部门"), (("products", "department"), ("inventory_items", "product_department"))),
    "distribution_center": (("配送中心",), (("distribution_centers", "name"),)),
    "event_type": (("事件类型", "行为类型"), (("events", "event_type"),)),
    "event_browser": (("事件浏览器", "浏览器"), (("events", "browser"),)),
    "event_traffic_source": (("事件流量来源",), (("events", "traffic_source"),)),
    "user_country": (("注册用户国家", "注册用户所在国家", "用户国家", "国家"), (("users", "country"),)),
    "user_traffic_source": (("注册用户流量来源", "用户注册流量来源", "用户流量来源"), (("users", "traffic_source"),)),
    "user_state": (("注册用户州", "注册用户所在州", "用户注册州地区", "用户注册州", "用户州", "用户所在州", "各州", "按州"), (("users", "state"),)),
}
_SHAPE_BOUNDS_PATH = THELOOK_V2_CATALOG_PATH.with_name("thelook_runtime_shape_bounds_v1.json")
_SHAPE_BOUNDS = json.loads(_SHAPE_BOUNDS_PATH.read_text(encoding="utf-8"))
if (_SHAPE_BOUNDS.get("dataset_version") != THELOOK_DATASET_VERSION
        or _SHAPE_BOUNDS.get("version") != "thelook-runtime-shape-bounds-v1"
        or _SHAPE_BOUNDS.get("timezone") != "UTC"):
    raise ValueError("TheLook runtime shape bounds do not match the pinned UTC snapshot")


def _fact_date_counts() -> dict[str, dict[str, int]]:
    output = {}
    for source, bounds in _SHAPE_BOUNDS["fact_date_bounds"].items():
        start = date.fromisoformat(bounds["start"])
        output[source] = {(start + timedelta(days=i)).isoformat(): count
                          for i, count in enumerate(bounds["counts"])}
    return output

THELOOK_V2_WORKSPACE = WorkspaceProfile(
    workspace_id="thelook-cross-schema-eval-v2",
    dataset_id="thelook-ecommerce-kaggle-mirror",
    dataset_version=THELOOK_DATASET_VERSION,
    metric_version=THELOOK_V2_METRIC_VERSION,
    catalog_version=THELOOK_V2_CATALOG_VERSION,
    policy_version=THELOOK_V2_POLICY_VERSION,
    sql_dialect="postgres",
    analytics_schema="analytics",
    reader_role="daa_thelook_reader",
    writer_role="daa_thelook_writer_unconfigured",
    allowed_columns=THELOOK_ANALYTICS_COLUMNS,
    analyst_tables=frozenset(THELOOK_ANALYTICS_COLUMNS),
    sensitive_projection_columns=THELOOK_SENSITIVE_PROJECTION_COLUMNS,
    catalog_path=THELOOK_V2_CATALOG_PATH,
    dimension_bindings={
        name: DimensionBinding(aliases, sources, _SHAPE_BOUNDS["cardinalities"][name])
        for name, (aliases, sources) in _DIMENSION_CONFIG.items()
    },
    fact_date_counts=_fact_date_counts(),
)
