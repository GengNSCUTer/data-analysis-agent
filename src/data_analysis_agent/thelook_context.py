"""TheLook cross-schema evaluation workspace definition.

This profile is intentionally not wired into the default Olist runtime. It
exists to load and validate the frozen TheLook semantic Catalog and to prepare
future zero-shot Base/Adapter evaluation under the same policy interface.
"""

from pathlib import Path

from .workspace import WorkspaceProfile


THELOOK_CATALOG_VERSION = "thelook-catalog-v1"
THELOOK_DATASET_VERSION = "thelook-kaggle-mirror-v1-20260908"
THELOOK_METRIC_VERSION = "0.1-frozen"
THELOOK_POLICY_VERSION = "sql-policy-v1"
THELOOK_CATALOG_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "catalog" / "thelook_catalog.yaml"
)

# These sets describe only the vetted analytics views, never `thelook_raw`.
THELOOK_ANALYTICS_COLUMNS = {
    "distribution_centers": frozenset({"id", "name", "latitude", "longitude"}),
    "events": frozenset(
        {
            "id", "user_id", "sequence_number", "session_id", "created_at", "city",
            "state", "browser", "traffic_source", "uri", "event_type",
        }
    ),
    "inventory_items": frozenset(
        {
            "id", "product_id", "created_at", "sold_at", "cost", "product_category",
            "product_name", "product_brand", "product_retail_price",
            "product_department", "product_sku", "product_distribution_center_id",
        }
    ),
    "order_items": frozenset(
        {
            "id", "order_id", "user_id", "product_id", "inventory_item_id", "status",
            "created_at", "shipped_at", "delivered_at", "returned_at", "sale_price",
        }
    ),
    "orders": frozenset(
        {
            "order_id", "user_id", "status", "gender", "created_at", "returned_at",
            "shipped_at", "delivered_at", "num_of_item",
        }
    ),
    "products": frozenset(
        {
            "id", "cost", "category", "name", "brand", "retail_price", "department",
            "sku", "distribution_center_id",
        }
    ),
    "users": frozenset(
        {"id", "age", "gender", "state", "city", "country", "traffic_source", "created_at"}
    ),
}

THELOOK_SENSITIVE_PROJECTION_COLUMNS = frozenset(
    {
        "id", "user_id", "order_id", "product_id", "inventory_item_id",
        "distribution_center_id", "product_distribution_center_id", "session_id", "sku",
        "latitude", "longitude",
    }
)

THELOOK_WORKSPACE = WorkspaceProfile(
    workspace_id="thelook-cross-schema-eval",
    dataset_id="thelook-ecommerce-kaggle-mirror",
    dataset_version=THELOOK_DATASET_VERSION,
    metric_version=THELOOK_METRIC_VERSION,
    catalog_version=THELOOK_CATALOG_VERSION,
    policy_version=THELOOK_POLICY_VERSION,
    sql_dialect="postgres",
    analytics_schema="analytics",
    reader_role="daa_thelook_reader",
    # No writer role is provisioned: this workspace is evaluation-only.
    writer_role="daa_thelook_writer_unconfigured",
    allowed_columns=THELOOK_ANALYTICS_COLUMNS,
    analyst_tables=frozenset(THELOOK_ANALYTICS_COLUMNS),
    sensitive_projection_columns=THELOOK_SENSITIVE_PROJECTION_COLUMNS,
    catalog_path=THELOOK_CATALOG_PATH,
)
