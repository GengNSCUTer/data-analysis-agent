#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
psql_bin="${PSQL_BIN:-/disk2/gengnan/conda_envs/pg_runtime/bin/psql}"
postgres_port="${POSTGRES_PORT:-35434}"
postgres_user="${POSTGRES_USER:-postgres}"
postgres_database="${POSTGRES_DB:-data_analysis_agent}"
analytics_data_dir="${ANALYTICS_DATA_DIR:-/disk2/gengnan/data-analysis-agent-data/olist-v2-2026-08-03/analytics-v1}"
dataset_version_id="${1:-olist-kaggle-v2-2026-08-03}"

required_files=(
  dim_customers.csv
  dim_sellers.csv
  dim_category_translation.csv
  dim_products.csv
  fact_orders.csv
  fact_order_items.csv
  fact_payments.csv
  fact_reviews.csv
)

for filename in "${required_files[@]}"; do
  if [[ ! -f "$analytics_data_dir/$filename" ]]; then
    printf 'missing transformed data file: %s\n' "$analytics_data_dir/$filename" >&2
    exit 2
  fi
done

{
  cat "$repository_root/infra/postgres/analytics.sql"
  cat <<SQL
BEGIN;
TRUNCATE analytics.fact_reviews, analytics.fact_payments, analytics.fact_order_items,
  analytics.fact_orders, analytics.dim_products, analytics.dim_category_translation,
  analytics.dim_sellers, analytics.dim_customers, analytics.dataset_versions CASCADE;
INSERT INTO analytics.dataset_versions (
  dataset_version_id, dataset_id, source_url, source_license, source_version,
  archive_sha256, transform_version
) VALUES (
  :'dataset_version_id', 'olist_brazilian_ecommerce',
  'https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce',
  'CC BY-NC-SA 4.0', '2',
  '967e41e04fc306fe604e2a693f488995a8b41e5047418f8a5c8e4abd6deca784',
  'olist-analytics-v1'
);
\copy analytics.dim_customers FROM '$analytics_data_dir/dim_customers.csv' CSV HEADER NULL ''
\copy analytics.dim_sellers FROM '$analytics_data_dir/dim_sellers.csv' CSV HEADER NULL ''
\copy analytics.dim_category_translation FROM '$analytics_data_dir/dim_category_translation.csv' CSV HEADER NULL ''
\copy analytics.dim_products FROM '$analytics_data_dir/dim_products.csv' CSV HEADER NULL ''
\copy analytics.fact_orders FROM '$analytics_data_dir/fact_orders.csv' CSV HEADER NULL ''
\copy analytics.fact_order_items FROM '$analytics_data_dir/fact_order_items.csv' CSV HEADER NULL ''
\copy analytics.fact_payments FROM '$analytics_data_dir/fact_payments.csv' CSV HEADER NULL ''
\copy analytics.fact_reviews FROM '$analytics_data_dir/fact_reviews.csv' CSV HEADER NULL ''
COMMIT;
SQL
} | "$psql_bin" -p "$postgres_port" -U "$postgres_user" -d "$postgres_database" \
  -v ON_ERROR_STOP=1 -v dataset_version_id="$dataset_version_id"
