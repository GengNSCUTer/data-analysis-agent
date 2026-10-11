"""Contracts for offline candidate-SQL generation in trusted workspaces.

The product runtime owns routing, catalog retrieval, policy enforcement and
result validation.  This module only renders the bounded context supplied by
those server-owned components for a small offline candidate generator.  It
intentionally has no Transformers, PEFT, database, or repair dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import TYPE_CHECKING

from .question_router import QuestionRoute

if TYPE_CHECKING:
    from .query_plan import QueryPlan
    from .semantic_catalog import CatalogSelection, ResultContract
    from .workspace import WorkspaceProfile


OLIST_CANDIDATE_SQL_PROMPT_VERSION = "olist-candidate-sql-v1"
RUNTIME_CANDIDATE_SQL_PROMPT_VERSION = "runtime-candidate-sql-v2"

# This is the sole completion-unwrapping contract for runtime candidates, DPO,
# SQLite diagnostics and the official Spider bridge. Models sometimes add a
# presentation wrapper even when asked for SQL only. The accepted wrappers are
# deliberately finite and local: this function never searches for SQL inside
# arbitrary prose and never repairs generated SQL.
_DISPLAY_PREFIXES = frozenset({"Query", "Query Plan", "Code", "Selection", "Solution"})
_SQL_OPENING = re.compile(r"^(?:SELECT|WITH)\b", flags=re.IGNORECASE)
_PRESENTATION_WORD = re.compile(r"^[\w-]+$", flags=re.UNICODE)
_SQL_PREFIXES = ("sqlquery:", "sql:")
_SQL_SECTION_MARKERS = frozenset({"### sql", "## sql"})
_CODE_FENCE_OPENERS = frozenset({"```", "```sql", "```postgresql"})
_TRAILING_PRESENTATION_SECTION = re.compile(
    r"^#{2,3}\s+(?:answer|explanation)\b", flags=re.IGNORECASE
)


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


@dataclass(frozen=True)
class CandidateSqlRequest:
    """Immutable SQL-component input, shared by runtime and data replay.

    These JSON strings contain server-derived contracts, never an offline
    QuerySpec, assistant summary, SQL target, result rows or tool transcript.
    This is a component boundary, not the full conversational Agent request.
    """

    question: str
    catalog_prompt: str
    scope_json: str
    query_plan_json: str
    result_contract_json: str

    @property
    def prompt_sha256(self) -> str:
        return hashlib.sha256(render_runtime_candidate_prompt(self).encode("utf-8")).hexdigest()

    def safe_evidence(self) -> dict[str, str]:
        """Only version and fingerprint may be copied to safe run evidence."""
        return {
            "prompt_version": RUNTIME_CANDIDATE_SQL_PROMPT_VERSION,
            "prompt_sha256": self.prompt_sha256,
        }


def build_runtime_candidate_request(
    *,
    question: str,
    selection: CatalogSelection,
    query_plan: QueryPlan,
    result_contract: ResultContract,
    workspace: WorkspaceProfile,
    route: QuestionRoute,
) -> CandidateSqlRequest:
    """Build from actual runtime facts; there is intentionally no Spec argument.

    Call only after server routing and admission. Shared construction prevents
    offline callers from quietly adding the correct plan from the Gold label.
    A successful build is not SQL admission or proof that NL parsing is right.
    """
    require_database_route(route)
    if not isinstance(question, str) or not question.strip() or not selection.prompt.strip():
        raise CandidateSqlGenerationError("question and selected Catalog must be non-empty")
    for field in ("catalog_version", "dataset_version", "metric_version", "policy_version"):
        if not (
            getattr(selection.trace, field)
            == getattr(result_contract, field)
            == getattr(workspace, field)
        ):
            raise CandidateSqlGenerationError(f"workspace/selection/contract {field} mismatch")
    if not query_plan.required_result_columns or (
        query_plan.required_result_columns != result_contract.required_result_columns
    ):
        raise CandidateSqlGenerationError("QueryPlan/ResultContract result columns mismatch")
    if set(query_plan.metric_ids) != set(result_contract.metric_ids):
        raise CandidateSqlGenerationError("QueryPlan/ResultContract metrics mismatch")

    def encode(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    scope = {
        "workspace_id": workspace.workspace_id,
        "dialect": workspace.sql_dialect,
        **{field: getattr(workspace, field) for field in (
            "catalog_version", "dataset_version", "metric_version", "policy_version"
        )},
    }
    return CandidateSqlRequest(
        question=question.strip(),
        catalog_prompt=selection.prompt.strip(),
        scope_json=encode(scope),
        query_plan_json=encode(query_plan.as_dict()),
        result_contract_json=encode(result_contract.as_tool_metadata()),
    )


def render_runtime_candidate_prompt(request: CandidateSqlRequest) -> str:
    """Render the same SQL-only request in data, evaluation and future serving."""
    return "\n".join([
        "### Task",
        "Generate exactly one read-only PostgreSQL SELECT or WITH ... SELECT query.",
        "Return SQL only. Do not return Markdown, explanation or a tool call.",
        "Use only the supplied Catalog objects, joins and business definitions.",
        "Preserve metric fact grains; aggregate each metric before combining fact domains.",
        "Return exactly the QueryPlan required_result_columns in their declared order.",
        "The server independently checks AST policy, readonly access and ResultContract.",
        "### Server-provided Workspace",
        request.scope_json,
        "### Server-provided Semantic Catalog",
        request.catalog_prompt,
        "### Server-provided QueryPlan",
        request.query_plan_json,
        "### Server-provided ResultContract",
        request.result_contract_json,
        "### Question",
        request.question,
        "### SQL",
    ])


def unwrap_sql_completion(completion: str) -> str:
    """Return SQL after removing only a bounded presentation wrapper.

    Supported wrappers are one leading SQL section heading, one fenced code
    block, ``SQLQuery:`` / ``SQL:``, one short presentation heading and a
    trailing ``### Answer`` / ``### Explanation`` section. Everything else,
    including prose, multiple statements, unsafe SQL and invalid identifiers,
    remains untouched for the downstream policy to decide. This is not a SQL
    parser or repair function.
    """
    if not isinstance(completion, str) or not completion.strip():
        raise CandidateSqlGenerationError("model generated an empty completion")
    lines = completion.strip().splitlines()
    if lines and lines[0].strip().lower() in _SQL_SECTION_MARKERS:
        lines = lines[1:]
    if lines and lines[0].strip().lower() in _CODE_FENCE_OPENERS:
        lines = lines[1:]
        for closing_index, line in enumerate(lines):
            if line.strip() == "```":
                lines = lines[:closing_index]
                break
    value = "\n".join(lines).strip()
    value = _strip_leading_sql_prefix(value)
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
    # A generator may emit a display label followed by a SQL marker. Remove
    # that second presentation layer only; SQL content remains untouched.
    value = _strip_leading_sql_prefix(value)
    value = _strip_trailing_presentation_section(value)
    if not value:
        raise CandidateSqlGenerationError("model generated only an empty SQL wrapper")
    return value


def _strip_leading_sql_prefix(value: str) -> str:
    """Strip exactly one known SQL presentation prefix at the completion start."""

    lowered = value.lower()
    for prefix in _SQL_PREFIXES:
        if lowered.startswith(prefix):
            return value[len(prefix) :].lstrip()
    return value


def _strip_trailing_presentation_section(value: str) -> str:
    """Drop a known Markdown presentation tail after an already-started SQL body."""

    output: list[str] = []
    for line in value.splitlines():
        stripped = line.strip()
        if output and (stripped == "```" or _TRAILING_PRESENTATION_SECTION.match(stripped)):
            break
        output.append(line)
    return "\n".join(output).strip()


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
