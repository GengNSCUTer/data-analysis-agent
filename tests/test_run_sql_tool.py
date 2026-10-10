"""Regression tests for UI handling of read-only SQL query forms."""

from __future__ import annotations

import pandas as pd
import pytest

from vanna.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory import DemoAgentMemory
from vanna.integrations.local.file_system import LocalFileSystem
from vanna.tools import RunSqlTool


class DataFrameRunner(SqlRunner):
    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        return pd.DataFrame([{"customer_state": "SP", "paid_order_count": 10}])


class LargeDataFrameRunner(SqlRunner):
    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        return pd.DataFrame({"row_number": list(range(250))})


class EmptyDataFrameRunner(SqlRunner):
    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        return pd.DataFrame()


@pytest.mark.asyncio
async def test_read_only_cte_renders_as_a_dataframe(tmp_path) -> None:
    context = ToolContext(
        user=User(id="test-user", group_memberships=["analyst"]),
        conversation_id="test-conversation",
        request_id="test-request",
        agent_memory=DemoAgentMemory(),
    )
    result = await RunSqlTool(
        sql_runner=DataFrameRunner(), file_system=LocalFileSystem(str(tmp_path))
    ).execute(
        context,
        RunSqlToolArgs(
            sql=(
                "WITH orders_by_state AS (SELECT customer_state, COUNT(*) AS paid_order_count "
                "FROM analytics.fact_orders GROUP BY customer_state) "
                "SELECT customer_state, paid_order_count FROM orders_by_state"
            )
        ),
    )

    assert result.success is True
    assert result.metadata["query_type"] == "WITH"
    assert result.metadata["row_count"] == 1
    assert result.ui_component is not None
    assert result.ui_component.rich_component.type == "dataframe"


@pytest.mark.asyncio
async def test_empty_result_does_not_expose_generic_export(tmp_path) -> None:
    context = ToolContext(
        user=User(id="empty-result-user", group_memberships=["analyst"]),
        conversation_id="empty-result-conversation",
        request_id="empty-result-request",
        agent_memory=DemoAgentMemory(),
    )
    result = await RunSqlTool(
        sql_runner=EmptyDataFrameRunner(), file_system=LocalFileSystem(str(tmp_path))
    ).execute(context, RunSqlToolArgs(sql="SELECT 1 WHERE FALSE"))

    assert result.success is True
    assert result.ui_component.rich_component.exportable is False


@pytest.mark.asyncio
async def test_large_result_keeps_full_artifact_but_limits_ui_preview(tmp_path) -> None:
    context = ToolContext(
        user=User(id="large-result-user", group_memberships=["analyst"]),
        conversation_id="large-result-conversation",
        request_id="large-result-request",
        agent_memory=DemoAgentMemory(),
        metadata={"display_result_row_budget": 200},
    )
    result = await RunSqlTool(
        sql_runner=LargeDataFrameRunner(), file_system=LocalFileSystem(str(tmp_path))
    ).execute(context, RunSqlToolArgs(sql="SELECT row_number FROM analytics.rows"))

    assert result.success is True
    assert result.metadata["row_count"] == 250
    assert len(result.metadata["results"]) == 250
    assert len(result.metadata["display_results"]) == 200
    assert result.metadata["display_truncated"] is True
    component = result.ui_component.rich_component
    assert len(component.rows) == 200
    assert component.row_count == 200
    assert component.total_row_count == 250
    assert component.preview_row_count == 200
    assert component.display_truncated is True
    assert component.exportable is False
    assert component.max_rows_displayed == 200
    assert "previewing first 200 rows" in component.description
