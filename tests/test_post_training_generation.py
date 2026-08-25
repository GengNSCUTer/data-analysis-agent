from __future__ import annotations

import pytest

pytest.importorskip("torch", reason="generation utilities run in the isolated QLoRA environment")

from scripts.generate_post_training_text_to_sql import (
    GenerationInputError,
    require_dev_cases_without_gold,
    table_mapping,
)


class QueryForbiddenCase(dict[str, object]):
    def get(self, key: str, default: object = None) -> object:
        if key == "query":
            raise AssertionError("Spider dev gold SQL must not be read for generation")
        return super().get(key, default)


def test_generation_case_normalization_never_reads_dev_gold_sql() -> None:
    cases = [
        QueryForbiddenCase(
            db_id="shop",
            question="How many items are available?",
            query="SELECT private_gold_sql",
        )
    ]

    assert require_dev_cases_without_gold(cases) == [("shop", "How many items are available?")]


def test_generation_table_mapping_rejects_duplicate_database_metadata() -> None:
    with pytest.raises(GenerationInputError, match="duplicate table metadata"):
        table_mapping([{"db_id": "shop"}, {"db_id": "shop"}])
