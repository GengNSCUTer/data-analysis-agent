from __future__ import annotations

from data_analysis_agent.text_to_sql_output import normalize_text_to_sql_candidate


def test_normalize_text_to_sql_candidate_removes_known_presentation_wrappers() -> None:
    completion = """```sql
SQLQuery: SELECT name FROM singer;

### Answer
| name |
| --- |
| John |
"""

    assert normalize_text_to_sql_candidate(completion) == "SELECT name FROM singer;"


def test_normalize_text_to_sql_candidate_does_not_repair_sql_content() -> None:
    completion = "SELECT 'unterminated FROM singer"

    assert normalize_text_to_sql_candidate(completion) == completion
