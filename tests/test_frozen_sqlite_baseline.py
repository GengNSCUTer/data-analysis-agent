from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from data_analysis_agent.frozen_sqlite_baseline import (
    BaselineGenerationError,
    ModelCompletion,
    ensure_path_outside_repository,
    extract_sql_candidate,
    generate_predictions,
    load_existing_prediction_case_ids,
    load_native_generation_cases,
    render_sql_prompt,
    render_sqlite_schema,
)
from data_analysis_agent.sqlite_benchmark import (
    BenchmarkRunMetadata,
    load_normalized_cases,
    run_sqlite_benchmark,
)


@pytest.fixture()
def generation_database_root(tmp_path: Path) -> Path:
    database_root = tmp_path / "database"
    database_directory = database_root / "shop"
    database_directory.mkdir(parents=True)
    database_path = database_directory / "shop.sqlite"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE customers (
                customer_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE orders (
                order_id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                total NUMERIC NOT NULL,
                FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
            );
            INSERT INTO customers VALUES (1, 'Ada');
            INSERT INTO orders VALUES (1, 1, 9.5);
            """
        )
        connection.commit()
    finally:
        connection.close()
    return database_root


class StaticChatClient:
    def __init__(self, content: str):
        self.content = content
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> ModelCompletion:
        self.prompts.append(prompt)
        return ModelCompletion(
            content=self.content,
            generated_tokens=12,
            generation_elapsed_ms=7,
        )


class FailingAfterFirstChatClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str) -> ModelCompletion:
        self.calls += 1
        if self.calls == 2:
            raise BaselineGenerationError("simulated later model failure")
        return ModelCompletion(
            content="SELECT COUNT(*) AS order_count FROM orders",
            generated_tokens=12,
            generation_elapsed_ms=7,
        )


def test_native_generation_cases_keep_question_only_for_in_memory_prompt() -> None:
    cases = load_native_generation_cases(
        [{"db_id": "shop", "question": "How many orders are there?", "query": "gold"}],
        dataset_id="spider_dev",
    )

    assert cases[0].case_id == "spider_dev:00000"
    assert cases[0].database_path == "shop/shop.sqlite"
    assert cases[0].benchmark_case().__dict__ == {
        "case_id": "spider_dev:00000",
        "database_id": "shop",
        "database_path": "shop/shop.sqlite",
    }


def test_schema_rendering_reads_ddl_without_table_rows(
    generation_database_root: Path,
) -> None:
    case = load_native_generation_cases(
        [{"db_id": "shop", "question": "How many orders are there?"}],
        dataset_id="spider_dev",
    )[0]

    schema = render_sqlite_schema(generation_database_root, case)

    assert "CREATE TABLE customers" in schema
    assert "FOREIGN KEY" in schema
    assert "Ada" not in schema
    assert "9.5" not in schema


def test_schema_rendering_fails_closed_when_budget_is_exceeded(
    generation_database_root: Path,
) -> None:
    case = load_native_generation_cases(
        [{"db_id": "shop", "question": "How many orders are there?"}],
        dataset_id="spider_dev",
    )[0]

    with pytest.raises(BaselineGenerationError, match="exceeds max_schema_characters"):
        render_sqlite_schema(generation_database_root, case, max_schema_characters=10)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("```sql\nSELECT COUNT(*) FROM orders\n```", "SELECT COUNT(*) FROM orders"),
        ("SQLQuery: SELECT * FROM orders; Explanation", "SELECT * FROM orders; Explanation"),
        ("DELETE FROM orders", "DELETE FROM orders"),
        ("SELECT * FROM orders; DROP TABLE orders", "SELECT * FROM orders; DROP TABLE orders"),
    ],
)
def test_sql_extraction_removes_wrappers_without_correcting_sql(
    content: str, expected: str
) -> None:
    assert extract_sql_candidate(content) == expected


def test_generation_output_contains_adapter_contract_but_not_question(
    generation_database_root: Path,
) -> None:
    question = "How many orders are there?"
    cases = load_native_generation_cases(
        [{"db_id": "shop", "question": question}], dataset_id="spider_dev"
    )
    client = StaticChatClient("```sql\nSELECT COUNT(*) AS order_count FROM orders\n```")

    predictions = generate_predictions(
        cases=cases, database_root=generation_database_root, client=client
    )

    assert predictions[0].case_id == "spider_dev:00000"
    assert predictions[0].candidate_sql == "SELECT COUNT(*) AS order_count FROM orders"
    assert predictions[0].generated_tokens == 12
    assert question in client.prompts[0]
    assert question not in json.dumps(predictions[0].__dict__)


def test_each_successful_prediction_can_be_persisted_before_later_failure(
    generation_database_root: Path,
) -> None:
    cases = load_native_generation_cases(
        [
            {"db_id": "shop", "question": "How many orders are there?"},
            {"db_id": "shop", "question": "How many customers are there?"},
        ],
        dataset_id="spider_dev",
    )
    persisted_case_ids: list[str] = []

    with pytest.raises(BaselineGenerationError, match="simulated later model failure"):
        generate_predictions(
            cases=cases,
            database_root=generation_database_root,
            client=FailingAfterFirstChatClient(),
            on_prediction=lambda prediction: persisted_case_ids.append(prediction.case_id),
        )

    assert persisted_case_ids == ["spider_dev:00000"]


def test_generated_prediction_runs_through_existing_sqlite_adapter(
    generation_database_root: Path,
) -> None:
    cases = load_native_generation_cases(
        [{"db_id": "shop", "question": "How many orders are there?"}],
        dataset_id="spider_dev",
    )
    predictions = generate_predictions(
        cases=cases,
        database_root=generation_database_root,
        client=StaticChatClient("SELECT COUNT(*) AS order_count FROM orders"),
    )

    report = run_sqlite_benchmark(
        cases=load_normalized_cases(
            case.benchmark_case().__dict__ for case in cases
        ),
        predictions=predictions,
        metadata=BenchmarkRunMetadata(
            dataset_id="spider_dev",
            dataset_version="fixture",
            model_id="fixture-model",
            model_version="fixture-digest",
            prompt_version="sqlite-frozen-baseline-v1",
        ),
        database_root=generation_database_root,
    )

    assert report["summary"]["executed_candidates"] == 1
    assert report["official_evaluation"]["status"] == "not_run"


def test_generated_multi_statement_reaches_adapter_policy(
    generation_database_root: Path,
) -> None:
    cases = load_native_generation_cases(
        [{"db_id": "shop", "question": "How many orders are there?"}],
        dataset_id="spider_dev",
    )
    predictions = generate_predictions(
        cases=cases,
        database_root=generation_database_root,
        client=StaticChatClient("SELECT COUNT(*) FROM orders; DROP TABLE orders"),
    )

    report = run_sqlite_benchmark(
        cases=load_normalized_cases(
            case.benchmark_case().__dict__ for case in cases
        ),
        predictions=predictions,
        metadata=BenchmarkRunMetadata(
            dataset_id="spider_dev",
            dataset_version="fixture",
            model_id="fixture-model",
            model_version="fixture-digest",
            prompt_version="sqlite-frozen-baseline-v1",
        ),
        database_root=generation_database_root,
    )

    assert report["summary"]["policy_rejected_candidates"] == 1
    assert report["records"][0]["execution"]["status"] == "policy_rejected"


def test_prediction_output_must_stay_outside_repository(tmp_path: Path) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()

    with pytest.raises(BaselineGenerationError, match="outside the repository"):
        ensure_path_outside_repository(repository_root / "predictions.jsonl", repository_root)


def test_prediction_resume_loader_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    output = tmp_path / "predictions.jsonl"
    output.write_text(
        "\n".join(
            [
                json.dumps({"case_id": "spider_dev:00000", "candidate_sql": "SELECT 1"}),
                json.dumps({"case_id": "spider_dev:00000", "candidate_sql": "SELECT 2"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(BaselineGenerationError, match="duplicate case IDs"):
        load_existing_prediction_case_ids(output)
