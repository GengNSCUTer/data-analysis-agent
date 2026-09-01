from __future__ import annotations

import importlib.util
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/post_training/data/acquire_cspider.py"
SPEC = importlib.util.spec_from_file_location("acquire_cspider", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _sqlite_bytes(path: Path) -> bytes:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE fixture (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    return path.read_bytes()


def _write_release_archive(path: Path, *, overlap: bool = False, unsafe_member: bool = False) -> None:
    record_ids = {"train": "train_db", "dev": "train_db" if overlap else "dev_db", "test": "test_db"}
    sqlite_file = path.parent / "fixture.sqlite"
    database = _sqlite_bytes(sqlite_file)
    prefix = "full_CSpider/CSpider/"
    with zipfile.ZipFile(path, "w") as archive:
        for split, db_id in record_ids.items():
            relative = "test_data/test.json" if split == "test" else f"{split}.json"
            archive.writestr(
                prefix + relative,
                json.dumps([{"db_id": db_id, "question": "中文问题", "query": "SELECT 1"}]),
            )
        archive.writestr(prefix + "tables.json", json.dumps([{"db_id": record_ids["train"]}, {"db_id": record_ids["dev"]}]))
        archive.writestr(prefix + "test_data/tables_test.json", json.dumps([{"db_id": record_ids["test"]}]))
        archive.writestr(prefix + "test_data/test_gold.sql", "SELECT 1\n")
        written_databases: set[str] = set()
        for split, db_id in record_ids.items():
            root = "test_database" if split == "test" else "database"
            member = prefix + f"{root}/{db_id}/{db_id}.sqlite"
            if member not in written_databases:
                archive.writestr(member, database)
                written_databases.add(member)
        if unsafe_member:
            archive.writestr("../outside.txt", "blocked")


def test_acquire_extracts_official_layout_and_records_split_roles(tmp_path: Path) -> None:
    archive = tmp_path / "full_CSpider.zip"
    _write_release_archive(archive)

    output = tmp_path / "extracted"
    manifest = MODULE.acquire(archive, output)

    assert (output / "train.json").is_file()
    assert (output / "test_database/test_db/test_db.sqlite").is_file()
    assert manifest["splits"]["train"]["role"] == "parameter_updates"
    assert manifest["splits"]["dev"]["forbidden_for_training"] is True
    assert manifest["splits"]["test"]["role"] == "final_evaluation_only"
    assert json.loads((output / "acquisition-manifest.json").read_text(encoding="utf-8")) == manifest


def test_acquire_rejects_unsafe_archive_member_without_output(tmp_path: Path) -> None:
    archive = tmp_path / "full_CSpider.zip"
    _write_release_archive(archive, unsafe_member=True)

    with pytest.raises(MODULE.CSpiderAcquisitionError, match="unsafe archive member path"):
        MODULE.acquire(archive, tmp_path / "extracted")

    assert not (tmp_path / "extracted").exists()


def test_acquire_rejects_schema_overlap_without_output(tmp_path: Path) -> None:
    archive = tmp_path / "full_CSpider.zip"
    _write_release_archive(archive, overlap=True)

    with pytest.raises(MODULE.CSpiderAcquisitionError, match="split schema overlap"):
        MODULE.acquire(archive, tmp_path / "extracted")

    assert not (tmp_path / "extracted").exists()
