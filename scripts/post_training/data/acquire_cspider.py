#!/usr/bin/env python3
"""Safely extract and structurally verify one official CSpider full release.

This command is deliberately an acquisition boundary, not an SFT builder.  It
does not render prompts, tokenize examples, inspect gold SQL semantics, or
start training.  The output contains the untouched official files plus an
acquisition manifest that records split isolation and source fingerprints.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


EXPECTED_PREFIX = PurePosixPath("full_CSpider/CSpider")
REQUIRED_FILES = (
    PurePosixPath("train.json"),
    PurePosixPath("dev.json"),
    PurePosixPath("tables.json"),
    PurePosixPath("test_data/test.json"),
    PurePosixPath("test_data/test_gold.sql"),
    PurePosixPath("test_data/tables_test.json"),
)
SOURCE_URL = "https://taolusi.github.io/CSpider-explorer/"
LICENSE = "CC BY-SA 4.0"


class CSpiderAcquisitionError(ValueError):
    """Raised when archive contents cannot form an isolated CSpider release."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Empty target for extracted official files and acquisition-manifest.json.",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative_member(info: zipfile.ZipInfo) -> PurePosixPath:
    """Return a validated path below the expected CSpider archive prefix."""
    if "\\" in info.filename:
        raise CSpiderAcquisitionError(f"archive member uses a backslash: {info.filename!r}")
    path = PurePosixPath(info.filename)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CSpiderAcquisitionError(f"unsafe archive member path: {info.filename!r}")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise CSpiderAcquisitionError(f"archive symlink is forbidden: {info.filename!r}")
    if info.is_dir() and path in {EXPECTED_PREFIX.parent, EXPECTED_PREFIX}:
        return PurePosixPath(".")
    try:
        return path.relative_to(EXPECTED_PREFIX)
    except ValueError as exc:
        raise CSpiderAcquisitionError(
            f"archive member escapes expected release root: {info.filename!r}"
        ) from exc


def checked_members(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    members: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    seen: set[PurePosixPath] = set()
    for info in archive.infolist():
        relative = safe_relative_member(info)
        if info.is_dir():
            continue
        if relative in seen:
            raise CSpiderAcquisitionError(f"duplicate archive member: {relative}")
        seen.add(relative)
        members.append((info, relative))

    paths = {relative for _, relative in members}
    missing = sorted(str(path) for path in set(REQUIRED_FILES) - paths)
    if missing:
        raise CSpiderAcquisitionError(f"archive misses required files: {missing}")
    if not any(path.parts[:1] == ("database",) for path in paths):
        raise CSpiderAcquisitionError("archive lacks database/ assets")
    if not any(path.parts[:1] == ("test_database",) for path in paths):
        raise CSpiderAcquisitionError("archive lacks test_database/ assets")
    return members


def load_records(path: Path, split_name: str) -> list[Mapping[str, Any]]:
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CSpiderAcquisitionError(f"{split_name} JSON is invalid: {path}") from exc
    if not isinstance(records, list) or not records:
        raise CSpiderAcquisitionError(f"{split_name} JSON must be a non-empty list")
    for index, row in enumerate(records):
        if not isinstance(row, Mapping):
            raise CSpiderAcquisitionError(f"{split_name}[{index}] is not an object")
        missing = {"db_id", "question", "query"} - row.keys()
        if missing:
            raise CSpiderAcquisitionError(
                f"{split_name}[{index}] misses required fields: {sorted(missing)}"
            )
        if not all(isinstance(row[key], str) and row[key].strip() for key in ("db_id", "question", "query")):
            raise CSpiderAcquisitionError(
                f"{split_name}[{index}] has an empty db_id, question, or query"
            )
    return records


def load_table_db_ids(path: Path, table_name: str) -> set[str]:
    try:
        tables = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CSpiderAcquisitionError(f"{table_name} is invalid JSON: {path}") from exc
    if not isinstance(tables, list) or not tables:
        raise CSpiderAcquisitionError(f"{table_name} must be a non-empty list")
    db_ids = {entry.get("db_id") for entry in tables if isinstance(entry, Mapping)}
    if not db_ids or not all(isinstance(value, str) and value for value in db_ids):
        raise CSpiderAcquisitionError(f"{table_name} has an invalid db_id entry")
    return set(db_ids)


def verify_sqlite_database(database_path: Path) -> None:
    uri = f"file:{database_path.resolve()}?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.Error as exc:
        raise CSpiderAcquisitionError(f"invalid read-only SQLite database: {database_path}") from exc
    finally:
        if connection is not None:
            connection.close()


def split_db_ids(records: Iterable[Mapping[str, Any]]) -> set[str]:
    return {str(row["db_id"]) for row in records}


def database_paths(root: Path, db_ids: set[str], split_name: str) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for db_id in sorted(db_ids):
        candidate = root / db_id / f"{db_id}.sqlite"
        if not candidate.is_file():
            raise CSpiderAcquisitionError(
                f"{split_name} database is missing for db_id={db_id!r}: {candidate}"
            )
        verify_sqlite_database(candidate)
        paths[db_id] = candidate
    return paths


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def verify_release(root: Path, archive_sha256: str) -> dict[str, Any]:
    train = load_records(root / "train.json", "train")
    dev = load_records(root / "dev.json", "dev")
    test = load_records(root / "test_data/test.json", "test")
    split_records = {"train": train, "dev": dev, "test": test}
    split_ids = {name: split_db_ids(records) for name, records in split_records.items()}

    for left, right in (("train", "dev"), ("train", "test"), ("dev", "test")):
        overlap = sorted(split_ids[left].intersection(split_ids[right]))
        if overlap:
            raise CSpiderAcquisitionError(
                f"official split schema overlap between {left} and {right}: {overlap[:5]}"
            )

    main_table_ids = load_table_db_ids(root / "tables.json", "tables.json")
    test_table_ids = load_table_db_ids(root / "test_data/tables_test.json", "tables_test.json")
    for name in ("train", "dev"):
        missing = split_ids[name] - main_table_ids
        if missing:
            raise CSpiderAcquisitionError(f"{name} db_ids missing from tables.json: {sorted(missing)}")
    missing_test_tables = split_ids["test"] - test_table_ids
    if missing_test_tables:
        raise CSpiderAcquisitionError(
            f"test db_ids missing from tables_test.json: {sorted(missing_test_tables)}"
        )

    main_databases = database_paths(root / "database", split_ids["train"] | split_ids["dev"], "train/dev")
    test_databases = database_paths(root / "test_database", split_ids["test"], "test")
    source_files = [
        root / "train.json",
        root / "dev.json",
        root / "tables.json",
        root / "test_data/test.json",
        root / "test_data/test_gold.sql",
        root / "test_data/tables_test.json",
    ]
    return {
        "manifest_version": "1",
        "dataset": {"id": "cspider", "release": "full-2024-03-01", "language": "zh"},
        "source": {"url": SOURCE_URL, "license": LICENSE, "archive_sha256": archive_sha256},
        "extraction": {"expected_archive_prefix": str(EXPECTED_PREFIX), "tree_sha256": tree_sha256(root)},
        "splits": {
            name: {
                "role": {"train": "parameter_updates", "dev": "validation_only", "test": "final_evaluation_only"}[name],
                "forbidden_for_training": name != "train",
                "record_count": len(records),
                "database_count": len(split_ids[name]),
                "database_ids_sha256": hashlib.sha256(
                    "\n".join(sorted(split_ids[name])).encode("utf-8")
                ).hexdigest(),
            }
            for name, records in split_records.items()
        },
        "database_assets": {
            "train_dev_sqlite_count": len(main_databases),
            "test_sqlite_count": len(test_databases),
            "verified_read_only": True,
        },
        "source_files": {
            path.relative_to(root).as_posix(): sha256_file(path) for path in source_files
        },
    }


def acquire(archive_path: Path, output_dir: Path) -> dict[str, Any]:
    archive_path = archive_path.resolve()
    output_dir = output_dir.resolve()
    if not archive_path.is_file():
        raise FileNotFoundError(archive_path)
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    archive_sha256 = sha256_file(archive_path)
    staging_dir = output_dir.parent / f".{output_dir.name}.staging-{uuid.uuid4().hex}"
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = checked_members(archive)
            staging_dir.mkdir()
            for info, relative in members:
                destination = staging_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("xb") as target:
                    shutil.copyfileobj(source, target)
        manifest = verify_release(staging_dir, archive_sha256)
        (staging_dir / "acquisition-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging_dir.replace(output_dir)
        return manifest
    except Exception:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        raise


def main() -> int:
    args = parse_args()
    manifest = acquire(args.archive, args.output_dir)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
