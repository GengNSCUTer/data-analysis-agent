from __future__ import annotations

import json
from pathlib import Path
from dataclasses import replace
from unittest.mock import Mock

import pytest

from data_analysis_agent.postgres_runner import SecurePostgresRunner
from data_analysis_agent.result_budget import (
    ResultBudget, ResultBudgetError, validate_runtime_result_budget,
)
from data_analysis_agent.sql_policy import PolicyViolation
from vanna.capabilities.sql_runner import RunSqlToolArgs
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory import DemoAgentMemory


ROOT = Path(__file__).resolve().parents[1]


def test_result_budget_manifest_freezes_separate_modes_and_budgets() -> None:
    manifest = json.loads(
        (ROOT / "data" / "manifest" / "result-budget-v2.json").read_text(encoding="utf-8")
    )

    assert manifest["contract_id"] == "result-budget-v2"
    assert manifest["query_modes"] == ["aggregate", "detail_page", "export"]
    assert manifest["budgets"]["llm_context_rows"] == 8
    assert manifest["budgets"]["preview_rows"] == 200
    assert manifest["budgets"]["detail_page_rows_hard_cap"] == 500
    assert manifest["current_online_compatibility"]["legacy_execution_cap"] == 1000
    assert manifest["budgets"]["aggregate_execution_max_rows"] == 100_000
    actual = ResultBudget.aggregate()
    for key in ("preview_rows", "llm_context_rows", "llm_context_chars"):
        assert getattr(actual, key) == manifest["budgets"][key]
    assert actual.execution_max_rows == manifest["current_online_compatibility"]["legacy_execution_cap"]


def test_perf_shop_fixture_is_isolated_from_business_workspaces() -> None:
    sql = (ROOT / "infra" / "postgres" / "perf_shop.sql").read_text(encoding="utf-8")

    assert "CREATE SCHEMA perf" in sql
    assert "CREATE ROLE perf_shop_reader" in sql
    assert "analytics." not in sql
    assert "data_analysis_agent" not in sql
    assert "generate_series" in sql


def budget_metadata(budget: ResultBudget | None = None) -> dict:
    budget = budget or ResultBudget.aggregate()
    return {**budget.as_metadata(), "query_plan": budget.as_metadata()}


def test_budget_evidence_names_each_layer_without_changing_contract_payload() -> None:
    budget = ResultBudget.aggregate(execution_max_rows=100_000)
    evidence = budget.as_evidence()

    assert evidence["process_row_ceiling"] == 100_000
    assert evidence["query_result_row_ceiling"] == 100_000
    assert evidence["frontend_preview_row_limit"] == 200
    assert evidence["offline_sample_row_limit"] == 8
    assert "model_summary_row_limit" not in evidence
    assert set(budget.as_metadata()["result_budget"]) != set(evidence)


@pytest.mark.parametrize("mode", ["aggregate", "detail_page", "export"])
def test_query_modes_have_distinct_roundtrippable_budgets(mode):
    budget = ResultBudget(
        query_mode=mode,
        aggregate_result_rows=1_000 if mode == "aggregate" else None,
        detail_page_rows=200 if mode == "detail_page" else None,
    )
    assert ResultBudget.from_metadata(budget.as_metadata()) == budget
    if mode != "aggregate":
        with pytest.raises(ResultBudgetError, match="not enabled"):
            validate_runtime_result_budget(budget_metadata(budget), expected=ResultBudget.aggregate())


@pytest.mark.parametrize("changes", [
    {"query_mode": "raw"}, {"contract_id": "v0"},
    {"execution_max_rows": True}, {"statement_timeout_ms": 0},
    {"aggregate_result_rows": None}, {"aggregate_result_rows": 1_001},
    {"preview_rows": 201}, {"llm_context_rows": 9}, {"llm_context_chars": 1_201},
    {"llm_context_rows": 1.5}, {"detail_page_rows": 200}, {"export_max_bytes": 1},
    {"query_mode": "detail_page", "aggregate_result_rows": None, "detail_page_rows": 501},
])
def test_invalid_execution_envelopes_fail_explicitly(changes):
    with pytest.raises(ResultBudgetError):
        replace(ResultBudget.aggregate(), **changes)


def test_legacy_context_and_valid_server_context_are_distinct():
    budget = ResultBudget.aggregate()
    assert validate_runtime_result_budget({}, expected=budget) is None
    assert validate_runtime_result_budget({"query_plan": {"metric_ids": ["gmv"]}}, expected=budget) is None
    assert validate_runtime_result_budget(budget_metadata(), expected=budget) == budget


@pytest.mark.parametrize("mutation", [
    "missing_plan", "missing_contract", "missing_mode", "missing_field",
    "extra_field", "null_budget", "mode_mismatch", "row_mismatch", "settings_drift",
])
def test_partial_mutated_or_drifted_metadata_is_rejected(mutation):
    metadata = budget_metadata()
    if mutation == "missing_plan":
        metadata.pop("query_plan")
    elif mutation == "missing_contract":
        metadata.pop("result_budget")
        metadata.pop("query_mode")
    elif mutation == "missing_mode":
        metadata.pop("query_mode")
    elif mutation == "missing_field":
        metadata["result_budget"].pop("preview_rows")
    elif mutation == "extra_field":
        metadata["result_budget"]["client_max_rows"] = 999_999
    elif mutation == "null_budget":
        metadata["result_budget"] = None
    elif mutation == "mode_mismatch":
        metadata["query_mode"] = "export"
        metadata["result_budget"]["aggregate_result_rows"] = None
    elif mutation == "row_mismatch":
        metadata["result_budget"]["execution_max_rows"] = 2_000
    else:
        metadata = budget_metadata(ResultBudget.aggregate(statement_timeout_ms=10_000))
    with pytest.raises(ResultBudgetError):
        validate_runtime_result_budget(metadata, expected=ResultBudget.aggregate())


@pytest.mark.parametrize("failure", ["page", "export", "missing_pair", "ceiling_drift"])
def test_runner_stops_before_policy_and_reader_and_audits_budget_rejection(monkeypatch, failure):
    from data_analysis_agent import postgres_runner

    runner = SecurePostgresRunner()
    budget = ResultBudget.aggregate()
    if failure in ("page", "export"):
        budget = replace(
            budget, query_mode="detail_page" if failure == "page" else "export",
            aggregate_result_rows=None, detail_page_rows=200 if failure == "page" else None,
        )
    metadata = budget_metadata(budget)
    if failure == "missing_pair":
        metadata.pop("query_plan")
    if failure == "ceiling_drift":
        metadata = budget_metadata(ResultBudget.aggregate(execution_max_rows=200_000))
    context = ToolContext(
        user=User(id="budget-test", group_memberships=["analyst"]),
        conversation_id="budget-test", request_id="budget-test",
        agent_memory=DemoAgentMemory(), metadata=metadata,
    )
    audit = Mock()
    policy = Mock()
    connection = Mock(side_effect=AssertionError("reader must not connect"))
    monkeypatch.setattr(runner.audit, "record", audit)
    monkeypatch.setattr(runner.policy, "evaluate", policy)
    monkeypatch.setattr(postgres_runner.psycopg2, "connect", connection)

    with pytest.raises(PolicyViolation, match="result_budget:"):
        runner._run_sql_sync(RunSqlToolArgs(sql="SELECT 1 AS gmv"), context)
    policy.assert_not_called()
    connection.assert_not_called()
    assert audit.call_args.kwargs["status"] == "rejected"
    assert audit.call_args.kwargs["reason"].startswith("result_budget:")
    assert context.metadata["result_budget_check"]["state"] == "rejected"
