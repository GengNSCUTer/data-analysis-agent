from __future__ import annotations

from scripts.build_spider_schema_stratified_smoke import (
    model_facing_case,
    select_schema_stratified_indices,
)


class QueryForbiddenCase(dict[str, object]):
    def get(self, key: str, default: object = None) -> object:
        if key == "query":
            raise AssertionError("selection must not inspect Spider dev gold SQL")
        return super().get(key, default)


def test_schema_stratified_selection_is_deterministic_and_never_reads_gold() -> None:
    cases = [
        QueryForbiddenCase(db_id="alpha", question="q0", query="SELECT secret"),
        QueryForbiddenCase(db_id="alpha", question="q1", query="SELECT secret"),
        QueryForbiddenCase(db_id="alpha", question="q2", query="SELECT secret"),
        QueryForbiddenCase(db_id="beta", question="q3", query="SELECT secret"),
        QueryForbiddenCase(db_id="beta", question="q4", query="SELECT secret"),
    ]

    first = select_schema_stratified_indices(
        cases, per_schema=1, seed=7, exclude_prefix=1, exclude_prefix_schemas=False
    )
    second = select_schema_stratified_indices(
        cases, per_schema=1, seed=7, exclude_prefix=1, exclude_prefix_schemas=False
    )

    assert first == second
    assert len(first) == 2
    assert all(index >= 1 for index in first)


def test_schema_stratified_selection_can_exclude_all_prefix_schemas() -> None:
    cases = [
        {"db_id": "alpha", "question": "q0"},
        {"db_id": "alpha", "question": "q1"},
        {"db_id": "beta", "question": "q2"},
        {"db_id": "beta", "question": "q3"},
    ]

    selected = select_schema_stratified_indices(
        cases, per_schema=10, seed=7, exclude_prefix=1, exclude_prefix_schemas=True
    )

    assert selected == [2, 3]


def test_model_facing_case_excludes_gold_sql() -> None:
    model_case = model_facing_case(
        {"db_id": "shop", "question": "How many items?", "query": "SELECT private_gold_sql"}
    )

    assert model_case == {"db_id": "shop", "question": "How many items?"}
