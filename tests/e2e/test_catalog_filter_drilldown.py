"""Real-model regression for composable Catalog filters and drilldowns."""
from __future__ import annotations

from decimal import Decimal
import json
import os

import psycopg2
import pytest

if os.getenv("RUN_VANNA_E2E") != "1":
    pytest.skip("Set RUN_VANNA_E2E=1 for live browser checks", allow_module_level=True)

from test_analysis_memory_acceptance import LiveConversation, BASE_URL
from test_trusted_embedded_window import page as page
from data_analysis_agent.postgres_runner import PostgresConnectionSettings

pytestmark = pytest.mark.integration


def _check_reader_values(live):
    detail = live.detail()
    state = detail["analysis_state"]
    artifact = detail["trusted_result_artifact"]
    assert artifact and state["status"] == "completed"
    actual = live.page.request.get(
        f"{BASE_URL}/api/project/conversations/{live.cid}/artifacts/{artifact['artifact_id']}/rows"
    ).json()["rows"]
    clauses = ["o.order_status NOT IN ('canceled','unavailable')"]
    params = []
    for item in state["filters"]:
        dimension, value = item.split("=", 1)
        if dimension == "customer_state":
            clauses.append("c.customer_state=%s")
            params.append(value)
        elif dimension == "product_category_name":
            clauses.append("(p.product_category_name=%s OR t.product_category_name_english=%s)")
            params.extend([value, value])
        elif dimension == "payment_type":
            clauses.append("EXISTS (SELECT 1 FROM analytics.fact_payments pay WHERE pay.order_id=o.order_id AND pay.payment_type=%s)")
            params.append(value)
        else:
            raise AssertionError(f"Add an independent check for dimension {dimension}")
    assert set(state["metric_ids"]) <= {"gmv", "item_count"}
    metrics = {"gmv": "SUM(i.price)", "item_count": "COUNT(*)"}
    fields = [f"{metrics[m]} AS {m}" for m in state["metric_ids"]]
    dims = {"customer_state": "c.customer_state", "product_category_name": "p.product_category_name"}
    group = [dims[d] for d in state["dimensions"]]
    projections = [*group, *fields]
    if "product_category_name" in state["dimensions"]:
        projections.append("MAX(t.product_category_name_english)")
    sql = (f"SELECT {', '.join(projections)} FROM analytics.fact_order_items i "
           "JOIN analytics.fact_orders o ON o.order_id=i.order_id "
           "JOIN analytics.dim_customers c ON c.customer_id=o.customer_id "
           "JOIN analytics.dim_products p ON p.product_id=i.product_id "
           "LEFT JOIN analytics.dim_category_translation t ON t.product_category_name=p.product_category_name "
           f"WHERE {' AND '.join(clauses)}")
    if group:
        sql += f" GROUP BY {', '.join(group)} ORDER BY {metrics[state['metric_ids'][0]]} DESC NULLS LAST"
    sql += " LIMIT %s"
    params.append(state["top_n"] if group else None)
    settings = PostgresConnectionSettings.from_environment()
    with psycopg2.connect(host=settings.host, port=settings.port, dbname=settings.database, user=settings.reader_user) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            gold = cursor.fetchall()
    assert len(actual) == len(gold)
    if group:
        ranked_values = [Decimal(str(row[state["metric_ids"][0]])) for row in actual]
        assert ranked_values == sorted(ranked_values, reverse=True)
    for row in actual:
        matches = [r for r in gold if all(
            row[d] == r[index] or d == "product_category_name" and (
                row[d] == r[-1] or r[index] is None and r[-1] is None and row[d] in ("", "nan", None)
            )
            for index, d in enumerate(state["dimensions"])
        )]
        assert len(matches) == 1, {"actual": row, "gold": gold}
        for offset, metric in enumerate(state["metric_ids"], len(group)):
            assert abs(Decimal(str(row[metric])) - Decimal(str(matches[0][offset]))) < Decimal("0.01")
    target = live.output / f"{len(live.records):02d}-gold.json"
    target.write_text(json.dumps({"matched": True, "actual": actual, "gold": gold}, ensure_ascii=False, indent=2, default=str))
    target.chmod(0o600)


def test_live_state_category_drilldown_and_filter_changes(page):
    browser, errors = page
    live = LiveConversation(browser, "state-category-composition")
    live.send("state-winner", "哪个州销量最好", sql=True,
              expected={"metric_ids": ["item_count"], "dimensions": ["customer_state"], "top_n": 1})
    _check_reader_values(live)
    live.send("categories", "这个州哪些商品种类卖的好，列出前十名", sql=True,
              expected={"metric_ids": ["item_count"], "dimensions": ["product_category_name"], "filters": ["customer_state=SP"], "top_n": 10})
    _check_reader_values(live)
    live.send("category-winner", "这个州卖的最好这个商品种类的相关数据给我一下", sql=True)
    filters = live.detail()["analysis_state"]["filters"]
    assert "customer_state=SP" in filters and any(f.startswith("product_category_name=") for f in filters)
    _check_reader_values(live)
    live.send("switch-metric", "范围保持不变，再看看销售额", sql=True, expected={"metric_ids": ["gmv"]})
    _check_reader_values(live)
    live.send("replace-state", "品类不变，客户州改成RJ，仍然看销售额", sql=True)
    assert "customer_state=RJ" in live.detail()["analysis_state"]["filters"]
    assert any(f.startswith("product_category_name=") for f in live.detail()["analysis_state"]["filters"])
    _check_reader_values(live)
    live.send("clear-state", "只撤销州的限制，品类和销售额保持不变", sql=True)
    assert all(not f.startswith("customer_state=") for f in live.detail()["analysis_state"]["filters"])
    assert any(f.startswith("product_category_name=") for f in live.detail()["analysis_state"]["filters"])
    _check_reader_values(live)
    live.finish(errors)


@pytest.mark.parametrize("category", ["bed_bath_table", "cama_mesa_banho"])
def test_live_explicit_composite_filter_in_both_category_encodings(page, category):
    browser, errors = page
    live = LiveConversation(browser, "explicit-category-filter")
    live.send("explicit", f"统计SP州{category}品类的商品件数和商品成交额，沿用有效订单口径", sql=True)
    _check_reader_values(live)
    live.finish(errors)


def test_live_payment_and_state_filter_keeps_item_grain(page):
    browser, errors = page
    live = LiveConversation(browser, "payment-state-composition")
    live.send("payment-state", "SP州只看使用过信用卡的订单，统计商品件数和商品成交额，仍按有效订单口径", sql=True,
              expected={"filters": ["customer_state=SP", "payment_type=credit_card"]})
    _check_reader_values(live)
    live.finish(errors)
