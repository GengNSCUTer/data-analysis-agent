from __future__ import annotations

import pandas as pd
import pytest

from data_analysis_agent.result_validator import ResultValidationError, ResultValidator
from data_analysis_agent.result_budget import ResultBudget
from data_analysis_agent.result_artifact_store import ResultArtifactStore
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
            "dataset_version_id": "test-data-v1",
            "metric_version": "test-metrics-v1",
        },
    )


def test_result_metadata_comes_from_audited_order_and_only_bounded_entities():
    context = _context()
    context.metadata.update({
        "query_audit_id": 42,
        "query_plan": {"dimensions": ["customer_state"]},
        "audited_sql": "SELECT customer_state, SUM(price) AS gmv FROM fact_order_items GROUP BY customer_state ORDER BY gmv DESC",
    })
    rows = [{"customer_state": f"S{i}", "gmv": 100-i, "private": "hidden"} for i in range(12)]
    meta = TrustedRunSqlTool._result_metadata(context, ["customer_state", "gmv", "private"], rows)
    assert meta["execution_id"] == "audit-42"
    assert meta["ordering"] == [{"column": "gmv", "direction": "desc", "nulls_first": True}]
    assert meta["entities"] == [{"customer_state": f"S{i}"} for i in range(8)]
    assert "hidden" not in str(meta)
    context.metadata["audited_sql"] = "SELECT customer_state, SUM(price) AS gmv FROM fact_order_items GROUP BY customer_state"
    assert TrustedRunSqlTool._result_metadata(context, ["customer_state", "gmv"], rows)["ordering"] == []


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
        if not validation.safe_to_display:
            raise ResultValidationError(validation)
        if validation.dropped_columns:
            frame = frame.drop(columns=list(validation.dropped_columns), errors="ignore")
        if not validation.safe_to_answer:
            context.metadata["result_display_notice"] = validation.reason
        return frame


class UnvalidatedRunner(SqlRunner):
    """Deliberately bypasses validation to prove artifact persistence is closed."""

    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        return pd.DataFrame({"paid_order_count": [7]})


class FakeRepairProvider:
    def __init__(self, candidate: str | None):
        self.candidate = candidate
        self.prompts: list[str] = []

    async def __call__(self, prompt: str, context: ToolContext) -> str | None:
        self.prompts.append(prompt)
        return self.candidate


@pytest.mark.asyncio
async def test_result_budget_rejection_does_not_invoke_sql_repair(monkeypatch, tmp_path):
    from data_analysis_agent import postgres_runner

    runner = postgres_runner.SecurePostgresRunner()
    provider = FakeRepairProvider("SELECT 7 AS paid_order_count")
    context = _context()
    envelope = ResultBudget(query_mode="export", aggregate_result_rows=None).as_metadata()
    context.metadata.update({**envelope, "query_plan": envelope})
    monkeypatch.setattr(runner.audit, "record", lambda *args, **kwargs: None)

    def forbidden_connection(*args, **kwargs):
        raise AssertionError("budget rejection must not open a reader connection")

    monkeypatch.setattr(postgres_runner.psycopg2, "connect", forbidden_connection)
    tool = TrustedRunSqlTool(
        runner, repair_provider=provider, file_system=LocalFileSystem(str(tmp_path))
    )
    result = await tool.execute(context, RunSqlToolArgs(sql="SELECT 7 AS paid_order_count"))
    assert result.success is False
    assert "export is not enabled" in result.error
    assert provider.prompts == []
    assert context.metadata["result_budget_check"]["state"] == "rejected"


def _tool(
    runner: SequenceRunner,
    provider: FakeRepairProvider | None,
    tmp_path,
    *,
    result_artifact_store: ResultArtifactStore | None = None,
):
    return TrustedRunSqlTool(
        runner,
        repair_provider=provider,
        repair_policy=SqlPolicy(),
        file_system=LocalFileSystem(str(tmp_path)),
        result_artifact_store=result_artifact_store,
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
    presentation = result.metadata["result_presentation"]
    assert presentation["total_row_count"] == 1
    assert presentation["preview_row_count"] == 1
    assert presentation["display_truncated"] is False
    assert presentation["preview_export_allowed"] is False
    assert presentation["full_export_allowed"] is False
    assert result.ui_component.rich_component.total_row_count == 1
    assert result.ui_component.rich_component.preview_row_count == 1
    assert result.ui_component.rich_component.exportable is False


@pytest.mark.asyncio
async def test_presenter_carries_persisted_artifact_reference(tmp_path) -> None:
    runner = SequenceRunner([pd.DataFrame({"paid_order_count": [7]})])
    context = _context()
    result = await _tool(
        runner,
        None,
        tmp_path,
        result_artifact_store=ResultArtifactStore(tmp_path / "artifacts"),
    ).execute(
        context,
        RunSqlToolArgs(sql="SELECT COUNT(order_id) AS paid_order_count FROM fact_orders"),
    )

    assert result.success is True
    artifact_id = context.metadata["result_artifact"]["artifact_id"]
    assert context.metadata["result_artifact"]["dataset_version"] == "test-data-v1"
    assert context.metadata["result_artifact"]["metric_version"] == "test-metrics-v1"
    assert result.metadata["result_presentation"]["artifact_id"] == artifact_id
    assert result.ui_component.rich_component.audit_ref == "trusted-tool-request"


@pytest.mark.asyncio
async def test_result_artifact_requires_explicit_validated_result_contract(tmp_path) -> None:
    runner = UnvalidatedRunner()
    context = _context()
    # Model/tool success must not be enough to externalize a result: this
    # isolated runner intentionally never sets ResultValidator metadata.
    store = ResultArtifactStore(tmp_path / "artifacts")
    result = await _tool(
        runner, None, tmp_path, result_artifact_store=store
    ).execute(
        context,
        RunSqlToolArgs(sql="SELECT COUNT(order_id) AS paid_order_count FROM fact_orders"),
    )

    assert result.success is True
    assert "result_artifact" not in context.metadata


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
    assert "result_presentation" in result.metadata


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
async def test_missing_dimension_displays_the_metric_without_a_trusted_stamp(tmp_path) -> None:
    runner = SequenceRunner([pd.DataFrame({"paid_order_count": [7]})])
    context = _context()
    context.metadata["required_result_columns"] = ["customer_state", "paid_order_count"]
    result = await _tool(runner, None, tmp_path).execute(
        context,
        RunSqlToolArgs(sql="SELECT COUNT(order_id) AS paid_order_count FROM fact_orders"),
    )

    assert result.success is True
    assert result.metadata.get("terminal_reason") is None
    assert context.metadata.get("result_contract_satisfied") is None
    assert "customer_state" in context.metadata["result_display_notice"]
    assert "customer_state" in result.ui_component.rich_component.description


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
