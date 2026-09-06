from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.post_training.evaluation.run_olist_medium_matching_evaluation import (
    MediumEvaluationError,
    load_test_contract,
)


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_final_test_runtime_contract_requires_matching_prompt_and_identity(tmp_path: Path) -> None:
    test_path = tmp_path / "final_evaluation_only" / "in_domain_test.jsonl"
    test_path.parent.mkdir()
    _write_jsonl(test_path, [{"seed_id": "test-001", "split": {"name": "in_domain_test"}}])
    prompt = "### Task\n### SQL"
    runtime_path = tmp_path / "runtime_candidates.jsonl"
    _write_jsonl(
        runtime_path,
        [{
            "seed_id": "test-001",
            "split": "in_domain_test",
            "prompt": prompt,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "query_plan": {"time_grain": None},
            "result_contract": {"required_result_columns": ["gmv"]},
            "route": {"state": "answerable"},
        }],
    )
    audit_path = tmp_path / "split_audit.json"
    audit_path.write_text(json.dumps({
        "checks": {"status": "pass", "in_domain_test_forbidden_for_training": True},
        "outputs": {"in_domain_test_jsonl": str(test_path)},
        "splits": {"in_domain_test": {"rows": 240, "sha256": hashlib.sha256(test_path.read_bytes()).hexdigest()}},
    }), encoding="utf-8")

    rows = load_test_contract(test_path, runtime_path, audit_path)

    assert [row["seed_id"] for row in rows] == ["test-001"]


def test_final_test_runtime_contract_rejects_prompt_hash_drift(tmp_path: Path) -> None:
    test_path = tmp_path / "in_domain_test.jsonl"
    _write_jsonl(test_path, [{"seed_id": "test-001", "split": {"name": "in_domain_test"}}])
    runtime_path = tmp_path / "runtime.jsonl"
    _write_jsonl(runtime_path, [{"seed_id": "test-001", "split": "in_domain_test", "prompt": "### SQL", "prompt_sha256": "bad", "query_plan": {}, "result_contract": {}, "route": {"state": "answerable"}}])
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps({"checks": {"status": "pass", "in_domain_test_forbidden_for_training": True}, "outputs": {"in_domain_test_jsonl": str(test_path)}, "splits": {"in_domain_test": {"rows": 240, "sha256": hashlib.sha256(test_path.read_bytes()).hexdigest()}}}), encoding="utf-8")

    with pytest.raises(MediumEvaluationError, match="prompt hash"):
        load_test_contract(test_path, runtime_path, audit_path)
