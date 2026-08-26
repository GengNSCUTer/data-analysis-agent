"""Contracts for offline candidate-SQL generation in trusted workspaces.

The product runtime owns routing, catalog retrieval, policy enforcement and
result validation.  This module only renders the bounded context supplied by
those server-owned components for a small offline candidate generator.  It
intentionally has no Transformers, PEFT, database, or repair dependency.
"""

from __future__ import annotations

from dataclasses import dataclass

from .question_router import QuestionRoute


OLIST_CANDIDATE_SQL_PROMPT_VERSION = "olist-candidate-sql-v1"


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
    """Remove only a single outer SQL wrapper; never repair model-generated SQL.

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
    if not value:
        raise CandidateSqlGenerationError("model generated only an empty SQL wrapper")
    return value
