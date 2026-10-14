"""Output completeness, batch reads and immutable result navigation."""
import json
from time import perf_counter
from unittest.mock import Mock

import pandas as pd
import pytest

from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE
from data_analysis_agent.postgres_runner import read_bounded_rows
from data_analysis_agent.query_plan import QueryPlan, requested_top_n
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.result_artifact_store import ResultArtifactStore
from data_analysis_agent.result_budget import ResultBudget
from data_analysis_agent.result_validator import ResultValidator, ResultValidationError
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
from data_analysis_agent.sql_policy import SqlPolicy, PolicyViolation
from vanna.core.user import User


def plan_for(question):
    workspace = OLIST_RUNTIME_WORKSPACE
    retriever = CatalogRetriever(CatalogLoader(workspace).load())
    router = QuestionRouter(retriever, max_result_rows=100_000, full_results=True)
    user = User(id="budget-test", group_memberships=["analyst"])
    selection = retriever.retrieve(question, user)
    route = router.admit_sql_request(question, user=user, selection=selection)
    assert route.should_generate_sql, route
    return QueryPlan.from_selection(
        selection, question, route,
        result_budget=ResultBudget.aggregate(execution_max_rows=100_000),
    )


def test_large_time_dimension_plan_is_not_limited_to_1000_rows():
    plan = plan_for("2017年按日统计各州有效订单数")
    assert plan.time_grain == "day"
    assert plan.result_budget.aggregate_result_rows == 365 * 27
    assert plan.result_budget.execution_max_rows == 100_000


def test_city_grouping_is_complete_unless_user_requests_top_n():
    plan = plan_for("按客户城市统计有效订单数")
    assert not plan.top_n
    assert plan.result_budget.aggregate_result_rows == 4119
    plan = plan_for("按客户城市统计有效订单数前五名")
    assert dict(plan.top_n) == {"customer_city": 5}
    assert plan.result_budget.aggregate_result_rows == 5


@pytest.mark.parametrize("text,n", [("前5名", 5), ("前五名", 5), ("前二十五名", 25), ("TOP 50", 50), ("前一百零五名", 105), ("前十万名", 100_000)])
def test_chinese_and_numeric_top_n(text, n):
    assert requested_top_n(text) == n


def test_complete_policy_replaces_incidental_limit_but_preserves_user_top_n():
    sql = "SELECT customer_city, COUNT(*) AS paid_order_count FROM dim_customers GROUP BY customer_city ORDER BY paid_order_count DESC LIMIT 1000 OFFSET 10"
    policy = SqlPolicy(analyst_limit=1000)
    full = policy.evaluate(sql, complete_result=True, result_limit=4119)
    assert "LIMIT 4119" in full.final_sql and "OFFSET" not in full.final_sql
    assert full.policy_limit_applied
    top = policy.evaluate(sql, complete_result=True, result_limit=4119, top_n=5)
    assert "LIMIT 5" in top.final_sql and not top.policy_limit_applied
    assert policy.evaluate(sql).row_limit == 1000
    with pytest.raises(PolicyViolation):
        policy.evaluate("DELETE FROM fact_orders", complete_result=True, result_limit=4119)
    with pytest.raises(PolicyViolation):
        policy.evaluate("SELECT order_id FROM fact_orders", complete_result=True, result_limit=4119)


def test_complete_aggregate_exceeds_preview_without_being_execution_truncated():
    frame = pd.DataFrame({"paid_order_count": range(1205)})
    validator = ResultValidator(1205)
    result = validator.validate(frame, metric_columns=("paid_order_count",), complete_required=True, execution_truncated=False)
    assert result.safe_to_display and result.display_truncated and not result.truncated
    overflow = validator.validate(frame, complete_required=True, execution_truncated=True)
    assert overflow.state == "needs_clarification" and not overflow.safe_to_display
    legacy = validator.validate(frame, execution_truncated=True)
    assert legacy.state == "degraded"


def test_batch_reader_does_not_fetch_the_entire_result_at_once():
    cursor = Mock()
    cursor.fetchmany.side_effect = [[{"n": n} for n in range(1000)], [{"n": n} for n in range(1000, 1206)]]
    rows, size = read_bounded_rows(cursor, probe_count=1206, max_bytes=100_000, deadline=perf_counter() + 10)
    assert len(rows) == 1206 and size < 100_000
    assert [call.args[0] for call in cursor.fetchmany.call_args_list] == [1000, 206]


@pytest.mark.parametrize("max_bytes,deadline", [(20, float("inf")), (100_000, 0)])
def test_byte_or_time_budget_cannot_return_partial_success(max_bytes, deadline):
    cursor = Mock()
    cursor.fetchmany.return_value = [{"n": "长字符串" * 20}]
    with pytest.raises(ResultValidationError) as raised:
        read_bounded_rows(cursor, probe_count=10, max_bytes=max_bytes, deadline=deadline)
    assert raised.value.validation.state == "needs_clarification"


def test_sparse_csv_pages_preserve_unicode_newlines_and_all_rows(tmp_path):
    store = ResultArtifactStore(tmp_path)
    rows = [{"n": str(n), "name": '中文\n带逗号,与"引号"'} for n in range(1205)]
    scope = dict(artifact_id="pages-test", user_id="owner", workspace_id="workspace")
    manifest = store.save_result_csv(**scope, columns=["n", "name"], rows=rows, row_count=len(rows), dataset_version="d1", metric_version="m1")
    assert len(manifest["page_offsets"]) == 7
    observed = []
    cursor = 0
    while True:
        page = store.read_csv_page(**scope, cursor=cursor)
        observed.extend(page["rows"])
        assert page["total_rows"] == 1205 and page["page_rows"] <= 200
        if not page["has_next_page"]:
            assert page["page_rows"] == 5
            break
        cursor = page["next_cursor"]
    assert observed == rows
    assert store.read_csv_page(**scope, cursor=1003, page_size=3)["rows"] == rows[1003:1006]
    with pytest.raises(FileNotFoundError):
        store.read_csv_page(**{**scope, "user_id": "other"})
    with pytest.raises(FileNotFoundError):
        store.read_csv_page(**{**scope, "workspace_id": "other"})
    with pytest.raises(ValueError):
        store.read_csv_page(**scope, page_size=501)
    # Old artifacts without the sparse index remain readable.
    path = store._scope("owner", "workspace", "pages-test") / "manifest.json"
    path.write_text(json.dumps({k: v for k, v in manifest.items() if k not in ("page_offsets", "page_index_stride")}), encoding="utf-8")
    assert store.read_csv_page(**scope, cursor=1003, page_size=3)["rows"] == rows[1003:1006]
