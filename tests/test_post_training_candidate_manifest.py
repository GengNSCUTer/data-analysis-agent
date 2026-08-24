from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "evals/manifests/post_training_candidates_v1.yaml"
HOLDOUT = ROOT / "evals/manifests/post_training_holdout_v1.yaml"


def test_candidate_manifest_is_template_only_and_empty():
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))

    assert data["status"] == "template_only"
    assert data["candidate_ids"] == []
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

