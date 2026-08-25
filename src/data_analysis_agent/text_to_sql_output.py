"""Conservative normalization of presentation wrappers around model SQL output.

Offline Text-to-SQL evaluation receives a model completion, not a parser AST.
Some instruction-tuned models add a Markdown fence, ``SQLQuery:`` prefix, or a
separate ``### Answer``/``### Explanation`` section after a complete SQL query.
Those wrappers are not part of the candidate SQL and must be removed the same
way for every compared model. This module never repairs SQL syntax, identifiers,
joins, literals, or semantics.
"""

from __future__ import annotations

from typing import Final


_SQL_PREFIXES: Final[tuple[str, ...]] = ("sqlquery:", "sql:")
_SQL_SECTION_MARKERS: Final[frozenset[str]] = frozenset({"### sql", "## sql"})


def normalize_text_to_sql_candidate(completion: str) -> str:
    """Remove unambiguous presentation wrappers without changing SQL content.

    The function intentionally leaves arbitrary prose, malformed SQL, comments,
    and multi-statement output untouched. Downstream AST policy remains the sole
    authority on whether the resulting text is executable or allowed.
    """

    lines = completion.strip().splitlines()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not output:
            if not stripped or stripped.lower() in _SQL_SECTION_MARKERS:
                continue
            if stripped.startswith("```"):
                continue
            prefix = next(
                (
                    candidate
                    for candidate in _SQL_PREFIXES
                    if stripped.lower().startswith(candidate)
                ),
                None,
            )
            if prefix is not None:
                remainder = stripped[len(prefix) :].lstrip()
                if remainder:
                    output.append(remainder)
                continue
            output.append(line)
            continue

        if stripped.startswith("```") or stripped.startswith("###"):
            break
        output.append(line)

    return "\n".join(output).strip()
