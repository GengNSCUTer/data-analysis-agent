from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HOLDOUT_MANIFEST = REPOSITORY_ROOT / "evals/manifests/post_training_holdout_v1.yaml"
GOLDEN_SUITE = REPOSITORY_ROOT / "evals/cases/text_to_sql_v2.yaml"


def test_post_training_holdout_matches_every_v2_golden_case() -> None:
    manifest = yaml.safe_load(HOLDOUT_MANIFEST.read_text(encoding="utf-8"))
    suite = yaml.safe_load(GOLDEN_SUITE.read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == "1"
    assert manifest["suite_version"] == suite["version"]
    assert manifest["forbidden_for_training"] is True

    holdout_cases = manifest["cases"]
    holdout_ids = [case["case_id"] for case in holdout_cases]
    suite_ids = [case["id"] for case in suite["cases"]]

    assert len(suite_ids) == 60
    assert len(holdout_ids) == len(set(holdout_ids)) == len(suite_ids)
    assert holdout_ids == suite_ids
    assert all(case["forbidden_for_training"] is True for case in holdout_cases)
    assert all(set(case) == {"case_id", "forbidden_for_training"} for case in holdout_cases)
