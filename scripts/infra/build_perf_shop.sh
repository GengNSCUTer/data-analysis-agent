#!/usr/bin/env bash
set -euo pipefail

# Build the isolated synthetic performance workspace used for result-budget
# measurements. It is deliberately separate from Olist and TheLook.

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
psql_bin="${PSQL_BIN:-/disk2/gengnan/conda_envs/pg_runtime/bin/psql}"
createdb_bin="${CREATEDB_BIN:-/disk2/gengnan/conda_envs/pg_runtime/bin/createdb}"
pg_dump_bin="${PG_DUMP_BIN:-/disk2/gengnan/conda_envs/pg_runtime/bin/pg_dump}"
postgres_host="${POSTGRES_HOST:-/tmp}"
postgres_port="${POSTGRES_PORT:-35434}"
postgres_user="${POSTGRES_USER:-postgres}"
postgres_database="${PERF_SHOP_DB:-perf_shop}"
order_count="${1:-100000}"
dataset_version="perf-shop-v1-${order_count}"
manifest_root="${PERF_SHOP_MANIFEST_ROOT:-/disk2/gengnan/data-analysis-agent-data/perf/perf-shop-v1-${order_count}}"

if [[ ! "$order_count" =~ ^[0-9]+$ ]] || (( order_count < 100000 || order_count > 1000000 )); then
  printf 'order_count must be an integer between 100000 and 1000000\n' >&2
  exit 2
fi
if [[ "$postgres_database" != "perf_shop" ]]; then
  printf 'refusing unexpected database name: %s\n' "$postgres_database" >&2
  exit 2
fi
if [[ ! -x "$psql_bin" || ! -x "$createdb_bin" || ! -x "$pg_dump_bin" ]]; then
  printf 'missing PostgreSQL client binaries\n' >&2
  exit 2
fi

database_exists="$($psql_bin -h "$postgres_host" -p "$postgres_port" -U "$postgres_user" -d postgres -Atqc "SELECT 1 FROM pg_database WHERE datname = '$postgres_database'")"
if [[ "$database_exists" == "1" && "${PERF_SHOP_ALLOW_REBUILD:-0}" != "1" ]]; then
  printf '%s\n' 'perf_shop already exists; set PERF_SHOP_ALLOW_REBUILD=1 for an intentional rebuild.' >&2
  exit 3
fi
if [[ "$database_exists" != "1" ]]; then
  "$createdb_bin" -h "$postgres_host" -p "$postgres_port" -U "$postgres_user" "$postgres_database"
fi

"$psql_bin" -h "$postgres_host" -p "$postgres_port" -U "$postgres_user" \
  -d "$postgres_database" -v ON_ERROR_STOP=1 \
  -v order_count="$order_count" -v dataset_version="$dataset_version" \
  -f "$repository_root/infra/postgres/perf_shop.sql"

mkdir -p "$manifest_root"
schema_dump="$manifest_root/schema.sql"
manifest="$manifest_root/manifest.json"
"$pg_dump_bin" -h "$postgres_host" -p "$postgres_port" -U "$postgres_user" \
  -d "$postgres_database" --schema-only --no-owner --no-privileges -n perf \
  > "$schema_dump"

counts="$($psql_bin -h "$postgres_host" -p "$postgres_port" -U "$postgres_user" \
  -d "$postgres_database" -At -F ' ' -c \
  "SELECT (SELECT count(*) FROM perf.fact_orders), (SELECT count(*) FROM perf.fact_order_items), (SELECT count(*) FROM perf.dim_customers), (SELECT count(*) FROM perf.dim_products)")"
read -r orders items customers products <<< "$counts"
schema_sha256="$(sha256sum "$schema_dump" | awk '{print $1}')"
generated_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat > "$manifest" <<JSON
{
  "workspace_id": "perf-shop",
  "database": "$postgres_database",
  "schema": "perf",
  "dataset_version": "$dataset_version",
  "generated_at_utc": "$generated_at",
  "generator": "infra/postgres/perf_shop.sql",
  "requested_order_count": $order_count,
  "row_counts": {
    "fact_orders": $orders,
    "fact_order_items": $items,
    "dim_customers": $customers,
    "dim_products": $products
  },
  "reader_role": "perf_shop_reader",
  "schema_sha256": "$schema_sha256"
}
JSON
printf 'built %s orders in %s\nmanifest: %s\n' "$orders" "$postgres_database" "$manifest"
