"""Workflow customization for the trusted Olist embedded demo."""

from __future__ import annotations

from typing import Optional

from vanna.components import ButtonGroupComponent, RichTextComponent, UiComponent
from vanna.core.workflow import DefaultWorkflowHandler, WorkflowResult


class TrustedOlistWorkflowHandler(DefaultWorkflowHandler):
    """Chinese, demo-specific starter UI for the embedded analytics copilot."""

    async def try_handle(self, agent, user, conversation, message: str) -> WorkflowResult:
        normalized = message.strip().lower()
        if normalized in {"/help", "help", "/h"}:
            content = (
                "## 经营分析副驾\n\n"
                "你可以直接用中文提出经营分析问题，我会在受控的 PostgreSQL analytics "
                "Schema 内生成并执行只读 SQL，再返回结论、结果表、必要时的图表，以及可追溯的 SQL 与版本证据。\n\n"
                "**适合直接问的问题**\n"
                '- “按客户州统计有效订单数前五名，并生成柱状图”\n'
                '- “统计有效订单数最多的前十个商品品类”\n'
                '- “对比各州的 GMV 和好评率，说明口径”\n'
                '- “概览 GMV、有效订单数、平均履约天数和好评率”\n\n'
                "**边界**\n"
                "- 只能查询公开 Olist 电商数据集，不会写库\n"
                "- 不能访问 app、system schema 或任意文件\n"
                "- 没有明确时间范围时，我会先说明数据覆盖范围或补充追问\n\n"
                "当前演示身份为受控只读分析员，宿主页中的审计卡片会展示最近查询和版本信息。"
            )
            return WorkflowResult(
                should_skip_llm=True,
                components=[
                    UiComponent(
                        rich_component=RichTextComponent(content=content, markdown=True),
                        simple_component=None,
                    )
                ],
            )

        return await super().try_handle(agent, user, conversation, message)

    async def get_starter_ui(self, agent, user, conversation) -> Optional[list[UiComponent]]:
        role = "管理员" if "admin" in user.group_memberships else "分析员"
        intro = (
            "## 经营分析副驾\n\n"
            f"当前以**{role}**演示身份访问 Olist 公开电商数据集。查询仅在受控只读 SQL 范围内执行，"
            "结果附带版本和 SQL 证据。选择示例或直接输入问题。"
        )
        quick_actions = ButtonGroupComponent(
            buttons=[
                {
                    "label": "州前五",
                    "action": "按客户州统计有效订单数前五名，并生成柱状图",
                    "variant": "primary",
                    "size": "medium",
                },
                {
                    "label": "品类前十",
                    "action": "统计有效订单数最多的前十个商品品类",
                    "variant": "secondary",
                    "size": "medium",
                },
                {
                    "label": "指标概览",
                    "action": "概览 GMV、有效订单数、平均履约天数和好评率，并说明统计口径",
                    "variant": "secondary",
                    "size": "medium",
                },
            ],
            orientation="horizontal",
            spacing="small",
            alignment="stretch",
            full_width=True,
        )
        return [
            UiComponent(
                rich_component=RichTextComponent(content=intro, markdown=True),
                simple_component=None,
            ),
            UiComponent(rich_component=quick_actions, simple_component=None),
        ]
