from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from data_analysis_agent.user_memory import UserMemoryStore


def test_preferences_are_not_read_by_the_sql_prompt_modules() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "data_analysis_agent"
    for path in root.rglob("*.py"):
        if path.name in {"user_memory.py", "runtime_diagnostics.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert "user_preferences" not in text
        assert "saved_analyses" not in text
        assert "UserMemoryStore" not in text


def test_preference_panel_discloses_display_is_not_wired() -> None:
    html = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "embeddable_copilot_shell.html"
    ).read_text(encoding="utf-8")
    assert "还不会改变本轮回答的语言、时区、图表或数字格式" in html
    assert 'id="preference-form"' in html


def test_unknown_preference_is_rejected_before_any_write() -> None:
    store = UserMemoryStore()
    with pytest.raises(ValueError, match="unsupported preference"):
        store.put_preference("analyst", "olist-demo", "language", "fr")
    with pytest.raises(ValueError, match="unsupported preference"):
        store.put_preference("analyst", "olist-demo", "sql_fact", "gmv")


@pytest.mark.skipif(os.getenv("RUN_PROJECT_DB") != "1", reason="set RUN_PROJECT_DB=1")
def test_preferences_and_saved_analyses_stay_on_their_subject() -> None:
    store = UserMemoryStore()
    subject = f"memory-{uuid.uuid4().hex[:8]}"
    other = f"memory-{uuid.uuid4().hex[:8]}"
    workspace = "olist-demo"
    store.put_preference(subject, workspace, "language", "zh")
    store.put_preference(subject, workspace, "default_chart", "bar")
    saved_id = store.save_analysis(
        subject_id=subject,
        workspace_id=workspace,
        dataset_version="olist-v1",
        metric_version="0.3-proposal",
        artifact_id="rta_test",
        summary="SP GMV",
    )
    assert {item["memory_kind"] for item in store.list_preferences(subject, workspace)} == {
        "language",
        "default_chart",
    }
    assert store.list_preferences(other, workspace) == []
    assert [item["saved_id"] for item in store.list_analyses(subject, workspace)] == [saved_id]
    assert store.list_analyses(other, workspace) == []
    assert store.delete_analysis(other, workspace, saved_id) is False
    assert store.delete_preference(subject, workspace, "language") is True
    assert store.delete_analysis(subject, workspace, saved_id) is True
