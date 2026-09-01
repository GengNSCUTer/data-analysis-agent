from __future__ import annotations

import importlib.util
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ACQUIRE = _module("acquire_cspider_for_build_test", ROOT / "scripts/post_training/data/acquire_cspider.py")
BUILD = _module(
    "build_cspider_sft_splits", ROOT / "scripts/post_training/data/build_cspider_sft_splits.py"
)


def _sqlite_bytes(path: Path) -> bytes:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE data (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    return path.read_bytes()


def _table(db_id: str) -> dict[str, object]:
    return {
        "db_id": db_id,
        "table_names_original": ["data"],
        "column_names_original": [[-1, "*"], [0, "id"]],
        "column_types": ["text", "number"],
        "primary_keys": [1],
        "foreign_keys": [],
    }


def _write_full_release(path: Path) -> None:
    sqlite_path = path.parent / "fixture.sqlite"
    database = _sqlite_bytes(sqlite_path)
    records = {
        "train": {"db_id": "train_db", "question": "列出全部编号", "query": "SELECT id FROM data"},
        "dev": {"db_id": "dev_db", "question": "返回编号", "query": "SELECT id FROM data"},
        "test": {"db_id": "test_db", "question": "测试编号", "query": "SELECT id FROM data"},
    }
    prefix = "full_CSpider/CSpider/"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(prefix + "train.json", json.dumps([records["train"]], ensure_ascii=False))
        archive.writestr(prefix + "dev.json", json.dumps([records["dev"]], ensure_ascii=False))
        archive.writestr(prefix + "test_data/test.json", json.dumps([records["test"]], ensure_ascii=False))
        archive.writestr(prefix + "tables.json", json.dumps([_table("train_db"), _table("dev_db")]))
        archive.writestr(prefix + "test_data/tables_test.json", json.dumps([_table("test_db")]))
        archive.writestr(prefix + "test_data/test_gold.sql", "SELECT id FROM data\n")
        archive.writestr(prefix + "database/train_db/train_db.sqlite", database)
        archive.writestr(prefix + "database/dev_db/dev_db.sqlite", database)
        archive.writestr(prefix + "test_database/test_db/test_db.sqlite", database)


def _extracted_release(tmp_path: Path) -> Path:
    archive = tmp_path / "full_CSpider.zip"
    _write_full_release(archive)
    extracted = tmp_path / "extracted"
    ACQUIRE.acquire(archive, extracted)
    return extracted


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_builder_preserves_official_roles_and_shared_prompt_contract(tmp_path: Path) -> None:
    extracted = _extracted_release(tmp_path)
    output = tmp_path / "prepared"

    audit = BUILD.build_splits(extracted, output, generated_at="2026-09-01T00:00:00Z")

    train = _read_jsonl(output / "train.jsonl")
    validation = _read_jsonl(output / "validation.jsonl")
    test = _read_jsonl(output / "final_evaluation_only/test.jsonl")
    assert train[0]["split"] == {
        "name": "train",
        "official_name": "train",
        "group": BUILD.split_group("train_db"),
    }
    assert validation[0]["split"]["name"] == "validation"
    assert test[0]["split"]["name"] == "test"
    assert "### Question\n列出全部编号" in train[0]["training_text"]
    assert train[0]["candidate_sql"] == "SELECT id FROM data"
    assert audit["policy"]["test_forbidden_for_training"] is True
    assert audit["checks"]["sqlite_readonly_explain"] == {
        "dev": {"pass": 1},
        "test": {"pass": 1},
        "train": {"pass": 1},
    }


def test_builder_rejects_source_tree_drift_without_output(tmp_path: Path) -> None:
    extracted = _extracted_release(tmp_path)
    train_path = extracted / "train.json"
    train_path.write_text("[]", encoding="utf-8")

    with pytest.raises(BUILD.CSpiderBuildError, match="tree does not match"):
        BUILD.build_splits(extracted, tmp_path / "prepared")

    assert not (tmp_path / "prepared").exists()


def test_builder_quarantines_sql_explain_failure_from_sft(tmp_path: Path, monkeypatch) -> None:
    extracted = _extracted_release(tmp_path)
    monkeypatch.setattr(
        BUILD,
        "read_only_explain",
        lambda _path, _sql: {"sqlite_readonly_explain": "error", "error_message": "fixture"},
    )

    output = tmp_path / "prepared"
    audit = BUILD.build_splits(extracted, output)

    assert _read_jsonl(output / "train.jsonl") == []
    excluded = _read_jsonl(output / "source_quality_exclusions/train.jsonl")
    assert excluded[0]["exclusion"] == {
        "reason": "sqlite_readonly_explain_failed",
        "eligible_for_sft": False,
    }
    assert audit["checks"]["source_quality_exclusions"]["train"]["rows"] == 1
