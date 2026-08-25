from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.build_spider_sft_candidates import main


def test_candidate_builder_keeps_train_only_rows_external_and_checked(
    monkeypatch, tmp_path: Path
) -> None:
    train_path = tmp_path / "train_spider.json"
    tables_path = tmp_path / "tables.json"
    holdout_path = tmp_path / "holdout.yaml"
    database_root = tmp_path / "database"
    output_dir = tmp_path / "external-output"
    db_dir = database_root / "shop"
    db_dir.mkdir(parents=True)
    with sqlite3.connect(db_dir / "shop.sqlite") as connection:
        connection.execute("CREATE TABLE products (name TEXT NOT NULL)")

    train_path.write_text(
        json.dumps(
            [
                {
                    "db_id": "shop",
                    "question": "List all product names.",
                    "query": "SELECT name FROM products",
                }
            ]
        ),
        encoding="utf-8",
    )
    tables_path.write_text(
        json.dumps(
            [
                {
                    "db_id": "shop",
                    "table_names_original": ["products"],
                    "column_names_original": [[-1, "*"], [0, "name"]],
                    "foreign_keys": [],
                }
            ]
        ),
        encoding="utf-8",
    )
    holdout_path.write_text(
        "cases:\n  - case_id: data_001\n    forbidden_for_training: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "build_spider_sft_candidates.py",
            "--train-json",
            str(train_path),
            "--tables-json",
            str(tables_path),
            "--database-root",
            str(database_root),
            "--holdout-manifest",
            str(holdout_path),
            "--output-dir",
            str(output_dir),
            "--limit",
            "1",
            "--generated-at",
            "2026-08-25T00:00:00Z",
        ],
    )

    assert main() == 0

    candidate = json.loads((output_dir / "candidates.jsonl").read_text(encoding="utf-8"))
    assert candidate["sample_id"] == "spider_train:00000"
    assert candidate["split"]["name"] == "train"
    assert candidate["execution_outcome"] == {"sqlite_readonly_explain": "pass"}
    assert candidate["target_route"] == {
        "intent": "data_query",
        "requires_database": True,
    }
    assert not {
        "raw_question",
        "raw_result_rows",
        "api_key",
        "cookie",
        "password",
    }.intersection(candidate)

    audit = json.loads((output_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["selection"]["selected_count"] == 1
    assert audit["holdout_check"] == {
        "collisions": [],
        "forbidden_case_count": 1,
        "manifest": str(holdout_path),
        "status": "pass",
    }
