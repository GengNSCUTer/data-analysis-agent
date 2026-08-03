from scripts.run_project_evaluation import ROOT, run


def test_first_round_suite_has_sixty_unique_cases_and_policy_expectations() -> None:
    result = run(ROOT / "evals/cases/v1.yaml", verify_database=False)
    assert result["total_cases"] == 60
    assert result["unique_ids"] is True
    assert result["categories"]["safety"] >= 20
    assert result["safety"]["passed"] == result["safety"]["total"]
