"""Freeze the wide router decision set and its written report."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.router_decision_score import (
    render_router_decision_report,
    score_router_decisions,
    validate_label_set,
)
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever


ROOT = Path(__file__).resolve().parents[1]
LABEL_PATH = ROOT / "evals" / "cases" / "router_decision_labels_v2.yaml"
SCORECARD_PATH = ROOT / "evals" / "results" / "router_decision_scorecard_v2.yaml"
REPORT_PATH = ROOT / "evals" / "results" / "router_decision_report_v2.md"
_REQUIRED_STRATA = ("sql", "clarify_time", "definition", "advice")


def _builder():
    spec = importlib.util.spec_from_file_location(
        "build_router_decision_labels_v2",
        ROOT / "evals" / "build_router_decision_labels_v2.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_router_decision_v2_scorecard_and_report_match() -> None:
    builder = _builder()
    document = yaml.safe_load(LABEL_PATH.read_text(encoding="utf-8"))
    assert document == builder.build_label_document()
    validate_label_set(document)
    assert document["catalog_version"] == OLIST_RUNTIME_WORKSPACE.catalog_version
    assert document["metric_version"] == OLIST_RUNTIME_WORKSPACE.metric_version
    assert len(document["cases"]) >= 300

    seen = set()
    covered = {stratum: set() for stratum in _REQUIRED_STRATA}
    for case in document["cases"]:
        key = (case["question"], yaml.safe_dump(case.get("conversation_state"), sort_keys=True))
        assert key not in seen
        seen.add(key)
        if case["stratum"] in covered and case["gold"]["metric_ids"]:
            covered[case["stratum"]].update(case["gold"]["metric_ids"])
    assert all(len(metric_ids) == 19 for metric_ids in covered.values())

    router = QuestionRouter(CatalogRetriever(CatalogLoader(OLIST_RUNTIME_WORKSPACE).load()))
    measured = score_router_decisions(router, document["cases"])
    scorecard = yaml.safe_load(SCORECARD_PATH.read_text(encoding="utf-8"))
    assert scorecard["label_set_id"] == document["label_set_id"]
    assert scorecard["catalog_version"] == document["catalog_version"]
    assert scorecard["metric_version"] == document["metric_version"]
    assert scorecard["counts"] == {
        "cases": measured["cases"],
        "evidence_match": measured["evidence_match"],
        "false_open": measured["false_open"],
        "false_block": measured["false_block"],
        "metric_exact": measured["metric_exact"],
        "metric_scored": measured["metric_scored"],
    }
    assert scorecard["by_stratum"] == measured["by_stratum"]
    assert scorecard["mismatches"] == measured["mismatches"]

    report = render_router_decision_report(
        title="路由决策测试报告 v2",
        document=document,
        measured=measured,
        stratum_notes=builder.STRATUM_NOTES,
    )
    assert REPORT_PATH.read_text(encoding="utf-8") == report
    counts = scorecard["counts"]
    assert f"证据类型一致：{counts['evidence_match']}/{counts['cases']}" in report
    assert f"误开 SQL：{counts['false_open']}" in report
    assert f"该查却拦住：{counts['false_block']}" in report
    assert f"指标对上：{counts['metric_exact']}/{counts['metric_scored']}" in report
    assert "这不是 SQL 可执行率，也不是在线语义准确率。" in report
