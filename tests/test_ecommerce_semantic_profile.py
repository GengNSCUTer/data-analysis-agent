"""Shared ecommerce roles must bind to real metric and dimension ids."""

from pathlib import Path

import yaml

from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE, OLIST_V3_WORKSPACE
from data_analysis_agent.semantic_catalog import CatalogLoader
from data_analysis_agent.thelook_v2_context import THELOOK_V2_WORKSPACE


ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "data" / "catalog" / "ecommerce_semantic_profile_v1.yaml"


def test_profile_bindings_exist_in_the_dataset_catalogs() -> None:
    profile = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
    catalogs = {
        "olist-catalog-v3": CatalogLoader(workspace=OLIST_V3_WORKSPACE).load(),
        "thelook-catalog-v2": CatalogLoader(THELOOK_V2_WORKSPACE).load(),
    }
    for role in profile["metric_roles"]:
        for catalog_version, metric_id in role["bindings"].items():
            catalog = catalogs[catalog_version]
            assert metric_id in catalog.metrics_by_id, role["role"]
    for role in profile["dimension_roles"]:
        for catalog_version, dimension in role["bindings"].items():
            catalog = catalogs[catalog_version]
            assert any(
                dimension == "date" or dimension in metric.allowed_dimensions
                for metric in catalog.metrics
            ), role["role"]


def test_repeated_and_large_groups_have_executable_rules() -> None:
    from data_analysis_agent.question_router import QuestionRouter
    from data_analysis_agent.semantic_catalog import CatalogRetriever
    from vanna.core.user import User

    catalog = CatalogLoader(workspace=OLIST_RUNTIME_WORKSPACE).load()
    router = QuestionRouter(CatalogRetriever(catalog))
    user = User(id="coverage-analyst", group_memberships=["analyst"])
    for question in ("不同支付方式的 GMV", "各品类有效订单数", "按客户城市统计销量"):
        route = router.admit_sql_request(question, user=user)
        assert route.should_generate_sql is True, question


def test_online_runtime_uses_the_wider_olist_catalog() -> None:
    catalog = CatalogLoader(workspace=OLIST_RUNTIME_WORKSPACE).load()

    assert OLIST_RUNTIME_WORKSPACE.workspace_id == "olist-demo"
    assert catalog.metric_version == "0.3-proposal"
    assert len(catalog.metrics) >= 19
    assert "seller_state" in catalog.metrics_by_id["item_count"].allowed_dimensions
    assert "销量" in catalog.metrics_by_id["item_count"].aliases
