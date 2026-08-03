from __future__ import annotations

import pytest

from data_analysis_agent.trusted_workflow import TrustedOlistWorkflowHandler
from vanna.components import ButtonGroupComponent, RichTextComponent
from vanna.core.user import User


@pytest.mark.asyncio
async def test_starter_ui_is_chinese_and_uses_safe_demo_questions() -> None:
    handler = TrustedOlistWorkflowHandler()
    user = User(id="demo-analyst", username="demo-analyst", group_memberships=["analyst"])

    components = await handler.get_starter_ui(agent=None, user=user, conversation=None)

    assert components is not None
    intro = components[0].rich_component
    actions = components[1].rich_component
    assert isinstance(intro, RichTextComponent)
    assert "经营分析副驾" in intro.content
    assert "只读 SQL" in intro.content
    assert isinstance(actions, ButtonGroupComponent)
    assert [button["label"] for button in actions.data["buttons"]] == [
        "州前五",
        "品类前十",
        "指标概览",
    ]
    assert all(
        "app schema" not in button["action"].lower()
        for button in actions.data["buttons"]
    )


@pytest.mark.asyncio
async def test_help_is_handled_without_an_llm_call() -> None:
    handler = TrustedOlistWorkflowHandler()
    user = User(id="demo-analyst", username="demo-analyst", group_memberships=["analyst"])

    result = await handler.try_handle(
        agent=None, user=user, conversation=None, message="/help"
    )

    assert result.should_skip_llm is True
    assert result.components is not None
    help_component = result.components[0].rich_component
    assert isinstance(help_component, RichTextComponent)
    assert "受控的 PostgreSQL" in help_component.content
    assert "不能访问 app、system schema 或任意文件" in help_component.content
