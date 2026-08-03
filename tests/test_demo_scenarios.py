"""Coverage for versioned, repeatable embedded-demo scenarios."""

from __future__ import annotations

import os

import pytest
import yaml

from data_analysis_agent.trusted_workflow import TrustedOlistWorkflowHandler
from scripts.run_demo_scenario_evaluation import ROOT, run
from vanna.components import ButtonGroupComponent
from vanna.core.user import User


def test_demo_scenario_contract_inventory_is_complete() -> None:
    result = run(ROOT / "evals/cases/demo_scenarios.yaml", verify_database=False)

    assert result["scenario_count"] == 3
    assert result["unique_ids"] is True
    assert result["missing_fields"] == {}
    assert result["invalid_roles"] == {}
    assert result["invalid_charts"] == {}


@pytest.mark.asyncio
async def test_starter_actions_match_the_versioned_demo_scenarios() -> None:
    suite = yaml.safe_load((ROOT / "evals/cases/demo_scenarios.yaml").read_text(encoding="utf-8"))
    handler = TrustedOlistWorkflowHandler()
    user = User(id="demo-analyst", username="demo-analyst", group_memberships=["analyst"])
    components = await handler.get_starter_ui(agent=None, user=user, conversation=None)

    assert components is not None
    actions = components[1].rich_component
    assert isinstance(actions, ButtonGroupComponent)
    assert [button["label"] for button in actions.data["buttons"]] == [
        scenario["label"] for scenario in suite["scenarios"]
    ]
    assert [button["action"] for button in actions.data["buttons"]] == [
        scenario["question"] for scenario in suite["scenarios"]
    ]


@pytest.mark.skipif(os.getenv("RUN_PROJECT_DB") != "1", reason="set RUN_PROJECT_DB=1 for local golden verification")
def test_demo_scenarios_match_the_loaded_postgres_dataset() -> None:
    result = run(ROOT / "evals/cases/demo_scenarios.yaml", verify_database=True)
    assert result["database_golden"]["passed"] is True
