from __future__ import annotations

import pandas as pd
import pytest

from data_analysis_agent.result_validator import ResultValidationError, ResultValidator
from data_analysis_agent.sql_policy import SqlPolicy
from data_analysis_agent.sql_repair import SafeSqlExecutionError, SanitizedSqlError
from data_analysis_agent.trusted_sql_tool import TrustedRunSqlTool
from vanna.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory import DemoAgentMemory
from vanna.integrations.local.file_system import LocalFileSystem


def _context() -> ToolContext:
    return ToolContext(
        user=User(id="trusted-tool-test", group_memberships=["analyst"]),
        conversation_id="trusted-tool-conversation",
        request_id="trusted-tool-request",
        agent_memory=DemoAgentMemory(),
        metadata={
            "required_result_columns": ["paid_order_count"],
            "metric_result_columns": ["paid_order_count"],
            "catalog_context": "analytics.fact_orders.order_id",
        },
    )


class SequenceRunner(SqlRunner):
    def __init__(self, responses: list[object]):
        self.responses = list(responses)
        self.calls: list[str] = []
        self.validator = ResultValidator()

    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        self.calls.append(args.sql)
        response = self.responses.pop(0)
        if isinstance(response, SafeSqlExecutionError):
            context.metadata["safe_sql_error"] = response.error
            context.metadata["sql_error"] = response.error.as_dict()
            raise response
        frame = response
        validation = self.validator.validate(
            frame,
            required_columns=context.metadata.get("required_result_columns", ()),
            metric_columns=context.metadata.get("metric_result_columns", ()),
        )
        context.metadata["result_validation"] = validation.as_dict()
        if not validation.safe_to_answer:
            raise ResultValidationError(validation)
        return frame


class FakeRepairProvider:
    def __init__(self, candidate: str | None):
        self.candidate = candidate
        self.prompts: list[str] = []

    async def __call__(self, prompt: str, context: ToolContext) -> str | None:
        self.prompts.append(prompt)
        return self.candidate


def _tool(runner: SequenceRunner, provider: FakeRepairProvider | None, tmp_path):
    return TrustedRunSqlTool(
        runner,
        repair_provider=provider,
        repair_policy=SqlPolicy(),
        file_system=LocalFileSystem(str(tmp_path)),
    )


@pytest.mark.asyncio
async def test_normal_query_passes_result_contract(tmp_path) -> None:
    runner = SequenceRunner([pd.DataFrame({"paid_order_count": [7]})])
    result = await _tool(runner, None, tmp_path).execute(
        _context(),
        RunSqlToolArgs(
            sql="SELECT COUNT(order_id) AS paid_order_count FROM fact_orders"
        ),
    )

    assert result.success is True
    assert runner.calls == [
        "SELECT COUNT(order_id) AS paid_order_count FROM fact_orders"
    ]


@pytest.mark.asyncio
async def test_execution_error_is_repaired_once_and_reexecuted(tmp_path) -> None:
    error = SafeSqlExecutionError(
        SanitizedSqlError(
            category="unknown_column",
            public_reason="生成的查询引用了不存在或不可用的列。",
            retryable=True,
        )
    )
    runner = SequenceRunner([error, pd.DataFrame({"paid_order_count": [7]})])
    provider = FakeRepairProvider(
        "SELECT COUNT(order_id) AS paid_order_count FROM fact_orders"
    )
    context = _context()
    result = await _tool(runner, provider, tmp_path).execute(
        context, RunSqlToolArgs(sql="SELECT missing_column FROM fact_orders")
    )

    assert result.success is True
    assert len(provider.prompts) == 1
    assert runner.calls == [
        "SELECT missing_column FROM fact_orders",
        "SELECT COUNT(order_id) AS paid_order_count FROM fact_orders",
    ]
    evidence = result.metadata["sql_repair"]
    assert evidence["repair_attempted"] is True
    assert evidence["repair_error_category"] == "unknown_column"
    assert evidence["repair_execution_status"] == "succeeded"
    assert evidence["terminal_reason"] is None


@pytest.mark.asyncio
async def test_unsafe_repair_candidate_is_rejected_without_second_execution(tmp_path) -> None:
    error = SafeSqlExecutionError(
        SanitizedSqlError("unknown_column", "列不可用。", True)
    )
    runner = SequenceRunner([error])
    provider = FakeRepairProvider("DELETE FROM fact_orders")
    context = _context()
    result = await _tool(runner, provider, tmp_path).execute(
        context, RunSqlToolArgs(sql="SELECT missing_column FROM fact_orders")
    )

    assert result.success is False
    assert runner.calls == ["SELECT missing_column FROM fact_orders"]
    assert result.metadata["sql_repair"]["repair_reason"] == (
        "repaired_sql_rejected_by_policy"
    )
    assert result.metadata["terminal_reason"] == "repaired_sql_rejected_by_policy"


@pytest.mark.asyncio
async def test_executable_result_missing_metric_is_terminal_without_repair(tmp_path) -> None:
    runner = SequenceRunner([pd.DataFrame({"wrong_column": [7]})])
    provider = FakeRepairProvider(
        "SELECT COUNT(order_id) AS paid_order_count FROM fact_orders"
    )
    context = _context()
    result = await _tool(runner, provider, tmp_path).execute(
        context,
        RunSqlToolArgs(sql="SELECT COUNT(order_id) AS wrong_column FROM fact_orders"),
    )

    assert result.success is False
    assert provider.prompts == []
    assert result.metadata["terminal_reason"] == "result_validation_failed"
    assert context.metadata["result_validation"]["missing_columns"] == [
        "paid_order_count"
    ]
