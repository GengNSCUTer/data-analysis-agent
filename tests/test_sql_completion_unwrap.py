from __future__ import annotations

import pytest

from data_analysis_agent.candidate_sql_generator import (
    CandidateSqlGenerationError,
    unwrap_sql_completion,
)


@pytest.mark.parametrize(
    ("completion", "expected"),
    [
        (
            "```sql\nSQLQuery: SELECT name FROM singer;\n```\n\n### Answer\n| name |\n| --- |\n| John |",
            "SELECT name FROM singer;",
        ),
        ("### SQL\nSQL: SELECT name FROM singer;", "SELECT name FROM singer;"),
        ("## SQL\n```postgresql\nSELECT name FROM singer;\n```", "SELECT name FROM singer;"),
        ("SELECT name FROM singer;\n\n### Explanation\nAn explanation", "SELECT name FROM singer;"),
    ],
)
def test_unwrap_sql_completion_is_the_single_presentation_contract(
    completion: str, expected: str
) -> None:
    assert unwrap_sql_completion(completion) == expected


def test_unwrap_sql_completion_preserves_non_presentation_content_for_policy() -> None:
    completion = "Here is SQL: SELECT 'unterminated FROM singer"

    assert unwrap_sql_completion(completion) == completion


def test_unwrap_sql_completion_rejects_a_completion_with_only_wrappers() -> None:
    with pytest.raises(CandidateSqlGenerationError, match="empty SQL wrapper"):
        unwrap_sql_completion("### SQL\n```sql\n```")
