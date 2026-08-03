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
