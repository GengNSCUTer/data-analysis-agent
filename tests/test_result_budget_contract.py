from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_result_budget_manifest_freezes_separate_modes_and_budgets() -> None:
    manifest = json.loads(
        (ROOT / "data" / "manifest" / "result-budget-v2.json").read_text(encoding="utf-8")
    )

    assert manifest["contract_id"] == "result-budget-v2"
    assert manifest["query_modes"] == ["aggregate", "detail_page", "export"]
    assert manifest["budgets"]["llm_context_rows"] == 8
    assert manifest["budgets"]["preview_rows"] == 200
    assert manifest["budgets"]["detail_page_rows_hard_cap"] == 500
    assert manifest["current_online_compatibility"]["global_execution_cap"] == 1000
    assert manifest["current_online_compatibility"]["global_cap_is_interim_only"] is True


def test_perf_shop_fixture_is_isolated_from_business_workspaces() -> None:
    sql = (ROOT / "infra" / "postgres" / "perf_shop.sql").read_text(encoding="utf-8")

    assert "CREATE SCHEMA perf" in sql
    assert "CREATE ROLE perf_shop_reader" in sql
    assert "analytics." not in sql
    assert "data_analysis_agent" not in sql
    assert "generate_series" in sql
