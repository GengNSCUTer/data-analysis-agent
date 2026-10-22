"""Verify the shared component boundary using actual routing/plan construction."""

from dataclasses import replace
import hashlib
import json

import pytest
from vanna.core.user import User

from data_analysis_agent.candidate_sql_generator import (
    CandidateSqlGenerationError,
    build_runtime_candidate_request,
    render_runtime_candidate_prompt,
)
from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE
from data_analysis_agent.query_plan import QueryPlan
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.runtime_alignment_audit import replay_single_turn
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever, ResultContract
from data_analysis_agent.working_memory import WorkingMemory
from data_analysis_agent.result_budget import ResultBudget


@pytest.fixture(scope="module")
def retriever():
    return CatalogRetriever(CatalogLoader(OLIST_RUNTIME_WORKSPACE).load())


def runtime_facts(retriever, question="按客户州统计有效订单数"):
    user = User(id="request-test", group_memberships=["analyst"])
    selection = retriever.retrieve(question, user)
    route = QuestionRouter(retriever).admit_sql_request(question, user=user, selection=selection)
    memory = WorkingMemory().apply(question, route)
    plan = QueryPlan.from_selection(selection, question, route, memory.as_dict())
    contract = ResultContract.from_selection(
        selection, question, memory.time_range, catalog=retriever.catalog,
        required_result_columns=plan.required_result_columns, requested_dimensions=plan.dimensions,
    )
    return dict(question=question, selection=selection, route=route, query_plan=plan,
                result_contract=contract, workspace=OLIST_RUNTIME_WORKSPACE)


def test_data_replay_and_runtime_produce_identical_component_input(retriever):
    facts = runtime_facts(retriever)
    request = build_runtime_candidate_request(**facts)
    offline = replay_single_turn(facts["question"], retriever, workspace=OLIST_RUNTIME_WORKSPACE)
    prompt = render_runtime_candidate_prompt(request)
    assert offline["candidate_prompt"] == prompt
    assert offline["candidate_request"] == request.safe_evidence()
    assert request.prompt_sha256 == hashlib.sha256(prompt.encode()).hexdigest()
    assert json.loads(request.query_plan_json)["dimensions"] == ["customer_state"]
    for forbidden in ("query_spec", "join_program_id", "candidate_sql", "previous_result_summary"):
        assert forbidden not in prompt
    assert set(request.safe_evidence()) == {"prompt_version", "prompt_sha256"}


@pytest.mark.parametrize("change", ["workspace", "columns", "metrics"])
def test_mismatched_server_contracts_fail_before_generation(retriever, change):
    facts = runtime_facts(retriever)
    if change == "workspace":
        facts["workspace"] = replace(OLIST_RUNTIME_WORKSPACE, metric_version="drifted")
    elif change == "columns":
        facts["result_contract"] = replace(facts["result_contract"], required_result_columns=("gmv",))
    else:
        facts["result_contract"] = replace(facts["result_contract"], metric_ids=("gmv",))
    with pytest.raises(CandidateSqlGenerationError, match="mismatch"):
        build_runtime_candidate_request(**facts)


def test_non_database_route_is_rejected_even_with_a_valid_prior_plan(retriever):
    facts = runtime_facts(retriever)
    user = User(id="request-test", group_memberships=["analyst"])
    facts["route"] = QuestionRouter(retriever).classify("删除订单", user=user)
    with pytest.raises(CandidateSqlGenerationError, match="non-database"):
        build_runtime_candidate_request(**facts)


def test_summary_never_becomes_candidate_context(retriever):
    facts = runtime_facts(retriever)
    memory = WorkingMemory(previous_result_summary="UNTRUSTED_SENTINEL; WHERE secret=1")
    facts["query_plan"] = QueryPlan.from_selection(
        facts["selection"], facts["question"], facts["route"], memory.as_dict()
    )
    assert "UNTRUSTED_SENTINEL" not in render_runtime_candidate_prompt(
        build_runtime_candidate_request(**facts)
    )


def test_server_execution_envelope_keeps_frozen_candidate_bytes_and_hash(retriever):
    facts = runtime_facts(retriever)
    legacy_request = build_runtime_candidate_request(**facts)
    legacy_plan_prompt = facts["query_plan"].prompt_context()
    budget = ResultBudget.aggregate()
    facts["query_plan"] = replace(facts["query_plan"], result_budget=budget)
    facts["result_contract"] = replace(facts["result_contract"], result_budget=budget)
    request = build_runtime_candidate_request(**facts)

    assert facts["query_plan"].as_dict()["query_mode"] == "aggregate"
    assert facts["result_contract"].as_evidence()["result_budget"]["preview_rows"] == 200
    assert request == legacy_request
    assert facts["query_plan"].prompt_context() == legacy_plan_prompt
    assert request.prompt_sha256 == legacy_request.prompt_sha256
    assert "result_budget" not in render_runtime_candidate_prompt(request)
    assert "query_mode" not in render_runtime_candidate_prompt(request)
    assert "additive_metric_ids" not in render_runtime_candidate_prompt(request)
    assert facts["result_contract"].as_tool_metadata()["additive_metric_ids"] == list(facts["result_contract"].additive_metric_ids)


@pytest.mark.parametrize("change", ["half_present", "mode", "timeout"])
def test_candidate_builder_rejects_unsupported_or_mismatched_budget_pairs(retriever, change):
    facts = runtime_facts(retriever)
    budget = ResultBudget.aggregate()
    if change == "mode":
        budget = replace(budget, query_mode="export", aggregate_result_rows=None)
    facts["query_plan"] = replace(facts["query_plan"], result_budget=budget)
    if change != "half_present":
        contract_budget = replace(budget, statement_timeout_ms=10_000) if change == "timeout" else budget
        facts["result_contract"] = replace(facts["result_contract"], result_budget=contract_budget)
    with pytest.raises(CandidateSqlGenerationError, match="mismatch|not enabled"):
        build_runtime_candidate_request(**facts)
