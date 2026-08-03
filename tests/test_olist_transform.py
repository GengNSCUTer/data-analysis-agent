"""Regression coverage for the deterministic Olist analysis-layer transform."""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_RAW_DIRECTORY = REPOSITORY_ROOT / "tests" / "fixtures" / "olist_raw_minimal"
MODULE_PATH = REPOSITORY_ROOT / "scripts" / "transform_olist.py"

spec = importlib.util.spec_from_file_location("transform_olist", MODULE_PATH)
assert spec and spec.loader
transform_olist = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = transform_olist
spec.loader.exec_module(transform_olist)


def test_transform_maps_the_full_minimal_release(tmp_path: Path) -> None:
    report = transform_olist.transform(
        FIXTURE_RAW_DIRECTORY,
        tmp_path / "normalized",
        "olist-mini-v1",
    )

    assert report["transform_version"] == "olist-analytics-v1"
    assert report["tables"] == {
        "dim_customers": 1,
        "dim_sellers": 1,
        "dim_category_translation": 1,
        "dim_products": 1,
        "fact_orders": 1,
        "fact_order_items": 1,
        "fact_payments": 1,
        "fact_reviews": 1,
        "rejected_fact_reviews_orphan_orders": 0,
    }

    with (tmp_path / "normalized" / "dim_products.csv").open(newline="") as file:
        product = next(csv.DictReader(file))
    assert product["product_name_length"] == "40"
    assert product["product_description_length"] == "287"
    assert product["dataset_version_id"] == "olist-mini-v1"

    with (tmp_path / "normalized" / "fact_order_items.csv").open(newline="") as file:
        item = next(csv.DictReader(file))
    assert item["price"] == "58.90"
    assert item["freight_value"] == "13.29"


def test_transform_rejects_invalid_timestamps(tmp_path: Path) -> None:
    raw_directory = tmp_path / "raw"
    raw_directory.mkdir()
    for source_path in FIXTURE_RAW_DIRECTORY.iterdir():
        (raw_directory / source_path.name).write_text(source_path.read_text(), encoding="utf-8")
    orders_path = raw_directory / "olist_orders_dataset.csv"
    orders_path.write_text(
        orders_path.read_text(encoding="utf-8").replace("2017-10-02 10:56:33", "not-a-date"),
        encoding="utf-8",
    )

    with pytest.raises(transform_olist.TransformError, match="olist_orders_dataset.csv:2"):
        transform_olist.transform(raw_directory, tmp_path / "normalized", "olist-mini-v1")


def test_verify_source_detects_checksum_mismatch(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """datasets:
  - id: olist_brazilian_ecommerce
    source_metadata:
      files:
        - name: olist_customers_dataset.csv
          sha256: "0000000000000000000000000000000000000000000000000000000000000000"
""",
        encoding="utf-8",
    )

    with pytest.raises(transform_olist.TransformError, match="checksum mismatch"):
        transform_olist.verify_source(FIXTURE_RAW_DIRECTORY, manifest)
