from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "evals/manifests/post_training_candidates_v1.yaml"
HOLDOUT = ROOT / "evals/manifests/post_training_holdout_v1.yaml"


def test_candidate_manifest_records_external_train_only_candidates():
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))

    assert data["status"] == "prepared_external_only"
    assert data["candidate_ids"] == []
    assert data["generator"]["selected_count"] == 128
    assert data["external_artifact"]["candidates_jsonl"] == "candidates.jsonl"
    assert len(data["external_artifact"]["candidates_sha256"]) == 64
    assert data["execution_check"] == {
        "readonly_explain": "pass",
        "pass_count": 128,
        "error_count": 0,
    }
    assert data["training_boundary"] == {
        "raw_data_in_git": False,
        "gold_dev_or_test_used": False,
        "v2_holdout_used": False,
        "production_postgres_modified": False,
    }
    assert data["source_policy"]["forbidden"]
    assert set(data["required_fields"]) >= {
        "sample_id",
        "question_redacted",
        "candidate_sql",
        "execution_outcome",
        "split",
    }


def test_candidate_manifest_forbids_sensitive_fields():
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    forbidden = set(data["forbidden_fields"])

    assert {
        "raw_question",
        "raw_user_text",
        "raw_result_rows",
        "api_key",
        "cookie",
        "access_token",
        "password",
    } <= forbidden


def test_candidate_manifest_does_not_reference_holdout_cases():
    candidates = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["candidate_ids"]
    holdout = yaml.safe_load(HOLDOUT.read_text(encoding="utf-8"))
    holdout_ids = {case["case_id"] for case in holdout["cases"]}

    assert not holdout_ids.intersection(candidates)
