"""Freeze the small runtime router probe. It does not replace SqlPolicy."""

from __future__ import annotations

from pathlib import Path

import yaml

from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE
from data_analysis_agent.question_router import QuestionRoute, QuestionRouter
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
from vanna.core.user import User


PROBE_PATH = Path(__file__).resolve().parents[1] / "evals" / "cases" / "runtime_router_probe_v1.yaml"


def _matches(route: QuestionRoute, expected: dict) -> None:
    assert route.state == expected["state"]
    assert route.reason == expected["reason"]
    assert route.requires_database is expected["requires_database"]
    if "intent" in expected:
        assert route.intent == expected["intent"]
    if "metric_ids" in expected:
        assert route.metric_ids == tuple(expected["metric_ids"])


def test_runtime_router_probe_matches_the_online_catalog() -> None:
    document = yaml.safe_load(PROBE_PATH.read_text(encoding="utf-8"))
    assert document["catalog_version"] == OLIST_RUNTIME_WORKSPACE.catalog_version
    assert document["metric_version"] == OLIST_RUNTIME_WORKSPACE.metric_version
    router = QuestionRouter(CatalogRetriever(CatalogLoader(OLIST_RUNTIME_WORKSPACE).load()))
    user = User(id="runtime-router-probe", group_memberships=["analyst"])
    seen = set()
    gaps = []
    for case in document["cases"]:
        assert case["id"] not in seen
        seen.add(case["id"])
        route = router.classify(case["question"], user=user)
        _matches(route, case["classify"])
        if case.get("admit") is None:
            assert route.requires_database is False
        else:
            assert route.requires_database is True
            _matches(router.admit_sql_request(case["question"], user=user), case["admit"])
        if case["status"] == "known_gap":
            assert case["gap"].strip()
            gaps.append(case["id"])
        else:
            assert case["status"] == "covered"
    assert gaps == ["colloquial-payment-amount"]
