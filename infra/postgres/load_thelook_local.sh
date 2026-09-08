#!/usr/bin/env bash
set -euo pipefail

# Imports a frozen TheLook CSV snapshot into an isolated database. It never
# targets the Olist `data_analysis_agent` database. Set THELOOK_ALLOW_RELOAD=1
# only when deliberately replacing an existing TheLook workspace.

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
psql_bin="${PSQL_BIN:-/disk2/gengnan/conda_envs/pg_runtime/bin/psql}"
createdb_bin="${CREATEDB_BIN:-/disk2/gengnan/conda_envs/pg_runtime/bin/createdb}"
postgres_port="${POSTGRES_PORT:-35434}"
postgres_user="${POSTGRES_USER:-postgres}"
postgres_database="${THELOOK_POSTGRES_DB:-thelook_analytics}"
snapshot_dir="${THELOOK_SNAPSHOT_DIR:-/disk2/gengnan/data-analysis-agent-data/datasets/thelook/kaggle-mirror-v1-20260908}"
raw_dir="$snapshot_dir/raw"

if [[ "$postgres_database" != "thelook_analytics" ]]; then
  printf 'refusing unexpected database name: %s\n' "$postgres_database" >&2
  exit 2
fi

required_files=(distribution_centers.csv products.csv users.csv inventory_items.csv orders.csv order_items.csv events.csv)
for filename in "${required_files[@]}"; do
  [[ -f "$raw_dir/$filename" ]] || { printf 'missing frozen TheLook source file: %s\n' "$raw_dir/$filename" >&2; exit 2; }
done

if ! "$psql_bin" -p "$postgres_port" -U "$postgres_user" -d postgres -Atqc \
  "SELECT 1 FROM pg_database WHERE datname = '$postgres_database'" | grep -qx 1; then
  "$createdb_bin" -p "$postgres_port" -U "$postgres_user" "$postgres_database"
fi

existing_tables="$("$psql_bin" -p "$postgres_port" -U "$postgres_user" -d "$postgres_database" -Atqc "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'thelook_raw'")"
if [[ "$existing_tables" != "0" && "${THELOOK_ALLOW_RELOAD:-0}" != "1" ]]; then
  printf '%s\n' 'TheLook workspace already has raw tables; set THELOOK_ALLOW_RELOAD=1 for an intentional reload.' >&2
  exit 3
fi
if [[ "${THELOOK_ALLOW_RELOAD:-0}" == "1" ]]; then
  "$psql_bin" -p "$postgres_port" -U "$postgres_user" -d "$postgres_database" -v ON_ERROR_STOP=1 <<'SQL'
DROP SCHEMA IF EXISTS analytics CASCADE;
DROP SCHEMA IF EXISTS thelook_raw CASCADE;
SQL
fi

"$psql_bin" -p "$postgres_port" -U "$postgres_user" -d "$postgres_database" -v ON_ERROR_STOP=1 -f "$repository_root/infra/postgres/thelook_schema.sql"
"$psql_bin" -p "$postgres_port" -U "$postgres_user" -d "$postgres_database" -v ON_ERROR_STOP=1 <<SQL
BEGIN;
\\copy thelook_raw.distribution_centers FROM '$raw_dir/distribution_centers.csv' CSV HEADER NULL ''
\\copy thelook_raw.products FROM '$raw_dir/products.csv' CSV HEADER NULL ''
\\copy thelook_raw.users FROM '$raw_dir/users.csv' CSV HEADER NULL ''
\\copy thelook_raw.inventory_items FROM '$raw_dir/inventory_items.csv' CSV HEADER NULL ''
\\copy thelook_raw.orders FROM '$raw_dir/orders.csv' CSV HEADER NULL ''
\\copy thelook_raw.order_items FROM '$raw_dir/order_items.csv' CSV HEADER NULL ''
\\copy thelook_raw.events FROM '$raw_dir/events.csv' CSV HEADER NULL ''
COMMIT;
SQL
"$psql_bin" -p "$postgres_port" -U "$postgres_user" -d "$postgres_database" -v ON_ERROR_STOP=1 -f "$repository_root/infra/postgres/thelook_security.sql"
