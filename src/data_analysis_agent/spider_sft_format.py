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
PROMPT_FORMAT_VERSION = "spider-sft-schema-question-sql-v1"


class SpiderSftFormatError(ValueError):
    """A Spider table metadata item cannot be serialized safely."""


def normalize_question(question: str) -> str:
    """Normalize formatting noise without changing benchmark semantics."""

    if not isinstance(question, str) or not question.strip():
        raise SpiderSftFormatError("question must be a non-empty string")
    return SPACE_RE.sub(" ", question.replace("\x00", " ")).strip()


def serialize_spider_schema(table: Mapping[str, Any]) -> str:
    """Render Spider table metadata in the exact SFT prompt representation."""

    names = table.get("table_names_original")
    columns = table.get("column_names_original")
    foreign_keys = table.get("foreign_keys", [])
    if not isinstance(names, list) or not all(isinstance(name, str) and name for name in names):
        raise SpiderSftFormatError("table_names_original must be a non-empty string list")
    if not isinstance(columns, list):
        raise SpiderSftFormatError("column_names_original must be a list")
    if not isinstance(foreign_keys, list):
        raise SpiderSftFormatError("foreign_keys must be a list")

    grouped: dict[int, list[str]] = defaultdict(list)
    normalized_columns: list[tuple[int, str]] = []
    for column in columns:
        if not isinstance(column, (list, tuple)) or len(column) != 2:
            raise SpiderSftFormatError("each column_names_original entry must be a pair")
        table_index, column_name = column
        if not isinstance(table_index, int) or not isinstance(column_name, str) or not column_name:
            raise SpiderSftFormatError("column metadata has an invalid table index or name")
        if table_index >= len(names):
            raise SpiderSftFormatError("column metadata references an unknown table")
        normalized_columns.append((table_index, column_name))
        if table_index >= 0:
            grouped[table_index].append(column_name)

    lines = [
        f"TABLE {table_name}: {', '.join(grouped.get(index, [])) or '<no_columns>'}"
        for index, table_name in enumerate(names)
    ]
    if foreign_keys:
        references: list[str] = []
        for foreign_key in foreign_keys:
            if not isinstance(foreign_key, (list, tuple)) or len(foreign_key) != 2:
                raise SpiderSftFormatError("each foreign key must be a column-index pair")
            left, right = foreign_key
            if not isinstance(left, int) or not isinstance(right, int):
                raise SpiderSftFormatError("foreign-key indexes must be integers")
            try:
                references.append(
                    f"{normalized_columns[left][1]} -> {normalized_columns[right][1]}"
                )
            except IndexError as exc:
                raise SpiderSftFormatError("foreign key references an unknown column") from exc
        lines.append("FOREIGN_KEYS: " + "; ".join(references))
    return "\n".join(lines)


def render_sft_prompt(question: str, schema: str) -> str:
    """Render the common inference prefix ending at the SQL completion point."""

    if not isinstance(schema, str) or not schema.strip():
        raise SpiderSftFormatError("schema must be a non-empty string")
    return f"### SQLite schema\n{schema}\n\n### Question\n{normalize_question(question)}{SQL_MARKER}"


def render_sft_training_text(question: str, schema: str, sql: str) -> str:
    """Render one SFT text record, retaining the target only outside Git."""

    if not isinstance(sql, str) or not sql.strip():
        raise SpiderSftFormatError("SQL target must be a non-empty string")
    return render_sft_prompt(question, schema) + sql.strip()
