"""Normalize the recorded Olist CSV release into PostgreSQL COPY-ready CSV files.

Raw Olist files live outside Git. This script is deliberately a deterministic
column mapper, not a data-cleaning guesser: invalid typed values stop the run.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from typing import Callable

import yaml


TRANSFORM_VERSION = "olist-analytics-v1"
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class TransformError(ValueError):
    """Raised when a raw release cannot be transformed without guessing."""


def _text(value: str) -> str:
    return value.strip()


def _integer(value: str) -> str:
    value = _text(value)
    if not value:
        return ""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise TransformError(f"expected integer, got {value!r}") from exc
    return str(parsed)


def _decimal(value: str) -> str:
    value = _text(value)
    if not value:
        return ""
    try:
        parsed = Decimal(value).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise TransformError(f"expected decimal, got {value!r}") from exc
    return format(parsed, "f")


def _timestamp(value: str) -> str:
    value = _text(value)
    if not value:
        return ""
    try:
        return datetime.strptime(value, TIMESTAMP_FORMAT).strftime(TIMESTAMP_FORMAT)
    except ValueError as exc:
        raise TransformError(f"expected timestamp {TIMESTAMP_FORMAT}, got {value!r}") from exc


def _state(value: str) -> str:
    value = _text(value).upper()
    if len(value) != 2 or not value.isalpha():
        raise TransformError(f"expected two-letter state, got {value!r}")
    return value


Converter = Callable[[str], str]


@dataclass(frozen=True)
class TableSpec:
    source_file: str
    target_table: str
    columns: tuple[tuple[str, str, Converter], ...]


TABLES = (
    TableSpec("olist_customers_dataset.csv", "dim_customers", (
        ("customer_id", "customer_id", _text),
        ("customer_unique_id", "customer_unique_id", _text),
        ("customer_zip_code_prefix", "customer_zip_code_prefix", _text),
        ("customer_city", "customer_city", _text),
        ("customer_state", "customer_state", _state),
    )),
    TableSpec("olist_sellers_dataset.csv", "dim_sellers", (
        ("seller_id", "seller_id", _text),
        ("seller_zip_code_prefix", "seller_zip_code_prefix", _text),
        ("seller_city", "seller_city", _text),
        ("seller_state", "seller_state", _state),
    )),
    TableSpec("product_category_name_translation.csv", "dim_category_translation", (
        ("product_category_name", "product_category_name", _text),
        ("product_category_name_english", "product_category_name_english", _text),
    )),
    TableSpec("olist_products_dataset.csv", "dim_products", (
        ("product_id", "product_id", _text),
        ("product_category_name", "product_category_name", _text),
        ("product_name_lenght", "product_name_length", _integer),
        ("product_description_lenght", "product_description_length", _integer),
        ("product_photos_qty", "product_photos_qty", _integer),
        ("product_weight_g", "product_weight_g", _integer),
        ("product_length_cm", "product_length_cm", _integer),
        ("product_height_cm", "product_height_cm", _integer),
        ("product_width_cm", "product_width_cm", _integer),
    )),
    TableSpec("olist_orders_dataset.csv", "fact_orders", (
        ("order_id", "order_id", _text),
        ("customer_id", "customer_id", _text),
        ("order_status", "order_status", _text),
        ("order_purchase_timestamp", "order_purchase_timestamp", _timestamp),
        ("order_approved_at", "order_approved_at", _timestamp),
        ("order_delivered_carrier_date", "order_delivered_carrier_date", _timestamp),
        ("order_delivered_customer_date", "order_delivered_customer_date", _timestamp),
        ("order_estimated_delivery_date", "order_estimated_delivery_date", _timestamp),
    )),
    TableSpec("olist_order_items_dataset.csv", "fact_order_items", (
        ("order_id", "order_id", _text),
        ("order_item_id", "order_item_id", _integer),
        ("product_id", "product_id", _text),
        ("seller_id", "seller_id", _text),
        ("shipping_limit_date", "shipping_limit_date", _timestamp),
        ("price", "price", _decimal),
        ("freight_value", "freight_value", _decimal),
    )),
    TableSpec("olist_order_payments_dataset.csv", "fact_payments", (
        ("order_id", "order_id", _text),
        ("payment_sequential", "payment_sequential", _integer),
        ("payment_type", "payment_type", _text),
        ("payment_installments", "payment_installments", _integer),
        ("payment_value", "payment_value", _decimal),
    )),
    TableSpec("olist_order_reviews_dataset.csv", "fact_reviews", (
        ("review_id", "review_id", _text),
        ("order_id", "order_id", _text),
        ("review_score", "review_score", _integer),
        ("review_creation_date", "review_creation_date", _timestamp),
    )),
)


def sha256sum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_checksums(manifest_path: Path) -> dict[str, str]:
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    for dataset in manifest["datasets"]:
        if dataset["id"] == "olist_brazilian_ecommerce":
            return {
                file["name"]: file["sha256"]
                for file in dataset["source_metadata"]["files"]
                if file["name"] in {spec.source_file for spec in TABLES}
            }
    raise TransformError("Olist dataset is missing from the manifest")


def verify_source(raw_directory: Path, manifest_path: Path) -> dict[str, str]:
    verified: dict[str, str] = {}
    for filename, expected in expected_checksums(manifest_path).items():
        if not isinstance(expected, str) or not SHA256_PATTERN.fullmatch(expected):
            raise TransformError(f"invalid expected checksum for {filename}")
        path = raw_directory / filename
        if not path.is_file():
            raise TransformError(f"missing source file: {path}")
        actual = sha256sum(path)
        if actual != expected:
            raise TransformError(f"checksum mismatch for {filename}: {actual}")
        verified[filename] = actual
    return verified


def transform(raw_directory: Path, output_directory: Path, dataset_version_id: str) -> dict[str, object]:
    if not dataset_version_id.strip():
        raise TransformError("dataset_version_id must not be empty")
    temporary_directory = output_directory.with_name(f".{output_directory.name}.tmp")
    if temporary_directory.exists():
        shutil.rmtree(temporary_directory)
    temporary_directory.mkdir(parents=True)
    row_counts: dict[str, int] = {}
    try:
        for spec in TABLES:
            source_path = raw_directory / spec.source_file
            if not source_path.is_file():
                raise TransformError(f"missing source file: {source_path}")
            destination_path = temporary_directory / f"{spec.target_table}.csv"
            with source_path.open("r", encoding="utf-8-sig", newline="") as source, destination_path.open(
                "w", encoding="utf-8", newline=""
            ) as destination:
                reader = csv.DictReader(source)
                required_fields = {source_column for source_column, _, _ in spec.columns}
                actual_fields = set(reader.fieldnames or [])
                missing_fields = required_fields - actual_fields
                if missing_fields:
                    raise TransformError(
                        f"missing required columns in {spec.source_file}: {sorted(missing_fields)!r}"
                    )
                writer = csv.DictWriter(
                    destination,
                    fieldnames=[target_column for _, target_column, _ in spec.columns]
                    + ["dataset_version_id"],
                    lineterminator="\n",
                )
                writer.writeheader()
                count = 0
                for line_number, row in enumerate(reader, start=2):
                    try:
                        normalized = {
                            target_column: converter(row[source_column])
                            for source_column, target_column, converter in spec.columns
                        }
                    except TransformError as exc:
                        raise TransformError(f"{spec.source_file}:{line_number}: {exc}") from exc
                    normalized["dataset_version_id"] = dataset_version_id
                    writer.writerow(normalized)
                    count += 1
                row_counts[spec.target_table] = count
        order_ids: set[str] = set()
        with (temporary_directory / "fact_orders.csv").open(newline="") as orders_file:
            order_ids = {row["order_id"] for row in csv.DictReader(orders_file)}
        reviews_path = temporary_directory / "fact_reviews.csv"
        accepted_reviews_path = temporary_directory / ".fact_reviews.accepted.csv"
        rejected_reviews_path = temporary_directory / "rejected_fact_reviews_orphan_orders.csv"
        rejected_count = 0
        accepted_count = 0
        with reviews_path.open(newline="") as source, accepted_reviews_path.open("w", encoding="utf-8", newline="") as accepted, rejected_reviews_path.open("w", encoding="utf-8", newline="") as rejected:
            reader = csv.DictReader(source)
            assert reader.fieldnames is not None
            accepted_writer = csv.DictWriter(accepted, fieldnames=reader.fieldnames, lineterminator="\n")
            rejected_writer = csv.DictWriter(rejected, fieldnames=reader.fieldnames + ["rejection_reason"], lineterminator="\n")
            accepted_writer.writeheader()
            rejected_writer.writeheader()
            for row in reader:
                if row["order_id"] in order_ids:
                    accepted_writer.writerow(row)
                    accepted_count += 1
                else:
                    rejected_writer.writerow({**row, "rejection_reason": "missing_fact_order"})
                    rejected_count += 1
        accepted_reviews_path.replace(reviews_path)
        row_counts["fact_reviews"] = accepted_count
        row_counts["rejected_fact_reviews_orphan_orders"] = rejected_count
        if output_directory.exists():
            shutil.rmtree(output_directory)
        temporary_directory.rename(output_directory)
    except Exception:
        shutil.rmtree(temporary_directory, ignore_errors=True)
        raise
    return {"transform_version": TRANSFORM_VERSION, "dataset_version_id": dataset_version_id, "tables": row_counts}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--dataset-version-id", required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--verify-source", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.verify_source:
        if args.manifest is None:
            raise SystemExit("--manifest is required with --verify-source")
        checksums = verify_source(args.raw_dir, args.manifest)
    else:
        checksums = {}
    report = transform(args.raw_dir, args.output_dir, args.dataset_version_id)
    report["source_checksums"] = checksums
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TransformError as exc:
        print(f"transform failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
