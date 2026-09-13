"""Contracts for offline candidate-SQL generation in trusted workspaces.

The product runtime owns routing, catalog retrieval, policy enforcement and
result validation.  This module only renders the bounded context supplied by
those server-owned components for a small offline candidate generator.  It
intentionally has no Transformers, PEFT, database, or repair dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from .question_router import QuestionRoute


OLIST_CANDIDATE_SQL_PROMPT_VERSION = "olist-candidate-sql-v1"

# These are presentation labels observed from the bounded offline SQL generator.
# They are intentionally exact, single-line matches: normalization must never
# guess at or rewrite model-generated SQL/prose.
# Models sometimes emit a presentation heading before the SQL even when the
# prompt asks for SQL only.  These are exact, bounded headings; normalization
# never strips arbitrary prose or repairs SQL.
_DISPLAY_PREFIXES = frozenset({"Query", "Query Plan", "Code", "Selection", "Solution"})
_SQL_OPENING = re.compile(r"^(?:SELECT|WITH)\b", flags=re.IGNORECASE)
_PRESENTATION_WORD = re.compile(r"^[\w-]+$", flags=re.UNICODE)


class CandidateSqlGenerationError(ValueError):
    """The offline candidate-generation contract cannot be constructed."""


@dataclass(frozen=True)
class CandidateSqlContext:
    """The complete server-derived input permitted to reach a SQL candidate model."""

    question: str
    catalog_prompt: str
    query_plan_prompt: str
    required_result_columns: tuple[str, ...]
    dialect: str = "PostgreSQL"

    def __post_init__(self) -> None:
        for name in ("question", "catalog_prompt", "query_plan_prompt", "dialect"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise CandidateSqlGenerationError(f"{name} must be a non-empty string")
        if not self.required_result_columns:
            raise CandidateSqlGenerationError("required_result_columns must not be empty")
        if any(
            not isinstance(column, str) or not column.strip()
            for column in self.required_result_columns
        ):
            raise CandidateSqlGenerationError(
                "required_result_columns must contain non-empty strings"
            )


def require_database_route(route: QuestionRoute) -> None:
    """Fail before model inference when deterministic routing opted out of SQL."""
    if not route.should_generate_sql:
        raise CandidateSqlGenerationError(
            "candidate SQL generation is forbidden for a non-database route"
        )


def render_candidate_sql_prompt(context: CandidateSqlContext) -> str:
    """Render a SQL-only prompt without adding inferred schema or business rules."""
    allowed_columns = ", ".join(f"`{column}`" for column in context.required_result_columns)
    return "\n".join(
        [
            "### Task",
            "Generate exactly one read-only SQL query for the supplied business question.",
            f"### SQL dialect\n{context.dialect}",
            "### Candidate contract",
            "- Return SQL only: no Markdown, explanation, tool call, or prose.",
            "- Generate one SELECT or WITH ... SELECT statement only.",
            "- Use only tables, columns, joins, metrics, and business rules in the server-provided Catalog.",
            "- Do not invent a metric definition, join, attribution rule, filter, table, or column.",
            "- The final top-level SELECT must return only these result columns: "
            f"{allowed_columns}.",
            "- The server will independently enforce AST policy, readonly PostgreSQL access, and the result contract.",
            "### Server-provided Semantic Catalog",
            context.catalog_prompt.strip(),
            "### Server-provided Query Plan",
            context.query_plan_prompt.strip(),
            "### Question",
            context.question.strip(),
            "### SQL",
        ]
    )


def unwrap_sql_completion(completion: str) -> str:
    """Remove a bounded leading presentation wrapper; never repair SQL.

    The returned content deliberately remains untouched when it contains prose,
    multiple statements, DDL/DML, invalid identifiers, or an unsupported dialect.
    Those properties must be rejected by the existing server-side SQL policy.
    """
    if not isinstance(completion, str) or not completion.strip():
        raise CandidateSqlGenerationError("model generated an empty completion")
    value = completion.strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if len(lines) >= 2 and lines[0].strip().lower() in {"```", "```sql", "```postgresql"}:
            value = "\n".join(lines[1:-1]).strip()
    if value[:4].lower() == "sql:":
        value = value[4:].lstrip()
    lines = value.splitlines()
    if lines and _is_leading_presentation_label(lines):
        value = "\n".join(lines[1:]).strip()
    else:
        # Support the same exact heading when the model places it on the SQL
        # line (for example ``Selection WITH ...``).  Restrict the suffix to
        # a read-query opener so a legitimate SQL identifier is untouched.
        for prefix in sorted(_DISPLAY_PREFIXES, key=len, reverse=True):
            marker = prefix + " "
            if value.startswith(marker):
                suffix = value[len(marker):].lstrip()
                if suffix[:4].upper() == "WITH" or suffix[:6].upper() == "SELECT":
                    value = suffix
                    break
    # A generator may emit a display label followed by the already-supported
    # ``SQL:`` marker.  Remove that second presentation layer only; all SQL
    # content remains untouched for the downstream policy gate.
    if value[:4].lower() == "sql:":
        value = value[4:].lstrip()
    if not value:
        raise CandidateSqlGenerationError("model generated only an empty SQL wrapper")
    return value


def _is_leading_presentation_label(lines: list[str]) -> bool:
    """Recognize one short display title only when a SQL query immediately follows.

    This is intentionally not a general prose-to-SQL extractor.  It removes at
    most the first line, requires the next non-empty line to be a ``SELECT`` or
    ``WITH`` query opener, and accepts only a narrow title grammar.  Everything
    else is returned unchanged for the AST policy to reject or accept.
    """

    title = lines[0].strip()
    if not title or len(title) > 64 or _SQL_OPENING.match(title):
        return False
    if any(marker in title for marker in (";", "`", "--", "/*", "*/")):
        return False

    words = title.split()
    is_known_title = title in _DISPLAY_PREFIXES
    # Generalize beyond a fixed vocabulary without accepting arbitrary prose:
    # a title is one word ("Proposal", "Answer") or a two-word SQL-labelled
    # heading ("SQL Query").  Existing exact multiword prefixes stay supported.
    is_generic_title = (
        1 <= len(words) <= 2
        and all(_PRESENTATION_WORD.fullmatch(word) for word in words)
        and (len(words) == 1 or any(word.lower() == "sql" for word in words))
    )
    if not (is_known_title or is_generic_title):
        return False

    for next_line in lines[1:]:
        candidate = next_line.strip()
        if not candidate:
            continue
        if candidate[:4].lower() == "sql:":
            candidate = candidate[4:].lstrip()
        return bool(_SQL_OPENING.match(candidate))
    return False
