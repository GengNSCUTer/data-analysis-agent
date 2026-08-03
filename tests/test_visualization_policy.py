"""Regression coverage for chart artifact constraints."""

from __future__ import annotations

import pytest

from data_analysis_agent.visualization import TrustedVisualizeDataTool
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory import DemoAgentMemory
from vanna.integrations.local.file_system import LocalFileSystem
from vanna.tools.visualize_data import VisualizeDataArgs


@pytest.fixture()
def context() -> ToolContext:
    return ToolContext(user=User(id="chart-user", group_memberships=["analyst"]), conversation_id="chart", request_id="chart", agent_memory=DemoAgentMemory())


@pytest.mark.asyncio
async def test_chart_rejects_file_names_not_emitted_by_run_sql(tmp_path, context: ToolContext) -> None:
    result = await TrustedVisualizeDataTool(LocalFileSystem(str(tmp_path))).execute(context, VisualizeDataArgs(filename="orders.csv"))
    assert result.success is False
    assert result.metadata["error_type"] == "chart_policy"


@pytest.mark.asyncio
async def test_chart_builds_plotly_component_from_compact_result(tmp_path, context: ToolContext) -> None:
    file_system = LocalFileSystem(str(tmp_path))
    await file_system.write_file("query_results_1234abcd.csv", "state,orders\nSP,10\nRJ,5\n", context)
    result = await TrustedVisualizeDataTool(file_system).execute(context, VisualizeDataArgs(filename="query_results_1234abcd.csv", title="州订单"))
    assert result.success is True
    assert result.ui_component is not None
    assert result.ui_component.rich_component.type == "chart"
    assert result.metadata["rows"] == 2
