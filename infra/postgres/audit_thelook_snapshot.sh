#!/usr/bin/env bash
set -euo pipefail

# Writes aggregate reproducibility evidence for an already-loaded TheLook
# workspace. Raw rows remain outside Git.

psql_bin="${PSQL_BIN:-/disk2/gengnan/conda_envs/pg_runtime/bin/psql}"
pg_dump_bin="${PG_DUMP_BIN:-/disk2/gengnan/conda_envs/pg_runtime/bin/pg_dump}"
postgres_port="${POSTGRES_PORT:-35434}"
postgres_user="${POSTGRES_USER:-postgres}"
snapshot_dir="${THELOOK_SNAPSHOT_DIR:-/disk2/gengnan/data-analysis-agent-data/datasets/thelook/kaggle-mirror-v1-20260908}"
output_dir="$snapshot_dir/audit"
archive="$snapshot_dir/thelook-kaggle-mirror-v1.zip"
mkdir -p "$output_dir"
[[ -f "$archive" ]] || { printf 'missing archive: %s\n' "$archive" >&2; exit 2; }

"$pg_dump_bin" -p "$postgres_port" -U "$postgres_user" -d thelook_analytics \
  --schema=thelook_raw --schema=analytics --schema-only --no-owner --no-privileges \
  > "$output_dir/schema.sql"

tables=(distribution_centers events inventory_items order_items orders products users)
{
  for table_name in "${tables[@]}"; do
    row_count="$("$psql_bin" -p "$postgres_port" -U "$postgres_user" -d thelook_analytics -Atqc "SELECT count(*) FROM thelook_raw.$table_name")"
    printf '%s\t%s\n' "$table_name" "$row_count"
  done
} > "$output_dir/raw_table_row_counts.tsv"

archive_sha="$(sha256sum "$archive" | awk '{print $1}')"
schema_sha="$(sha256sum "$output_dir/schema.sql" | awk '{print $1}')"
generated_at="$(date --iso-8601=seconds)"
{
  printf '{\n'
  printf '  "manifest_version": "thelook-snapshot-audit-v1",\n'
  printf '  "generated_at": "%s",\n' "$generated_at"
  printf '  "dataset_id": "thelook_ecommerce",\n'
  printf '  "snapshot_id": "thelook-kaggle-mirror-v1-20260908",\n'
  printf '  "source": {"kind": "kaggle_mirror", "dataset_ref": "mustafakeser4/looker-ecommerce-bigquery-dataset", "source_dataset": "bigquery-public-data.thelook_ecommerce", "license": "Unknown"},\n'
  printf '  "archive_sha256": "%s",\n' "$archive_sha"
  printf '  "postgres_database": "thelook_analytics",\n'
  printf '  "schema_dump_sha256": "%s",\n' "$schema_sha"
  printf '  "raw_tables": [\n'
  first=1
  while IFS=$'\t' read -r table_name row_count; do
    if [[ "$first" == 0 ]]; then printf ',\n'; fi
    first=0
    printf '    {"name": "%s", "row_count": %s}' "$table_name" "$row_count"
  done < "$output_dir/raw_table_row_counts.tsv"
  printf '\n  ],\n'
  printf '  "raw_file_sha256": {\n'
  first=1
  while IFS=' ' read -r hash path; do
    if [[ "$first" == 0 ]]; then printf ',\n'; fi
    first=0
    printf '    "%s": "%s"' "$(basename "$path")" "$hash"
  done < <(sha256sum "$snapshot_dir"/raw/*.csv | sort -k2)
  printf '\n  }\n}\n'
} > "$output_dir/snapshot_manifest.json"
printf 'wrote %s\n' "$output_dir/snapshot_manifest.json"
