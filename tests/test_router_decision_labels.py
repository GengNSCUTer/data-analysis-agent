"""Freeze the author-labeled router decision scorecard.

The label file is the contract. The scorecard file is the measured result.
A router change that moves a count must update the scorecard in the same
change; it must not rewrite gold to match the new output.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.router_decision_score import score_router_decisions, validate_label_set
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever


LABEL_PATH = Path(__file__).resolve().parents[1] / "evals" / "cases" / "router_decision_labels_v1.yaml"
SCORECARD_PATH = (
    Path(__file__).resolve().parents[1] / "evals" / "results" / "router_decision_scorecard_v1.yaml"
)


def test_router_decision_scorecard_matches_the_frozen_result() -> None:
    document = yaml.safe_load(LABEL_PATH.read_text(encoding="utf-8"))
    validate_label_set(document)
    assert document["catalog_version"] == OLIST_RUNTIME_WORKSPACE.catalog_version
    assert document["metric_version"] == OLIST_RUNTIME_WORKSPACE.metric_version
    assert len(document["cases"]) == 48

    router = QuestionRouter(CatalogRetriever(CatalogLoader(OLIST_RUNTIME_WORKSPACE).load()))
    measured = score_router_decisions(router, document["cases"])
    scorecard = yaml.safe_load(SCORECARD_PATH.read_text(encoding="utf-8"))

    assert scorecard["label_set_id"] == document["label_set_id"]
    assert scorecard["catalog_version"] == document["catalog_version"]
    assert scorecard["metric_version"] == document["metric_version"]
    assert scorecard["scope_note"].startswith("Author-labeled router decisions")
    assert "semantic accuracy" not in scorecard["scope_note"].lower()
    counts = scorecard["counts"]
    assert counts == {
        "cases": measured["cases"],
        "evidence_match": measured["evidence_match"],
        "false_open": measured["false_open"],
        "false_block": measured["false_block"],
        "metric_exact": measured["metric_exact"],
        "metric_scored": measured["metric_scored"],
    }
    assert scorecard["mismatches"] == measured["mismatches"]
