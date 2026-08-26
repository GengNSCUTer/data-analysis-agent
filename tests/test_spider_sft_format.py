from __future__ import annotations

import pytest

from data_analysis_agent.spider_sft_format import (
    PROMPT_FORMAT_VERSION,
    PROMPT_FORMAT_VERSION_V2,
    SpiderSftFormatError,
    render_sft_prompt,
    render_sft_training_text,
    serialize_spider_schema,
    serialize_spider_schema_for_version,
)


def table_metadata() -> dict[str, object]:
    return {
        "table_names_original": ["customers", "orders"],
        "column_names_original": [
            [-1, "*"],
            [0, "customer_id"],
            [0, "name"],
            [1, "order_id"],
            [1, "customer_id"],
        ],
        "foreign_keys": [[4, 1]],
    }


def test_training_and_inference_share_exact_schema_question_prefix() -> None:
    schema = serialize_spider_schema(table_metadata())
    question = "  List\nall customer names.  "
    training_text = render_sft_training_text(question, schema, "SELECT name FROM customers")

    assert PROMPT_FORMAT_VERSION == "spider-sft-schema-question-sql-v1"
    assert training_text == render_sft_prompt(question, schema) + "SELECT name FROM customers"
    assert schema == (
        "TABLE customers: customer_id, name\n"
        "TABLE orders: order_id, customer_id\n"
        "FOREIGN_KEYS: customer_id -> customer_id"
    )


def test_schema_serializer_rejects_foreign_key_with_unknown_column() -> None:
    malformed = table_metadata()
    malformed["foreign_keys"] = [[7, 1]]

    with pytest.raises(SpiderSftFormatError, match="unknown column"):
        serialize_spider_schema(malformed)


def test_v2_schema_serializer_preserves_qualified_pk_and_fk_identity() -> None:
    metadata = table_metadata()
    metadata["column_types"] = ["text", "number", "text", "number", "number"]
    metadata["primary_keys"] = [1, 3]

    schema = serialize_spider_schema_for_version(metadata, PROMPT_FORMAT_VERSION_V2)

    assert schema == (
        "TABLE customers\n"
        "  customers.customer_id: number [PRIMARY KEY]\n"
        "  customers.name: text\n"
        "TABLE orders\n"
        "  orders.order_id: number [PRIMARY KEY]\n"
        "  orders.customer_id: number\n"
        "FOREIGN_KEYS\n"
        "  orders.customer_id -> customers.customer_id"
    )
    assert render_sft_prompt("List customer names.", schema, PROMPT_FORMAT_VERSION_V2).endswith(
        "\n\n### SQL\n"
    )
