from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from scripts.run_spider_bounded_denotation_audit import main


def _diagnostic_report(final_sql: str, status: str = "executed") -> dict[str, object]:
    return {
        "records": [
            {
                "case_id": "spider_dev:00000",
                "database_path": "shop/shop.sqlite",
                "execution": {
                    "status": status,
                    "final_sql": final_sql if status == "executed" else None,
                },
            }
        ]
    }


def test_bounded_denotation_audit_only_writes_safe_aggregate(tmp_path: Path) -> None:
    database_root = tmp_path / "databases"
    database_path = database_root / "shop" / "shop.sqlite"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE metrics (value INTEGER)")
        connection.execute("INSERT INTO metrics VALUES (7)")

    base_path = tmp_path / "base.json"
    adapter_path = tmp_path / "adapter.json"
    audit_path = tmp_path / "audit.json"
    output_path = tmp_path / "output.json"
    base_path.write_text(json.dumps(_diagnostic_report("SELECT value FROM metrics")), encoding="utf-8")
    adapter_path.write_text(json.dumps(_diagnostic_report("SELECT 0 FROM metrics")), encoding="utf-8")
    audit_path.write_text(
        json.dumps(
            [
                {
                    "source_index": 999,
                    "db_id": "shop",
                    "query": "SELECT value FROM metrics /* secret_gold_sql */",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "--base-report",
                str(base_path),
                "--adapter-report",
                str(adapter_path),
                "--audit-cases",
                str(audit_path),
                "--database-root",
                str(database_root),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["summary"]["base_exact_or_bag_matches"] == 1
    assert report["summary"]["adapter_exact_or_bag_matches"] == 0
    assert report["records"] == [
        {
            "adapter": "mismatch",
            "base": "exact_ordered_match",
            "case_id": "spider_dev:00000",
        }
    ]
    serialized = output_path.read_text(encoding="utf-8")
    assert "secret_gold_sql" not in serialized
    assert "SELECT value" not in serialized


def test_bounded_denotation_audit_compares_invalid_utf8_text_as_original_bytes(
    tmp_path: Path,
) -> None:
    database_root = tmp_path / "databases"
    database_path = database_root / "shop" / "shop.sqlite"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE names (value TEXT)")
        connection.execute("INSERT INTO names VALUES (CAST(X'4E6F6E55544638FF' AS TEXT))")

    base_path = tmp_path / "base.json"
    adapter_path = tmp_path / "adapter.json"
    audit_path = tmp_path / "audit.json"
    output_path = tmp_path / "output.json"
    query = "SELECT value FROM names"
    base_path.write_text(json.dumps(_diagnostic_report(query)), encoding="utf-8")
    adapter_path.write_text(json.dumps(_diagnostic_report(query)), encoding="utf-8")
    audit_path.write_text(
        json.dumps([{"source_index": 1, "db_id": "shop", "query": query}]),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "--base-report",
                str(base_path),
                "--adapter-report",
                str(adapter_path),
                "--audit-cases",
                str(audit_path),
                "--database-root",
                str(database_root),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["summary"]["base_exact_or_bag_matches"] == 1
    assert report["summary"]["adapter_exact_or_bag_matches"] == 1
    assert "NonUTF8" not in output_path.read_text(encoding="utf-8")
