"""Live multi-turn observation of interpretation, plan, checks and execution."""
from __future__ import annotations

import json
import os

import psycopg2
import psycopg2.extras
import pytest

if os.getenv("RUN_VANNA_E2E") != "1":
    pytest.skip("Set RUN_VANNA_E2E=1 for live browser checks", allow_module_level=True)

from test_analysis_memory_acceptance import LiveConversation, _update_owned_conversation
from test_catalog_filter_drilldown import _check_reader_values
from test_trusted_embedded_window import page as page
from data_analysis_agent.postgres_runner import PostgresConnectionSettings

pytestmark = pytest.mark.integration


def _observe(live, label, question, *, gold=False, **kwargs):
    detail = live.send(label, question, **kwargs)
    run = live.records[-1]
    settings = PostgresConnectionSettings.from_environment()
    with psycopg2.connect(host=settings.host, port=settings.port, dbname=settings.database,
                          user=settings.writer_user) as connection:
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(
                "SELECT audit_id, original_sql, final_sql, policy_status, policy_reason, row_count, elapsed_ms "
                "FROM app.query_audits WHERE run_id=%s ORDER BY audit_id", (run["run_id"],),
            )
            attempts = [dict(row) for row in cursor.fetchall()]
    trace = run["catalog_trace"]
    stages = {
        "question": question, "state": detail["analysis_state"],
        "interpretation": trace.get("semantic_intent"),
        "route": {k: v for k, v in trace.items() if k.startswith("route_")},
        "reference": trace.get("result_reference"), "reuse": trace.get("result_reuse"),
        "query_plan": trace.get("query_plan"), "chart": trace.get("chart_contract"),
        "repair": run.get("repair_evidence"), "attempts": attempts,
        "result_contract_satisfied": trace.get("result_contract_satisfied"),
        "termination_reason": run["termination_reason"], "latency_ms": run["latency_ms"],
    }
    path = live.output / f"{len(live.records):02d}-stages.json"
    path.write_text(json.dumps(stages, ensure_ascii=False, indent=2, default=str))
    path.chmod(0o600)
    if gold and trace.get("result_contract_satisfied"):
        _check_reader_values(live)
    return detail


def test_live_continuous_analysis_constraints(page):
    browser, errors = page
    live = LiveConversation(browser, "flow-continuous")
    _observe(live, "state", "哪个州销售额最高", sql=True, gold=True,
             expected={"metric_ids": ["gmv"], "dimensions": ["customer_state"], "top_n": 1})
    _observe(live, "category", "这个州哪些商品种类卖得好", sql=True, gold=True,
             expected={"dimensions": ["product_category_name"], "filters": ["customer_state=SP"], "top_n": None})
    _observe(live, "ten", "排名前十的商品种类都给一下", sql=True, gold=True,
             expected={"top_n": 10, "filters": ["customer_state=SP"]})
    _observe(live, "five", "只保留前五名，其他条件不变", sql=True, gold=True,
             expected={"top_n": 5, "filters": ["customer_state=SP"]})
    _observe(live, "chart", "换成横向条形图，不用重新查询", sql=False, reuse=True, chart="horizontal_bar")
    _observe(live, "items", "还是这些条件，改看商品销量", sql=True, gold=True,
             expected={"metric_ids": ["item_count"], "top_n": 5, "filters": ["customer_state=SP"]})
    _observe(live, "rj", "州换成RJ，其他不动", sql=True, gold=True,
             expected={"metric_ids": ["item_count"], "top_n": 5, "filters": ["customer_state=RJ"]})
    _observe(live, "all", "取消州限制，仍按销量看前五个品类", sql=True, gold=True,
             expected={"metric_ids": ["item_count"], "top_n": 5, "filters": []})
    _observe(live, "explain", "帮我解释一下刚才的结果", sql=False, reuse=True)
    live.finish(errors)


def test_live_partitioned_top_n_shapes(page):
    """The live model must execute partitioned, bounded ranking shapes."""
    browser, errors = page
    by_state = LiveConversation(browser, "flow-partitioned-topn")
    _observe(
        by_state,
        "state-category-top-five",
        "每个州销售额最高的前五个商品品类",
        sql=True,
        expected={
            "metric_ids": ["gmv"],
            "dimensions": ["customer_state", "product_category_name"],
            "top_n": 5,
        },
    )
    plan = by_state.records[-1]["catalog_trace"]["query_plan"]
    assert plan["top_n_partition_dimensions"] == ["customer_state"]
    assert by_state.executions()[-1]["row_count"] <= 27 * 5
    by_state.finish(errors)

    by_month = LiveConversation(browser, "flow-time-partitioned-topn")
    _observe(
        by_month,
        "month-category-top-five",
        "2017年每月销售额最高的前五个商品品类",
        sql=True,
        expected={
            "metric_ids": ["gmv"],
            "dimensions": ["product_category_name"],
            "time_grain": "month",
            "top_n": 5,
        },
    )
    plan = by_month.records[-1]["catalog_trace"]["query_plan"]
    assert plan["top_n_partition_dimensions"] == ["time"]
    assert by_month.executions()[-1]["row_count"] <= 12 * 5
    by_month.finish(errors)


def test_live_reference_beyond_summary_rows(page):
    browser, errors = page
    live = LiveConversation(browser, "flow-rank-reference")
    _observe(live, "ten", "SP州按销售额列前十个商品品类", sql=True, gold=True)
    _observe(live, "tenth", "排名第十的那个品类，单独看它在这个州的销售额", sql=True, gold=True)
    assert any(f.startswith("product_category_name=") for f in live.detail()["analysis_state"]["filters"])
    live.finish(errors)


def test_live_previous_success_after_failed_scope_change(page):
    browser, errors = page
    live = LiveConversation(browser, "flow-failed-scope")
    _observe(live, "ten", "SP州按销售额列前十个商品品类", sql=True, gold=True)
    artifact_id = live.detail()["trusted_result_artifact"]["artifact_id"]

    def failed_attempt(conversation):
        # Controlled failure at a different requested scope. The follow-up uses
        # the real model, API and artifact store, not a special runtime endpoint.
        state = conversation.metadata["working_memory"]
        state.update(status="failed", top_n=5, state_version=state["state_version"] + 1)
    _update_owned_conversation(live.cid, failed_attempt)
    _observe(live, "previous-chart", "刚才的新查询没成功，先把之前成功的那张表画成横向条形图，不要重新查", sql=False,
             reuse=True, chart="horizontal_bar", expected={"top_n": 5})
    assert live.detail()["trusted_result_artifact"]["artifact_id"] == artifact_id
    _observe(live, "retry-five", "好了，接着查刚才没成功的前五名", sql=True, gold=True,
             expected={"metric_ids": ["gmv"], "top_n": 5, "filters": ["customer_state=SP"]})
    live.finish(errors)
