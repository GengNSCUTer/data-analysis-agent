"""Regression coverage for server-owned chart artifact constraints."""

from __future__ import annotations

import pytest

from data_analysis_agent.chart_contract import ChartContract
from data_analysis_agent.query_plan import QueryPlan
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.semantic_catalog import (
    CatalogLoader,
    CatalogRetriever,
    ResultContract,
)
from data_analysis_agent.visualization import TrustedVisualizeDataTool
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory import DemoAgentMemory
from vanna.integrations.local.file_system import LocalFileSystem
from vanna.tools.visualize_data import VisualizeDataArgs


@pytest.fixture()
def context() -> ToolContext:
    return ToolContext(
        user=User(id="chart-user", group_memberships=["analyst"]),
        conversation_id="chart",
        request_id="chart",
        agent_memory=DemoAgentMemory(),
    )


def _contract(question: str) -> ChartContract:
    user = User(id="chart-user", group_memberships=["analyst"])
    catalog = CatalogLoader().load()
    router = QuestionRouter(CatalogRetriever(catalog))
    selection = router.retriever.retrieve(question, user)
    route = router.classify(question, user=user, selection=selection)
    assert route.should_generate_sql
    plan = QueryPlan.from_selection(selection, question, route)
    result = ResultContract.from_selection(
        selection,
        question,
        catalog=catalog,
        required_result_columns=plan.required_result_columns,
        requested_dimensions=plan.dimensions,
    )
    return ChartContract.from_query_plan(question, plan, result)


async def _write_current_result(
    tmp_path, context: ToolContext, contract: ChartContract, csv: str
) -> str:
    filename = "query_results_1234abcd.csv"
    context.metadata["chart_contract"] = contract.as_tool_metadata()
    context.metadata["current_result_filename"] = filename
    await LocalFileSystem(str(tmp_path)).write_file(filename, csv, context)
    return filename


@pytest.mark.asyncio
async def test_chart_rejects_missing_server_contract(tmp_path, context: ToolContext) -> None:
    file_system = LocalFileSystem(str(tmp_path))
    await file_system.write_file("query_results_1234abcd.csv", "state,gmv\nSP,10\n", context)

    result = await TrustedVisualizeDataTool(file_system).execute(
        context, VisualizeDataArgs(filename="query_results_1234abcd.csv")
    )

    assert result.success is False
    assert result.metadata["error_type"] == "chart_policy"


@pytest.mark.asyncio
async def test_chart_requires_current_result_filename(tmp_path, context: ToolContext) -> None:
    contract = _contract("按州统计 GMV，并生成柱状图")
    filename = await _write_current_result(
        tmp_path, context, contract, "customer_state,gmv\nSP,10\nRJ,5\n"
    )
    file_system = LocalFileSystem(str(tmp_path))

    result = await TrustedVisualizeDataTool(file_system).execute(
        context, VisualizeDataArgs(filename=filename.replace("1234abcd", "99999999"))
    )

    assert result.success is False
    assert "当前结果文件" in result.error


@pytest.mark.asyncio
async def test_bar_chart_type_and_title_are_server_owned(tmp_path, context: ToolContext) -> None:
    contract = _contract("按州统计 GMV，并生成柱状图")
    filename = await _write_current_result(
        tmp_path, context, contract, "customer_state,gmv\nSP,10\nRJ,5\n"
    )

    result = await TrustedVisualizeDataTool(LocalFileSystem(str(tmp_path))).execute(
        context, VisualizeDataArgs(filename=filename, title="模型自定义标题")
    )

    assert result.success is True
    assert result.ui_component.rich_component.title == contract.title
    assert result.metadata["chart"]["data"][0]["type"] == "bar"
    assert result.metadata["chart"]["data"][0]["name"] == "gmv"


@pytest.mark.asyncio
async def test_line_chart_type_is_server_owned(tmp_path, context: ToolContext) -> None:
    contract = _contract("2017年按月统计 GMV，并生成折线图")
    filename = await _write_current_result(
        tmp_path,
        context,
        contract,
        "time,gmv\n2017-02-01,20\n2017-01-01,10\n",
    )

    result = await TrustedVisualizeDataTool(LocalFileSystem(str(tmp_path))).execute(
        context, VisualizeDataArgs(filename=filename)
    )

    trace = result.metadata["chart"]["data"][0]
    assert result.success is True
    assert trace["type"] == "scatter"
    assert trace["mode"] == "lines+markers"
    assert trace["x"] == ["2017-01-01", "2017-02-01"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("csv", "message"),
    (
        ("customer_state\nSP\n", "缺少图表合同字段"),
        ("customer_state,gmv\nSP,not-a-number\n", "不是完整数值列"),
        ("customer_state,gmv\nSP,10\nSP,15\n", "隐式聚合"),
        ("customer_state,gmv,other\nSP,10,x\n", "未在服务器图表合同中声明"),
    ),
)
async def test_chart_rejects_uncontracted_or_ambiguous_frames(
    tmp_path, context: ToolContext, csv: str, message: str
) -> None:
    contract = _contract("按州统计 GMV，并生成柱状图")
    filename = await _write_current_result(tmp_path, context, contract, csv)

    result = await TrustedVisualizeDataTool(LocalFileSystem(str(tmp_path))).execute(
        context, VisualizeDataArgs(filename=filename)
    )

    assert result.success is False
    assert message in result.error
