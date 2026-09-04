from __future__ import annotations

import pytest

from scripts.post_training.training.run_post_training_sft_smoke import split_prompt_and_target


def test_olist_runtime_prompt_layout_keeps_production_prompt_exact() -> None:
    prompt = "### Task\nGenerate SQL.\n### Question\n统计 GMV。\n### SQL"
    row = {
        "sample_id": "olist-001",
        "rendered_prompt": prompt,
        "candidate_sql": "SELECT 1;",
        "training_text": prompt + "\nSELECT 1;",
    }

    actual_prompt, target = split_prompt_and_target(row)

    assert actual_prompt == prompt + "\n"
    assert target == "SELECT 1;"


def test_olist_runtime_prompt_layout_rejects_training_text_drift() -> None:
    row = {
        "sample_id": "olist-001",
        "rendered_prompt": "### SQL",
        "candidate_sql": "SELECT 1;",
        "training_text": "### SQL\nSELECT 2;",
    }
    with pytest.raises(ValueError, match="runtime prompt and target SQL mismatch"):
        split_prompt_and_target(row)
