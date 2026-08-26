"""Canonical Spider schema/question/SQL serialization for SFT and inference.

The format is intentionally small and deterministic.  It contains table names,
column names and foreign-key relationships, but never database rows.  Keeping
this module shared prevents a base-versus-adapter comparison from accidentally
using a different prompt than the training corpus.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Mapping


SPACE_RE = re.compile(r"\s+")
SQL_MARKER = "\n\n### SQL\n"
# Keep v1 immutable: completed historical Base/Adapter comparisons used it.
PROMPT_FORMAT_VERSION = "spider-sft-schema-question-sql-v1"
PROMPT_FORMAT_VERSION_V2 = "spider-sft-schema-question-sql-v2"
SUPPORTED_PROMPT_FORMAT_VERSIONS = frozenset(
    {PROMPT_FORMAT_VERSION, PROMPT_FORMAT_VERSION_V2}
)


class SpiderSftFormatError(ValueError):
    """A Spider table metadata item cannot be serialized safely."""


def normalize_question(question: str) -> str:
    """Normalize formatting noise without changing benchmark semantics."""

    if not isinstance(question, str) or not question.strip():
        raise SpiderSftFormatError("question must be a non-empty string")
    return SPACE_RE.sub(" ", question.replace("\x00", " ")).strip()


def _normalize_schema_metadata(
    table: Mapping[str, Any], *, require_types_and_primary_keys: bool
) -> tuple[list[str], list[tuple[int, str]], list[str] | None, list[int]]:
    """Validate Spider metadata and return normalized table/column identities."""

    names = table.get("table_names_original")
    columns = table.get("column_names_original")
    if not isinstance(names, list) or not all(isinstance(name, str) and name for name in names):
        raise SpiderSftFormatError("table_names_original must be a non-empty string list")
    if not isinstance(columns, list):
        raise SpiderSftFormatError("column_names_original must be a list")

    normalized_columns: list[tuple[int, str]] = []
    for column in columns:
        if not isinstance(column, (list, tuple)) or len(column) != 2:
            raise SpiderSftFormatError("each column_names_original entry must be a pair")
        table_index, column_name = column
        if not isinstance(table_index, int) or not isinstance(column_name, str) or not column_name:
            raise SpiderSftFormatError("column metadata has an invalid table index or name")
        if table_index < -1 or table_index >= len(names):
            raise SpiderSftFormatError("column metadata references an unknown table")
        normalized_columns.append((table_index, column_name))

    types = table.get("column_types")
    if require_types_and_primary_keys:
        if not isinstance(types, list) or len(types) != len(normalized_columns):
            raise SpiderSftFormatError("column_types must align with column_names_original")
        if not all(isinstance(column_type, str) and column_type for column_type in types):
            raise SpiderSftFormatError("column_types must be a non-empty string list")
        primary_keys = table.get("primary_keys", [])
        if not isinstance(primary_keys, list) or not all(
            isinstance(index, int) for index in primary_keys
        ):
            raise SpiderSftFormatError("primary_keys must be an integer list")
        for index in primary_keys:
            if not 0 <= index < len(normalized_columns) or normalized_columns[index][0] < 0:
                raise SpiderSftFormatError("primary key references an unknown column")
        return list(names), normalized_columns, list(types), primary_keys
    return list(names), normalized_columns, None, []


def _normalized_foreign_keys(
    table: Mapping[str, Any], normalized_columns: list[tuple[int, str]]
) -> list[tuple[int, int]]:
    foreign_keys = table.get("foreign_keys", [])
    if not isinstance(foreign_keys, list):
        raise SpiderSftFormatError("foreign_keys must be a list")
    normalized: list[tuple[int, int]] = []
    for foreign_key in foreign_keys:
        if not isinstance(foreign_key, (list, tuple)) or len(foreign_key) != 2:
            raise SpiderSftFormatError("each foreign key must be a column-index pair")
        left, right = foreign_key
        if not isinstance(left, int) or not isinstance(right, int):
            raise SpiderSftFormatError("foreign-key indexes must be integers")
        if not 0 <= left < len(normalized_columns) or not 0 <= right < len(normalized_columns):
            raise SpiderSftFormatError("foreign key references an unknown column")
        if normalized_columns[left][0] < 0 or normalized_columns[right][0] < 0:
            raise SpiderSftFormatError("foreign key cannot reference the wildcard column")
        normalized.append((left, right))
    return normalized


def serialize_spider_schema(table: Mapping[str, Any]) -> str:
    """Render the immutable v1 Spider metadata representation."""

    names, normalized_columns, _, _ = _normalize_schema_metadata(
        table, require_types_and_primary_keys=False
    )
    grouped: dict[int, list[str]] = defaultdict(list)
    for table_index, column_name in normalized_columns:
        if table_index >= 0:
            grouped[table_index].append(column_name)

    lines = [
        f"TABLE {table_name}: {', '.join(grouped.get(index, [])) or '<no_columns>'}"
        for index, table_name in enumerate(names)
    ]
    foreign_keys = _normalized_foreign_keys(table, normalized_columns)
    if foreign_keys:
        references: list[str] = []
        for left, right in foreign_keys:
            references.append(f"{normalized_columns[left][1]} -> {normalized_columns[right][1]}")
        lines.append("FOREIGN_KEYS: " + "; ".join(references))
    return "\n".join(lines)


def serialize_spider_schema_v2(table: Mapping[str, Any]) -> str:
    """Render qualified table-column identities, types, PKs and full FKs.

    This format is deliberately separate from v1.  It makes foreign-key sides
    unambiguous and exposes key/type cues that are useful for schema linking,
    while still excluding database rows and values.
    """

    names, normalized_columns, types, primary_keys = _normalize_schema_metadata(
        table, require_types_and_primary_keys=True
    )
    assert types is not None  # Narrowed by require_types_and_primary_keys.
    grouped: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for column_index, (table_index, column_name) in enumerate(normalized_columns):
        if table_index >= 0:
            grouped[table_index].append((column_index, column_name))

    primary_key_set = set(primary_keys)
    lines: list[str] = []
    for table_index, table_name in enumerate(names):
        lines.append(f"TABLE {table_name}")
        columns = grouped.get(table_index, [])
        if not columns:
            lines.append("  <no_columns>")
            continue
        for column_index, column_name in columns:
            key_marker = " [PRIMARY KEY]" if column_index in primary_key_set else ""
            lines.append(f"  {table_name}.{column_name}: {types[column_index]}{key_marker}")

    foreign_keys = _normalized_foreign_keys(table, normalized_columns)
    if foreign_keys:
        lines.append("FOREIGN_KEYS")
        for left, right in foreign_keys:
            left_table, left_column = normalized_columns[left]
            right_table, right_column = normalized_columns[right]
            lines.append(
                f"  {names[left_table]}.{left_column} -> {names[right_table]}.{right_column}"
            )
    return "\n".join(lines)


def serialize_spider_schema_for_version(
    table: Mapping[str, Any], prompt_format_version: str
) -> str:
    """Select a serializer without silently changing historical prompt contracts."""

    if prompt_format_version == PROMPT_FORMAT_VERSION:
        return serialize_spider_schema(table)
    if prompt_format_version == PROMPT_FORMAT_VERSION_V2:
        return serialize_spider_schema_v2(table)
    raise SpiderSftFormatError(f"unsupported prompt format version: {prompt_format_version}")


def render_sft_prompt(
    question: str, schema: str, prompt_format_version: str = PROMPT_FORMAT_VERSION
) -> str:
    """Render the common inference prefix ending at the SQL completion point."""

    if not isinstance(schema, str) or not schema.strip():
        raise SpiderSftFormatError("schema must be a non-empty string")
    if prompt_format_version not in SUPPORTED_PROMPT_FORMAT_VERSIONS:
        raise SpiderSftFormatError(f"unsupported prompt format version: {prompt_format_version}")
    return f"### SQLite schema\n{schema}\n\n### Question\n{normalize_question(question)}{SQL_MARKER}"


def render_sft_training_text(
    question: str,
    schema: str,
    sql: str,
    prompt_format_version: str = PROMPT_FORMAT_VERSION,
) -> str:
    """Render one SFT text record, retaining the target only outside Git."""

    if not isinstance(sql, str) or not sql.strip():
        raise SpiderSftFormatError("SQL target must be a non-empty string")
    return render_sft_prompt(question, schema, prompt_format_version) + sql.strip()
