"""Generic SQL query execution tool with dependency injection."""

from typing import Any, Dict, List, Optional, Type, cast
import uuid
from vanna.core.tool import Tool, ToolContext, ToolResult
from vanna.components import (
    UiComponent,
    DataFrameComponent,
    NotificationComponent,
    ComponentType,
    SimpleTextComponent,
)
from vanna.capabilities.sql_runner import SqlRunner, RunSqlToolArgs
from vanna.capabilities.file_system import FileSystem
from vanna.integrations.local import LocalFileSystem


_ROW_CUT_NOTICE = (
    "\n(Results truncated to complete rows. FOR LARGE RESULTS YOU DO NOT NEED TO "
    "SUMMARIZE THESE RESULTS OR PROVIDE OBSERVATIONS. THE NEXT STEP SHOULD BE A "
    "VISUALIZE_DATA CALL)"
)


def result_preview_for_llm(csv_content: str, validated_summary: object) -> str:
    """Hand the model a complete summary, never a CSV slice that ends mid-row."""

    if isinstance(validated_summary, str) and validated_summary.strip():
        return validated_summary.strip()
    if len(csv_content) <= 1000:
        return csv_content
    cut = csv_content.rfind("\n", 0, 1000)
    if cut <= 0:
        cut = 1000
    return csv_content[:cut] + _ROW_CUT_NOTICE


class RunSqlTool(Tool[RunSqlToolArgs]):
    """Tool that executes SQL queries using an injected SqlRunner implementation."""

    def __init__(
        self,
        sql_runner: SqlRunner,
        file_system: Optional[FileSystem] = None,
        custom_tool_name: Optional[str] = None,
        custom_tool_description: Optional[str] = None,
    ):
        """Initialize the tool with a SqlRunner implementation.

        Args:
            sql_runner: SqlRunner implementation that handles actual query execution
            file_system: FileSystem implementation for saving results (defaults to LocalFileSystem)
            custom_tool_name: Optional custom name for the tool (overrides default "run_sql")
            custom_tool_description: Optional custom description for the tool (overrides default description)
        """
        self.sql_runner = sql_runner
        self.file_system = file_system or LocalFileSystem()
        self._custom_name = custom_tool_name
        self._custom_description = custom_tool_description

    @property
    def name(self) -> str:
        return self._custom_name if self._custom_name else "run_sql"

    @property
    def description(self) -> str:
        return (
            self._custom_description
            if self._custom_description
            else "Execute SQL queries against the configured database"
        )

    def get_args_schema(self) -> Type[RunSqlToolArgs]:
        return RunSqlToolArgs

    async def execute(self, context: ToolContext, args: RunSqlToolArgs) -> ToolResult:
        """Execute a SQL query using the injected SqlRunner."""
        try:
            # Use the injected SqlRunner to execute the query
            df = await self.sql_runner.run_sql(args, context)

            # Determine query type
            query_type = args.sql.strip().upper().split()[0]

            if query_type in {"SELECT", "WITH"}:
                # Handle SELECT queries with results
                if df.empty:
                    result = "Query executed successfully. No rows returned."
                    ui_component = UiComponent(
                        rich_component=DataFrameComponent(
                            rows=[],
                            columns=[],
                            title="Query Results",
                            description="No rows returned",
                            exportable=False,
                            preview_export_allowed=False,
                            full_export_allowed=False,
                        ),
                        simple_component=SimpleTextComponent(text=result),
                    )
                    metadata = {
                        "row_count": 0,
                        "columns": [],
                        "query_type": query_type,
                        "results": [],
                    }
                else:
                    # Convert DataFrame to records
                    results_data = df.to_dict("records")
                    columns = df.columns.tolist()
                    row_count = len(df)

                    # Write DataFrame to CSV file for downstream tools
                    file_id = str(uuid.uuid4())[:8]
                    filename = f"query_results_{file_id}.csv"
                    csv_content = df.to_csv(index=False)
                    await self.file_system.write_file(
                        filename, csv_content, context, overwrite=True
                    )

                    results_preview = result_preview_for_llm(
                        csv_content, context.metadata.get("validated_result_summary")
                    )

                    display_budget = int(context.metadata.get("display_result_row_budget", 200))
                    if display_budget <= 0:
                        display_budget = 200
                    display_truncated = row_count > display_budget
                    result = (
                        f"{results_preview}\n\nResults saved to file: {filename}\n"
                        f"\n完整查询返回 {row_count} 行；"
                        + (
                            f"界面仅预览前 {display_budget} 行。\n"
                            if display_truncated
                            else "界面已展示全部返回行。\n"
                        )
                        + f"\n**IMPORTANT: FOR VISUALIZE_DATA USE FILENAME: {filename}**"
                    )

                    # Create DataFrame component for UI
                    display_results_data = results_data[:display_budget]
                    dataframe_component = DataFrameComponent.from_records(
                        records=cast(List[Dict[str, Any]], display_results_data),
                        title="Query Results",
                        max_rows_displayed=display_budget,
                        description=(
                            f"SQL query returned {row_count} rows with {len(columns)} columns"
                            + (
                                f"; previewing first {display_budget} rows"
                                if display_truncated
                                else ""
                            )
                        ),
                        total_row_count=row_count,
                        preview_row_count=len(display_results_data),
                        display_truncated=display_truncated,
                        # The generic tool has no authenticated full-export
                        # route; never imply that its preview is complete.
                        exportable=False,
                        preview_export_allowed=False,
                        full_export_allowed=False,
                    )

                    ui_component = UiComponent(
                        rich_component=dataframe_component,
                        simple_component=SimpleTextComponent(text=result),
                    )

                    metadata = {
                        "row_count": row_count,
                        "columns": columns,
                        "query_type": query_type,
                        "results": results_data,
                        "display_results": display_results_data,
                        "display_row_budget": display_budget,
                        "display_truncated": display_truncated,
                        "output_file": filename,
                    }
            else:
                # For non-SELECT queries (INSERT, UPDATE, DELETE, etc.)
                # The SqlRunner should return a DataFrame with affected row count
                rows_affected = len(df) if not df.empty else 0
                result = (
                    f"Query executed successfully. {rows_affected} row(s) affected."
                )

                metadata = {"rows_affected": rows_affected, "query_type": query_type}
                ui_component = UiComponent(
                    rich_component=NotificationComponent(
                        type=ComponentType.NOTIFICATION, level="success", message=result
                    ),
                    simple_component=SimpleTextComponent(text=result),
                )

            return ToolResult(
                success=True,
                result_for_llm=result,
                ui_component=ui_component,
                metadata=metadata,
            )

        except Exception as e:
            error_message = f"Error executing query: {str(e)}"
            return ToolResult(
                success=False,
                result_for_llm=error_message,
                ui_component=UiComponent(
                    rich_component=NotificationComponent(
                        type=ComponentType.NOTIFICATION,
                        level="error",
                        message=error_message,
                    ),
                    simple_component=SimpleTextComponent(text=error_message),
                ),
                error=str(e),
                metadata={"error_type": "sql_error"},
            )
