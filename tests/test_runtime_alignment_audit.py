from copy import deepcopy

import pytest

from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE
from data_analysis_agent.runtime_alignment_audit import compare_source_semantics, replay_single_turn
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever


@pytest.fixture(scope="module")
def retriever():
    return CatalogRetriever(CatalogLoader(OLIST_RUNTIME_WORKSPACE).load())


@pytest.fixture
def source_spec():
    return {
        "metric_ids": ["paid_order_count"], "dimension": "customer_state",
        "time": {"grain": None, "start": None, "end_exclusive": None},
        "required_result_columns": ["customer_state", "paid_order_count"],
    }


def test_context_derives_from_question_without_source_answer(retriever, source_spec):
    replay = replay_single_turn("按客户州统计有效订单数", retriever)
    assert compare_source_semantics(replay, source_spec) == ()
    other = deepcopy(source_spec)
    other["dimension"] = "product_category_name"
    other["required_result_columns"] = ["product_category_name", "paid_order_count"]
    assert "dimension_mismatch" in compare_source_semantics(replay, other)
    assert "Server-provided QuerySpec" not in replay["candidate_prompt"]
    assert replay["plan"]["dimensions"] == ["customer_state"]


@pytest.mark.parametrize("question", ["有效订单数的口径是什么", "删除订单", "本月有效订单数是多少"])
def test_non_admitted_request_never_gets_candidate_prompt(retriever, source_spec, question):
    replay = replay_single_turn(question, retriever)
    assert replay["candidate_prompt"] is None
    assert compare_source_semantics(replay, source_spec) == ("route_not_admitted",)


def test_time_mismatch_is_detected_without_injecting_spec(retriever, source_spec):
    source_spec["time"] = {"grain": None, "start": "2017-01-01", "end_exclusive": "2017-02-01"}
    replay = replay_single_turn("按客户州统计有效订单数", retriever)
    assert compare_source_semantics(replay, source_spec) == ("time_range_mismatch",)


def test_column_order_disagreement_is_not_silently_accepted(retriever, source_spec):
    source_spec["required_result_columns"].reverse()
    replay = replay_single_turn("按客户州统计有效订单数", retriever)
    assert compare_source_semantics(replay, source_spec) == ("result_column_order_mismatch",)


def test_implicit_top_n_is_not_claimed_as_source_label_alignment(retriever, source_spec):
    source_spec["dimension"] = "customer_city"
    source_spec["required_result_columns"] = ["customer_city", "paid_order_count"]
    replay = replay_single_turn("按客户城市统计有效订单数", retriever)
    assert compare_source_semantics(replay, source_spec) == ("ranking_not_represented_in_source_spec",)
