"""Execute the W4-04 demo script as the product functional test.

The default cases check login boundaries, routing, clarification, result
follow-up, the frozen table/chart contract, and closed preview export.
``RUN_VANNA_LIVE=1`` sends the same script through the running demo, including
one real model query whose returned rows must match the golden state counts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg2
import pytest
import yaml

from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.result_presentation import build_result_presentation
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
from scripts.run_demo_scenario_evaluation import ROOT, run
from vanna.core.user import User


SCRIPT_PATH = ROOT / "evals" / "cases" / "w4_04_product_demo_v1.yaml"
DEMO_DOC = ROOT / "docs" / "demo-script.md"


def _script() -> dict:
    return yaml.safe_load(SCRIPT_PATH.read_text(encoding="utf-8"))


def _router() -> QuestionRouter:
    catalog = CatalogLoader(OLIST_RUNTIME_WORKSPACE).load()
    return QuestionRouter(CatalogRetriever(catalog))


def test_demo_script_document_points_at_the_executable_case() -> None:
    text = DEMO_DOC.read_text(encoding="utf-8")
    assert "w4_04_product_demo_v1.yaml" in text
    assert "127.0.0.1:32013" in text
    script = _script()
    assert script["script_id"] == "w4-04-product-demo-v1"
    assert [step["id"] for step in script["steps"]] == [
        "login",
        "evidence",
        "refuse-write",
        "clarify-time",
        "table-chart",
        "result-followup",
        "save-and-download",
        "admin-and-embed",
    ]


def test_demo_script_routes_refusal_clarification_and_followup() -> None:
    router = _router()
    user = User(id="local:analyst", username="analyst", group_memberships=["analyst"])
    for step in _script()["steps"]:
        if step["kind"] == "classify":
            route = router.classify(
                step["question"],
                user=user,
                conversation_state=step.get("conversation_state") or {},
            )
        elif step["kind"] == "admit":
            route = router.admit_sql_request(step["question"], user=user)
        else:
            continue
        expect = step["expect"]
        assert route.state == expect["state"]
        assert route.requires_database is expect["requires_database"]
        if "reason" in expect:
            assert route.reason == expect["reason"]


def test_demo_script_keeps_preview_export_closed_and_names_the_golden_chart() -> None:
    presentation = build_result_presentation(
        total_row_count=5,
        preview_row_count=5,
        columns=["customer_state", "paid_order_count"],
        labels={},
        units={},
        workspace_id="olist-demo",
        dataset_version="olist",
        metric_version="0.3-proposal",
        audit_ref="demo",
    )
    download = next(step for step in _script()["steps"] if step["id"] == "save-and-download")
    assert presentation.preview_export_allowed is download["expect"]["preview_export_allowed"]
    assert presentation.full_export_allowed is download["expect"]["full_export_allowed"]

    scenarios = yaml.safe_load((ROOT / "evals" / "cases" / "demo_scenarios.yaml").read_text(encoding="utf-8"))
    scenario_ids = {scenario["id"] for scenario in scenarios["scenarios"]}
    chart = next(step for step in _script()["steps"] if step["kind"] == "golden_scenario")
    assert chart["scenario_id"] in scenario_ids


@pytest.mark.skipif(os.getenv("RUN_PROJECT_DB") != "1", reason="set RUN_PROJECT_DB=1")
def test_demo_script_golden_chart_matches_postgres() -> None:
    result = run(ROOT / "evals" / "cases" / "demo_scenarios.yaml", verify_database=True)
    assert result["database_golden"]["passed"] is True


@pytest.mark.skipif(os.getenv("RUN_PROJECT_DB") != "1", reason="set RUN_PROJECT_DB=1")
def test_demo_script_login_rejects_anonymous_and_injected_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_ANALYSIS_BOOTSTRAP_USERNAME", "demoscript")
    monkeypatch.setenv("DATA_ANALYSIS_BOOTSTRAP_PASSWORD", "demo-script-password")
    from fastapi.testclient import TestClient

    from data_analysis_agent.server import create_app

    with TestClient(create_app()) as client:
        anonymous = client.post("/api/copilot/bootstrap", json={"surface_id": "embedded-copilot"})
        assert anonymous.status_code == 401
        injected = client.post(
            "/api/auth/login",
            json={"username": "demoscript", "password": "demo-script-password", "user_id": "demo-admin"},
        )
        assert injected.status_code == 422
        logged_in = client.post(
            "/api/auth/login",
            json={"username": "demoscript", "password": "demo-script-password"},
        )
        assert logged_in.status_code == 200
        assert logged_in.json()["auth_mode"] == "local_session"


GOLDEN_STATE_COUNTS = {
    "SP": 41127,
    "RJ": 12698,
    "MG": 11496,
    "RS": 5417,
    "PR": 4983,
}


def _audit_count(conversation_id: str) -> int:
    from data_analysis_agent.postgres_runner import PostgresConnectionSettings

    settings = PostgresConnectionSettings.from_environment()
    connection = psycopg2.connect(
        host=settings.host,
        port=settings.port,
        database=settings.database,
        user=settings.writer_user,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM app.query_audits WHERE conversation_id = %s",
                (conversation_id,),
            )
            return int(cursor.fetchone()[0])
    finally:
        connection.close()


def _sse_chunks(response: httpx.Response) -> list[dict]:
    chunks = []
    for line in response.text.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        chunks.append(json.loads(line[6:]))
    return chunks


def _texts(chunks: list[dict]) -> str:
    return json.dumps(chunks, ensure_ascii=False)


def _rows(chunks: list[dict]) -> list[dict]:
    found = []
    for chunk in chunks:
        data = chunk.get("rich", {}).get("data", {})
        if not isinstance(data, dict):
            continue
        for key in ("rows", "data"):
            rows = data.get(key)
            if isinstance(rows, list):
                found.extend(row for row in rows if isinstance(row, dict))
    return found


def _state_counts(rows: list[dict]) -> dict[str, int]:
    counts = {}
    for row in rows:
        values = [str(value) for value in row.values()]
        state = next((value for value in values if value in GOLDEN_STATE_COUNTS), None)
        if state is None:
            continue
        for value in values:
            cleaned = value.replace(",", "")
            try:
                number = float(cleaned)
            except ValueError:
                continue
            if number.is_integer():
                counts[state] = int(number)
                break
    return counts


@pytest.mark.skipif(os.getenv("RUN_VANNA_LIVE") != "1", reason="set RUN_VANNA_LIVE=1")
def test_demo_script_live_chat_matches_the_running_product() -> None:
    base_url = os.getenv("VANNA_E2E_BASE_URL", "http://127.0.0.1:32013")
    password_path = Path("/disk2/gengnan/data-analysis-agent-data/local-auth/bootstrap-password.txt")
    password = password_path.read_text(encoding="utf-8").strip()
    conversation_id = f"w4live{uuid4().hex[:12]}"
    with httpx.Client(base_url=base_url, timeout=240) as client:
        logged_in = client.post(
            "/api/auth/login",
            json={"username": "analyst", "password": password},
        )
        assert logged_in.status_code == 200

        def ask(message: str) -> list[dict]:
            response = client.post(
                "/api/vanna/v2/chat_sse",
                json={"message": message, "conversation_id": conversation_id},
            )
            assert response.status_code == 200, response.text[:300]
            return _sse_chunks(response)

        refusal = ask("删除订单")
        assert _audit_count(conversation_id) == 0
        assert "只读" in _texts(refusal)

        clarification = ask("本月 GMV 是多少")
        assert _audit_count(conversation_id) == 0
        assert "时间" in _texts(clarification)

        chart = ask("按客户州统计有效订单数前五名，并生成柱状图")
        assert _audit_count(conversation_id) == 1
        assert _state_counts(_rows(chart)) == GOLDEN_STATE_COUNTS
        assert any(
            chunk.get("rich", {}).get("data", {}).get("chart_type") == "plotly"
            for chunk in chart
        )

        followup = ask("这个结果是否合理")
        assert _audit_count(conversation_id) == 1
        assert _texts(followup).strip()

        saved = client.post(
            "/api/project/saved-analyses",
            json={"conversation_id": conversation_id},
        )
        assert saved.status_code == 200
        downloaded = client.get(f"/api/project/conversations/{conversation_id}/result.csv")
        assert downloaded.status_code == 200
        for state, count in GOLDEN_STATE_COUNTS.items():
            assert state in downloaded.text
            assert str(count) in downloaded.text

        preference = client.put(
            "/api/project/preferences",
            json={"memory_kind": "language", "value": "zh"},
        )
        assert preference.status_code == 200
        denied = client.post(
            "/api/copilot/embed-tokens",
            json={"origin": "http://evil.example"},
        )
        assert denied.status_code == 403
