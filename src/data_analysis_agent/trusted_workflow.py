"""Workspace-configured help and starter UI; no business intent routing."""
from __future__ import annotations

from vanna.components import ButtonGroupComponent, RichTextComponent, UiComponent
from vanna.core.workflow import DefaultWorkflowHandler, WorkflowResult
from .runtime_workspace import RuntimeWorkspace


class AnalyticsWorkflowHandler(DefaultWorkflowHandler):
    def __init__(self, runtime: RuntimeWorkspace):
        super().__init__()
        self.runtime = runtime

    async def try_handle(self, agent, user, conversation, message: str) -> WorkflowResult:
        if message.strip().lower() in {"/help", "help", "/h"}:
            examples = "\n".join(f'- “{item["question"]}”' for item in self.runtime.starter_questions)
            content = (
                f"## 经营分析副驾｜{self.runtime.display_name}\n\n{self.runtime.description}\n\n"
                "用中文提问，在受控的 PostgreSQL Schema 内执行只读 SQL，返回结论、表格、图表及版本证据。\n\n"
                f"{examples}\n\n"
                "不能访问 app、system schema 或任意文件；不能读取受限原始明细或写库。"
                "指标、时间和允许维度由当前 Catalog 定义；未支持的业务口径需要补充定义。"
            )
            return WorkflowResult(should_skip_llm=True, components=[UiComponent(
                rich_component=RichTextComponent(content=content, markdown=True), simple_component=None,
            )])
        return await super().try_handle(agent, user, conversation, message)

    async def get_starter_ui(self, agent, user, conversation) -> list[UiComponent]:
        return [
            UiComponent(rich_component=RichTextComponent(
                content=f"## 经营分析副驾｜{self.runtime.display_name}\n\n{self.runtime.description} 查询只使用受控只读 SQL。选择示例或直接提问。",
                markdown=True,
            ), simple_component=None),
            UiComponent(rich_component=ButtonGroupComponent(buttons=[
                {"label": item["label"], "action": item["question"], "variant": "primary" if i == 0 else "secondary", "size": "medium"}
                for i, item in enumerate(self.runtime.starter_questions)
            ], orientation="horizontal", spacing="small", alignment="stretch", full_width=True), simple_component=None),
        ]
