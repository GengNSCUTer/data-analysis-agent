"""Trusted chart rendering for server-owned SQL result artifacts."""

from __future__ import annotations

import io
import json

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio

from vanna.capabilities.file_system import FileSystem
from vanna.components import (
    ChartComponent,
    ComponentType,
    NotificationComponent,
    SimpleTextComponent,
    UiComponent,
)
from vanna.core.tool import ToolContext, ToolResult
from vanna.tools.visualize_data import VisualizeDataArgs, VisualizeDataTool

from .chart_contract import ChartContract, ChartContractError


class TrustedVisualizeDataTool(VisualizeDataTool):
    """Render only the current query artifact under a server chart contract."""

    def __init__(self, file_system: FileSystem):
        # Keep the upstream tool interface and schema, but deliberately do not
        # call its heuristic Plotly generator. It may select a different type
        # and aggregate the result again with groupby/sum.
        super().__init__(file_system=file_system)

    @property
    def description(self) -> str:
        return (
            "Render the current run_sql result only when the server supplied a "
            "valid chart contract. Chart type and fields are server-owned."
        )

    async def execute(self, context: ToolContext, args: VisualizeDataArgs) -> ToolResult:
        contract = ChartContract.from_tool_metadata(context.metadata)
        if contract is None:
            return self._reject("本轮没有服务器图表合同，不能生成图表。")
        if not contract.safe_to_visualize:
            return self._reject(contract.clarification or "当前图表请求需要先澄清。")
        current_filename = context.metadata.get("current_result_filename")
        if not isinstance(current_filename, str) or args.filename != current_filename:
            return self._reject("只能可视化本轮 run_sql 刚刚生成的当前结果文件。")
        try:
            csv_content = await self.file_system.read_file(args.filename, context)
            frame = pd.read_csv(io.StringIO(csv_content))
            chart_frame = contract.validate_frame(frame)
            chart_dict = self._render_chart(contract, chart_frame)
        except FileNotFoundError:
            return self._reject("当前查询结果文件不存在，请重新执行受控查询。")
        except (pd.errors.ParserError, ChartContractError, ValueError) as exc:
            return self._reject(str(exc))

        row_count = len(chart_frame)
        title = contract.title or "受控分析图表"
        result = f"已按服务器图表合同生成{title}。"
        return ToolResult(
            success=True,
            result_for_llm=result,
            ui_component=UiComponent(
                rich_component=ChartComponent(
                    chart_type="plotly",
                    data=chart_dict,
                    title=title,
                    config={
                        "data_shape": {
                            "rows": row_count,
                            "columns": len(chart_frame.columns),
                        },
                        "source_file": args.filename,
                        "chart_contract_version": contract.version,
                        "server_owned": True,
                    },
                ),
                simple_component=SimpleTextComponent(text=result),
            ),
            metadata={
                "filename": args.filename,
                "rows": row_count,
                "columns": list(chart_frame.columns),
                "chart": chart_dict,
                "chart_contract": contract.as_evidence(),
            },
        )

    @staticmethod
    def _render_chart(contract: ChartContract, frame: pd.DataFrame) -> dict:
        """Build direct Plotly traces without any display-time aggregation."""

        figure = go.Figure()
        groups = ((None, frame),)
        if contract.series_column:
            groups = tuple(frame.groupby(contract.series_column, sort=False, dropna=False))
        for metric in contract.y_columns:
            for series_value, group in groups:
                trace_name = metric if series_value is None else f"{series_value} · {metric}"
                common = {
                    "x": group[contract.x_column],
                    "y": group[metric],
                    "name": trace_name,
                }
                if contract.chart_type == "line":
                    figure.add_trace(go.Scatter(mode="lines+markers", **common))
                else:
                    figure.add_trace(go.Bar(**common))
        figure.update_layout(
            title=contract.title,
            xaxis_title=contract.x_column,
            yaxis_title="指标值",
            template="plotly_white",
            legend_title_text=contract.series_column or None,
        )
        return json.loads(pio.to_json(figure))

    @staticmethod
    def _reject(message: str) -> ToolResult:
        return ToolResult(
            success=False,
            result_for_llm=message,
            error=message,
            ui_component=UiComponent(
                rich_component=NotificationComponent(
                    type=ComponentType.NOTIFICATION,
                    level="warning",
                    message=message,
                ),
                simple_component=SimpleTextComponent(text=message),
            ),
            metadata={"error_type": "chart_policy"},
        )
