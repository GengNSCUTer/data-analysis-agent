"""Live browser checks for defects discovered in product development cases."""
from __future__ import annotations

import json
import io
import os

import pandas as pd
import psycopg2
import pytest

if os.getenv("RUN_VANNA_E2E") != "1":
    pytest.skip("Set RUN_VANNA_E2E=1 for live product checks", allow_module_level=True)

from test_analysis_memory_acceptance import LiveConversation, page as page
from data_analysis_agent.postgres_runner import PostgresConnectionSettings
from test_trusted_embedded_window import BASE_URL
from scripts.audit_product_release_dev import SCOPES, compare_frames, reference_sql

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(("name", "question", "check_id"), [
    ("category-null-group", "按商品品类统计有效订单数并生成柱状图", "chart_category_orders"),
    ("fractional-delivery", "按州统计平均履约天数并画柱状图", "chart_state_delivery"),
])
def test_live_chart_result_matches_independent_values(page, name, question, check_id):
    browser, errors = page
    live = LiveConversation(browser, name)
    detail = live.send(name, question, sql=True, chart="bar", terminal="completed")
    assert detail["trusted_result_artifact"] is not None
    response = browser.request.get(f"{BASE_URL}/api/project/conversations/{live.cid}/result.csv")
    assert response.ok
    observed = pd.read_csv(io.StringIO(response.text()))
    settings = PostgresConnectionSettings.from_environment()
    connection = psycopg2.connect(host=settings.host, port=settings.port,
                                 database=settings.database, user=settings.reader_user)
    connection.set_session(readonly=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute(reference_sql(SCOPES[check_id]))
            expected = pd.DataFrame(cursor.fetchall(), columns=[col.name for col in cursor.description])
    finally:
        connection.close()
    result = compare_frames(expected, observed, SCOPES[check_id][0])
    (live.output / "independent-values.json").write_text(json.dumps({
        "check_id": check_id, "result": result, "row_count": len(observed),
    }))
    assert result == "match"
    if check_id == "chart_category_orders":
        assert len(observed) == 74
        assert observed["product_category_name"].isna().sum() == 1
        chart = live.chat.locator(".js-plotly-plot").last
        chart.wait_for(state="visible")
        assert chart.evaluate("el => el.data[0].x.includes('（空值）')")
    live.finish(errors)


def test_live_null_aggregate_is_explained_without_a_trusted_numeric_result(page):
    browser, errors = page
    live = LiveConversation(browser, "null-aggregate")
    detail = live.send("empty-state", "查询不存在的 ZZ 州 GMV。没有匹配记录时保留空值，不要把空值替换成0。", terminal="completed")
    trace = live.records[-1]["catalog_trace"]
    assert live.records[-1]["sql_calls_used"] == 1
    assert not trace.get("result_contract_satisfied")
    assert not detail["trusted_result_artifact"]
    live.chat.get_by_text("本次查询没有可用的指标值", exact=False).first.wait_for(state="visible")
    live.finish(errors)
