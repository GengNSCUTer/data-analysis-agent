import importlib.util
from pathlib import Path

import pandas as pd
import pytest
from sqlglot import parse_one, exp

from data_analysis_agent.runtime_workspace import load_runtime_workspace
from data_analysis_agent.semantic_catalog import CatalogLoader

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_copyable_template_loads_with_existing_runtime_and_catalog_types():
    check = script("check_workspace")
    runtime, catalog, result = check.check_configuration(ROOT / "docs/templates/workspace/workspace.yaml")
    assert not result["issues"]
    assert catalog.metrics_by_id["completed_order_count"].time_field == "orders.created_at"
    assert runtime.profile.dimension_bindings["customer_region"].sources == (("customers", "region"),)
    assert result["business_review_required"]  # Parsing must not certify business correctness.


def test_unbound_metric_dimension_is_reported():
    from dataclasses import replace
    from unittest.mock import patch
    check = script("check_workspace")
    runtime = load_runtime_workspace(ROOT / "docs/templates/workspace/workspace.yaml")
    unbound = replace(runtime, profile=replace(runtime.profile, dimension_bindings={}))
    with patch.object(check, "load_runtime_workspace", return_value=unbound):
        _, _, report = check.check_configuration(Path("unused.yaml"))
    assert report["issues"] == ["metric completed_order_count: unbound dimension customer_region"]


def test_unknown_additive_metric_is_reported():
    from dataclasses import replace
    from unittest.mock import patch
    check = script("check_workspace")
    runtime = load_runtime_workspace(ROOT / "docs/templates/workspace/workspace.yaml")
    invalid = replace(runtime, profile=replace(runtime.profile, additive_metric_ids=frozenset({"typo_metric"})))
    with patch.object(check, "load_runtime_workspace", return_value=invalid):
        _, _, report = check.check_configuration(Path("unused.yaml"))
    assert report["issues"] == ["unknown additive metrics: ['typo_metric']"]


@pytest.mark.parametrize("workspace,metric,dimension,status", [
    ("data/workspaces/thelook.yaml", "event_count", "event_traffic_source", "valid"),
    ("data/workspaces/thelook.yaml", "unique_session_count", "event_traffic_source", "unsupported"),
    ("data/workspaces/thelook.yaml", "return_rate", "customer_state", "unsupported"),
    ("data/workspaces/thelook.yaml", "average_order_value", "customer_state", "unsupported"),
    ("docs/templates/workspace/workspace.yaml", "completed_order_count", "customer_region", "valid"),
])
def test_donut_capability_comes_from_business_configuration(workspace, metric, dimension, status):
    from data_analysis_agent.chart_contract import ChartContract
    from data_analysis_agent.semantic_catalog import CatalogRetriever, ResultContract
    from data_analysis_agent.semantic_intent import SemanticIntent, select_intent_catalog
    from vanna.core.user import User

    catalog = CatalogLoader(load_runtime_workspace(ROOT / workspace).profile).load()
    intent = SemanticIntent(request_kind="data_query", metric_ids=(metric,), dimensions=(dimension,))
    selection = select_intent_catalog(intent, CatalogRetriever(catalog), User(id="chart-test", group_memberships=["analyst"]))
    columns = (dimension, metric)
    result = ResultContract.from_selection(selection, "", catalog=catalog,
                                           required_result_columns=columns, requested_dimensions=(dimension,))
    chart = ChartContract._from_shape("", (dimension,), None, (metric,), columns, result,
                                      semantic_intent={"chart_request": "donut"})
    assert chart.status == status
    if status == "valid":
        assert chart.chart_type == "donut"


def test_numeric_comparison_detects_wrong_denominator_and_missing_groups():
    compare = script("verify_workspace_onboarding").compare_values
    expected = pd.DataFrame({"region": ["A", "B"], "return_rate": [0.1, 0.2]})
    compare(expected.iloc[::-1], expected)
    with pytest.raises(AssertionError):
        compare(expected.assign(return_rate=[0.05, 0.1]), expected)
    with pytest.raises(AssertionError):
        compare(expected.iloc[:1], expected)


def test_csv_comparison_preserves_literal_city_names():
    runner = script("verify_workspace_onboarding")
    observed = runner.read_result_csv("customer_city,completed_order_count\nnull,292\nNA,2\n,1\n")
    expected = pd.DataFrame({"customer_city": ["null", "NA", None], "completed_order_count": [292, 2, 1]})
    runner.compare_values(observed, expected)


def test_pagination_numeric_strings_match_csv_numeric_values():
    compare = script("verify_workspace_onboarding").compare_values
    page = pd.DataFrame({"city": pd.Series(["A", "B"], dtype="string"),
                         "orders": pd.Series(["10", "20"], dtype="string")})
    compare(page, pd.DataFrame({"city": ["A", "B"], "orders": [10, 20]}))


def test_ranking_order_checked_in_addition_to_row_set():
    compare = script("verify_workspace_onboarding").compare_ranking
    sql = "SELECT region, amount FROM sales ORDER BY amount DESC LIMIT 2"
    compare(pd.DataFrame({"amount": [3, 1]}), sql)
    with pytest.raises(AssertionError, match="ranking"):
        compare(pd.DataFrame({"amount": [1, 3]}), sql)


def test_thelook_cases_cover_all_metrics_and_independent_readonly_references():
    from typing import get_args
    from data_analysis_agent.chart_contract import ChartType
    suite = script("verify_workspace_onboarding").load_cases(ROOT / "evals/cases/thelook_product_queries.yaml")
    assert len(suite["cases"]) == 60
    catalog = CatalogLoader(load_runtime_workspace(ROOT / "data/workspaces/thelook.yaml").profile).load()
    tested = set()
    for case in suite["cases"]:
        if case.get("chart"):
            assert case["chart"] in get_args(ChartType)
        if "reference_sql" not in case:
            continue
        tree = parse_one(case["reference_sql"], read="postgres")
        assert isinstance(tree, exp.Query)
        assert all(table.db in ("", "analytics") for table in tree.find_all(exp.Table))
        tested.update(alias for alias in tree.named_selects if alias in catalog.metrics_by_id)
    assert tested == set(catalog.metrics_by_id)


def test_continuous_cases_keep_order_and_use_independent_expected_sql():
    suite = script("verify_workspace_onboarding").load_cases(ROOT / "evals/cases/thelook_product_queries.yaml")
    flows = {}
    for case in suite["cases"]:
        if "conversation_group" in case:
            flows.setdefault(case["conversation_group"], []).append(case)
    assert sorted(map(len, flows.values())) == [5, 5]
    assert sum(bool(case.get("reuse")) for case in suite["cases"]) == 1
    assert "best_state" in flows["sales-drilldown"][1]["reference_sql"]


def test_targeted_followup_includes_its_conversation_and_rejects_unknown_ids():
    runner = script("verify_workspace_onboarding")
    suite = runner.load_cases(ROOT / "evals/cases/thelook_product_queries.yaml")
    groups = runner.case_groups(suite, ["continuous-04"])
    assert len(groups) == 1 and len(groups[0]) == 5
    assert groups[0][0]["id"] == "continuous-01"
    with pytest.raises(ValueError, match="Unknown"):
        runner.case_groups(suite, ["misspelled-case"])
