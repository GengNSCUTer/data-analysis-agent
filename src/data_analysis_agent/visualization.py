"""Constrained chart tool for SQL result artifacts owned by the current user."""

from __future__ import annotations

import re

from vanna.capabilities.file_system import FileSystem
from vanna.components import ComponentType, NotificationComponent, SimpleTextComponent, UiComponent
from vanna.core.tool import ToolContext, ToolResult
from vanna.integrations.plotly import PlotlyChartGenerator
from vanna.tools.visualize_data import VisualizeDataArgs, VisualizeDataTool


RESULT_FILENAME = re.compile(r"^query_results_[0-9a-f]{8}\.csv$")
MAX_CHART_ROWS = 200
MAX_CHART_COLUMNS = 3


class TrustedVisualizeDataTool(VisualizeDataTool):
    """Visualize only compact result files emitted by the preceding SQL tool."""

    def __init__(self, file_system: FileSystem):
        super().__init__(file_system=file_system, plotly_generator=PlotlyChartGenerator())

    @property
    def description(self) -> str:
        return "Create a chart only from the preceding run_sql query_results_<id>.csv result."

    async def execute(self, context: ToolContext, args: VisualizeDataArgs) -> ToolResult:
        if not RESULT_FILENAME.fullmatch(args.filename):
            return self._reject("只能可视化当前查询生成的结果文件。")
        csv_content = await self.file_system.read_file(args.filename, context)
        lines = csv_content.splitlines()
        if len(lines) - 1 > MAX_CHART_ROWS or (lines and len(lines[0].split(",")) > MAX_CHART_COLUMNS):
            return self._reject("结果规模不适合图表，请先聚合并限制到 200 行、3 列以内。")
        return await super().execute(context, args)

    def _reject(self, message: str) -> ToolResult:
        return ToolResult(success=False, result_for_llm=message, error=message,
            ui_component=UiComponent(rich_component=NotificationComponent(type=ComponentType.NOTIFICATION, level="warning", message=message), simple_component=SimpleTextComponent(text=message)),
            metadata={"error_type": "chart_policy"})
