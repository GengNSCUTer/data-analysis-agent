-- PostgreSQL analytics schema for the Olist showcase dataset.
-- Apply with a migration runner in Phase 3; do not execute this file as an app role.

CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS analytics.dataset_versions (
    dataset_version_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_license TEXT NOT NULL,
    source_version TEXT NOT NULL,
    archive_sha256 CHAR(64) NOT NULL,
    transform_version TEXT NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (dataset_version_id <> ''),
    CHECK (dataset_id <> ''),
    CHECK (transform_version <> '')
);

CREATE TABLE IF NOT EXISTS analytics.dim_customers (
    customer_id TEXT PRIMARY KEY,
    customer_unique_id TEXT NOT NULL,
    customer_zip_code_prefix TEXT NOT NULL,
    customer_city TEXT NOT NULL,
    customer_state CHAR(2) NOT NULL,
    dataset_version_id TEXT NOT NULL REFERENCES analytics.dataset_versions(dataset_version_id)
);

CREATE TABLE IF NOT EXISTS analytics.dim_sellers (
    seller_id TEXT PRIMARY KEY,
    seller_zip_code_prefix TEXT NOT NULL,
    seller_city TEXT NOT NULL,
    seller_state CHAR(2) NOT NULL,
    dataset_version_id TEXT NOT NULL REFERENCES analytics.dataset_versions(dataset_version_id)
);

CREATE TABLE IF NOT EXISTS analytics.dim_category_translation (
    product_category_name TEXT PRIMARY KEY,
    product_category_name_english TEXT NOT NULL,
    dataset_version_id TEXT NOT NULL REFERENCES analytics.dataset_versions(dataset_version_id)
);

CREATE TABLE IF NOT EXISTS analytics.dim_products (
    product_id TEXT PRIMARY KEY,
    product_category_name TEXT,
    product_name_length INTEGER,
    product_description_length INTEGER,
    product_photos_qty INTEGER,
    product_weight_g INTEGER,
    product_length_cm INTEGER,
    product_height_cm INTEGER,
    product_width_cm INTEGER,
    dataset_version_id TEXT NOT NULL REFERENCES analytics.dataset_versions(dataset_version_id),
    CHECK (product_name_length IS NULL OR product_name_length >= 0),
    CHECK (product_description_length IS NULL OR product_description_length >= 0),
    CHECK (product_photos_qty IS NULL OR product_photos_qty >= 0),
    CHECK (product_weight_g IS NULL OR product_weight_g >= 0)
);

CREATE TABLE IF NOT EXISTS analytics.fact_orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES analytics.dim_customers(customer_id),
    order_status TEXT NOT NULL,
    order_purchase_timestamp TIMESTAMP NOT NULL,
    order_approved_at TIMESTAMP,
    order_delivered_carrier_date TIMESTAMP,
    order_delivered_customer_date TIMESTAMP,
    order_estimated_delivery_date TIMESTAMP,
    dataset_version_id TEXT NOT NULL REFERENCES analytics.dataset_versions(dataset_version_id),
    CHECK (order_status IN ('created', 'approved', 'invoiced', 'processing', 'shipped', 'delivered', 'canceled', 'unavailable')),
    CHECK (order_approved_at IS NULL OR order_approved_at >= order_purchase_timestamp),
    CHECK (order_delivered_customer_date IS NULL OR order_delivered_customer_date >= order_purchase_timestamp)
);

-- Olist contains carrier handoff timestamps slightly earlier than purchase time.
-- Remove the over-strict constraint from databases initialized by the first draft.
ALTER TABLE analytics.fact_orders DROP CONSTRAINT IF EXISTS fact_orders_check1;

CREATE TABLE IF NOT EXISTS analytics.fact_order_items (
    order_id TEXT NOT NULL REFERENCES analytics.fact_orders(order_id),
    order_item_id INTEGER NOT NULL,
    product_id TEXT NOT NULL REFERENCES analytics.dim_products(product_id),
    seller_id TEXT NOT NULL REFERENCES analytics.dim_sellers(seller_id),
    shipping_limit_date TIMESTAMP NOT NULL,
    price NUMERIC(14, 2) NOT NULL,
    freight_value NUMERIC(14, 2) NOT NULL,
    dataset_version_id TEXT NOT NULL REFERENCES analytics.dataset_versions(dataset_version_id),
    PRIMARY KEY (order_id, order_item_id),
    CHECK (order_item_id > 0),
    CHECK (price >= 0),
    CHECK (freight_value >= 0)
);

CREATE TABLE IF NOT EXISTS analytics.fact_payments (
    order_id TEXT NOT NULL REFERENCES analytics.fact_orders(order_id),
    payment_sequential INTEGER NOT NULL,
    payment_type TEXT NOT NULL,
    payment_installments INTEGER NOT NULL,
    payment_value NUMERIC(14, 2) NOT NULL,
    dataset_version_id TEXT NOT NULL REFERENCES analytics.dataset_versions(dataset_version_id),
    PRIMARY KEY (order_id, payment_sequential),
    CHECK (payment_sequential > 0),
    CHECK (payment_installments >= 0),
    CHECK (payment_value >= 0)
);

CREATE TABLE IF NOT EXISTS analytics.fact_reviews (
    review_id TEXT NOT NULL,
    order_id TEXT NOT NULL REFERENCES analytics.fact_orders(order_id),
    review_score SMALLINT NOT NULL,
    review_creation_date TIMESTAMP NOT NULL,
    dataset_version_id TEXT NOT NULL REFERENCES analytics.dataset_versions(dataset_version_id),
    PRIMARY KEY (review_id, order_id),
    CHECK (review_score BETWEEN 1 AND 5)
);

CREATE INDEX IF NOT EXISTS fact_orders_purchase_timestamp_idx ON analytics.fact_orders (order_purchase_timestamp);
CREATE INDEX IF NOT EXISTS fact_orders_customer_id_idx ON analytics.fact_orders (customer_id);
CREATE INDEX IF NOT EXISTS fact_order_items_product_id_idx ON analytics.fact_order_items (product_id);
CREATE INDEX IF NOT EXISTS fact_order_items_seller_id_idx ON analytics.fact_order_items (seller_id);
CREATE INDEX IF NOT EXISTS fact_payments_type_idx ON analytics.fact_payments (payment_type);
CREATE INDEX IF NOT EXISTS fact_reviews_creation_date_idx ON analytics.fact_reviews (review_creation_date);
