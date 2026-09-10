"""Frozen workspace identity for TheLook v2 cross-schema evaluation.

TheLook v2 deliberately shares only the physical read-only database with v1.
Its Catalog, QuerySpec and renderer versions are independent so the historical
206-case v1 evidence remains reproducible while v2 adds new fact domains.
"""

from __future__ import annotations

from pathlib import Path

from .thelook_context import (
    THELOOK_ANALYTICS_COLUMNS,
    THELOOK_DATASET_VERSION,
    THELOOK_SENSITIVE_PROJECTION_COLUMNS,
)
from .workspace import WorkspaceProfile


THELOOK_V2_CATALOG_VERSION = "thelook-catalog-v2"
THELOOK_V2_METRIC_VERSION = "0.2-frozen"
THELOOK_V2_POLICY_VERSION = "sql-policy-v1"
THELOOK_V2_CATALOG_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "catalog" / "thelook_catalog_v2.yaml"
)

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
)
