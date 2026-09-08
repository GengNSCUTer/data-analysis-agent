#!/usr/bin/env bash
set -euo pipefail

# Produces a no-raw-row schema and integrity audit for the frozen TheLook
# workspace. This is evidence for Catalog design, not a business metric report.

psql_bin="${PSQL_BIN:-/disk2/gengnan/conda_envs/pg_runtime/bin/psql}"
postgres_port="${POSTGRES_PORT:-35434}"
postgres_user="${POSTGRES_USER:-postgres}"
snapshot_dir="${THELOOK_SNAPSHOT_DIR:-/disk2/gengnan/data-analysis-agent-data/datasets/thelook/kaggle-mirror-v1-20260908}"
output="$snapshot_dir/audit/schema_audit.md"

query_value() {
  "$psql_bin" -p "$postgres_port" -U "$postgres_user" -d thelook_analytics -Atqc "$1"
}

raw_tables="$(query_value "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'thelook_raw' AND table_type = 'BASE TABLE'")"
analytics_views="$(query_value "SELECT count(*) FROM information_schema.views WHERE table_schema = 'analytics'")"
event_non_integral="$(query_value "SELECT count(*) FROM thelook_raw.events WHERE user_id IS NOT NULL AND user_id <> trunc(user_id)")"
event_orphans="$(query_value "SELECT count(*) FROM thelook_raw.events e LEFT JOIN thelook_raw.users u ON e.user_id::BIGINT = u.id WHERE e.user_id IS NOT NULL AND u.id IS NULL")"
order_item_orphans="$(query_value "SELECT count(*) FROM thelook_raw.order_items oi LEFT JOIN thelook_raw.orders o ON oi.order_id = o.order_id WHERE o.order_id IS NULL")"
order_item_count_mismatches="$(query_value "WITH actual AS (SELECT order_id, count(*) AS item_count FROM thelook_raw.order_items GROUP BY order_id) SELECT count(*) FROM thelook_raw.orders o LEFT JOIN actual a USING (order_id) WHERE coalesce(a.item_count, 0) <> o.num_of_item")"
reader_relations="$(query_value "SELECT string_agg(table_name, ', ' ORDER BY table_name) FROM information_schema.role_table_grants WHERE grantee = 'daa_thelook_reader' AND table_schema = 'analytics'")"
reader_raw_grants="$(query_value "SELECT count(*) FROM information_schema.role_table_grants WHERE grantee = 'daa_thelook_reader' AND table_schema = 'thelook_raw'")"

{
  printf '# TheLook PostgreSQL Schema Audit\n\n'
  printf 'Generated: `%s`  \n' "$(date --iso-8601=seconds)"
  printf 'Database: `thelook_analytics`  \n'
  printf 'Snapshot: `thelook-kaggle-mirror-v1-20260908`\n\n'
  printf '## Scope\n\n'
  printf 'This audit contains schema and aggregate integrity evidence only. It does not export customer, email, address, IP, query, or result rows. The source is a Kaggle CSV mirror of `bigquery-public-data.thelook_ecommerce`; it is not a direct authenticated BigQuery export, and its published license field is `Unknown`.\n\n'
  printf '## Relation Boundary\n\n'
  printf '| Item | Result |\n| --- | ---: |\n'
  printf '| Private raw tables | %s |\n' "$raw_tables"
  printf '| Reader-visible analytics views | %s |\n' "$analytics_views"
  printf '| Reader raw-table grants | %s |\n' "$reader_raw_grants"
  printf '| Reader view whitelist | `%s` |\n\n' "$reader_relations"
  printf 'The raw schema retains direct identifiers for load fidelity. The reader views omit first/last name, email, street address, postal code, IP address and user coordinates.\n\n'
  printf '## Source Integrity\n\n'
  printf '| Check | Result | Required |\n| --- | ---: | ---: |\n'
  printf '| Non-integral non-null `events.user_id` values | %s | 0 |\n' "$event_non_integral"
  printf '| Non-null `events.user_id` values absent from `users` | %s | 0 |\n' "$event_orphans"
  printf '| `order_items.order_id` absent from `orders` | %s | 0 |\n' "$order_item_orphans"
  printf '| Orders where `num_of_item` differs from loaded order-item count | %s | 0 |\n\n' "$order_item_count_mismatches"
  printf 'The CSV encodes non-null `events.user_id` as decimal literals such as `35092.0`. The raw table stores this as a checked `NUMERIC(20,1)` and the reader view casts it to `BIGINT`; the first two checks prove that this conversion is lossless for this frozen snapshot.\n\n'
  printf '## Time Coverage\n\n'
  printf '| Relation | Timestamp field | Min | Max |\n| --- | --- | --- | --- |\n'
  for spec in 'users|created_at' 'orders|created_at' 'order_items|created_at' 'inventory_items|created_at' 'events|created_at'; do
    table_name="${spec%%|*}"
    column_name="${spec##*|}"
    range="$(query_value "SELECT to_char(min($column_name), 'YYYY-MM-DD'), to_char(max($column_name), 'YYYY-MM-DD') FROM thelook_raw.$table_name")"
    min_date="${range%%|*}"
    max_date="${range##*|}"
    printf '| `%s` | `%s` | %s | %s |\n' "$table_name" "$column_name" "$min_date" "$max_date"
  done
  printf '\n## Status Distribution\n\n'
  printf '| Relation | Status | Rows |\n| --- | --- | ---: |\n'
  "$psql_bin" -p "$postgres_port" -U "$postgres_user" -d thelook_analytics -At -F $'\t' \
    -c "SELECT 'orders', status, count(*) FROM thelook_raw.orders GROUP BY status UNION ALL SELECT 'order_items', status, count(*) FROM thelook_raw.order_items GROUP BY status ORDER BY 1, 2" |
  while IFS=$'\t' read -r relation status row_count; do
    printf '| `%s` | `%s` | %s |\n' "$relation" "$status" "$row_count"
  done
  printf '\n## Result\n\n'
  if [[ "$event_non_integral" == 0 && "$event_orphans" == 0 && "$order_item_orphans" == 0 && "$order_item_count_mismatches" == 0 && "$reader_raw_grants" == 0 ]]; then
    printf '**Pass.** The frozen snapshot is structurally suitable for the next, separate TheLook Catalog and QuerySpec design step. It remains a protected cross-schema evaluation workspace and must not enter Olist training or validation.\n'
  else
    printf '**Fail.** One or more source-integrity or least-privilege checks failed. Do not build Catalog, QuerySpec, or evaluation data until the source/loader is corrected.\n'
    exit 4
  fi
} > "$output"

printf 'wrote %s\n' "$output"
