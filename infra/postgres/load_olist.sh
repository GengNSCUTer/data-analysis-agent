#!/usr/bin/env bash
set -euo pipefail

# Requires the compose service to mount ANALYTICS_DATA_DIR at /data/analytics.
compose_file="infra/postgres/compose.yaml"
compose_env="${POSTGRES_ENV_FILE:-infra/postgres/.env}"
dataset_version_id="${1:-olist-kaggle-v2-2026-08-03}"

{
  cat infra/postgres/analytics.sql
  cat <<'SQL'
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
\copy analytics.dim_customers FROM '/data/analytics/dim_customers.csv' CSV HEADER NULL ''
\copy analytics.dim_sellers FROM '/data/analytics/dim_sellers.csv' CSV HEADER NULL ''
\copy analytics.dim_category_translation FROM '/data/analytics/dim_category_translation.csv' CSV HEADER NULL ''
\copy analytics.dim_products FROM '/data/analytics/dim_products.csv' CSV HEADER NULL ''
\copy analytics.fact_orders FROM '/data/analytics/fact_orders.csv' CSV HEADER NULL ''
\copy analytics.fact_order_items FROM '/data/analytics/fact_order_items.csv' CSV HEADER NULL ''
\copy analytics.fact_payments FROM '/data/analytics/fact_payments.csv' CSV HEADER NULL ''
\copy analytics.fact_reviews FROM '/data/analytics/fact_reviews.csv' CSV HEADER NULL ''
COMMIT;
SQL
} | docker compose --env-file "$compose_env" -f "$compose_file" exec -T db psql -v ON_ERROR_STOP=1 \
  -v dataset_version_id="$dataset_version_id"
