-- Isolated storage for the TheLook cross-schema evaluation workspace.
-- Apply only to the separate `thelook_analytics` database as an administrator.

CREATE SCHEMA IF NOT EXISTS thelook_raw AUTHORIZATION postgres;
CREATE SCHEMA IF NOT EXISTS analytics AUTHORIZATION postgres;

REVOKE ALL ON SCHEMA thelook_raw FROM PUBLIC;
REVOKE ALL ON SCHEMA analytics FROM PUBLIC;

CREATE TABLE IF NOT EXISTS thelook_raw.distribution_centers (
    id BIGINT PRIMARY KEY,
    name TEXT NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS thelook_raw.products (
    id BIGINT PRIMARY KEY,
    cost NUMERIC(18, 8) NOT NULL,
    category TEXT NOT NULL,
    name TEXT,
    brand TEXT,
    retail_price NUMERIC(18, 8) NOT NULL,
    department TEXT NOT NULL,
    sku TEXT NOT NULL,
    distribution_center_id BIGINT NOT NULL REFERENCES thelook_raw.distribution_centers(id)
);
CREATE TABLE IF NOT EXISTS thelook_raw.users (
    id BIGINT PRIMARY KEY,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    email TEXT NOT NULL,
    age INTEGER,
    gender TEXT,
    state TEXT,
    street_address TEXT,
    postal_code TEXT,
    city TEXT,
    country TEXT,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    traffic_source TEXT,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS thelook_raw.inventory_items (
    id BIGINT PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES thelook_raw.products(id),
    created_at TIMESTAMPTZ NOT NULL,
    sold_at TIMESTAMPTZ,
    cost NUMERIC(18, 8) NOT NULL,
    product_category TEXT NOT NULL,
    product_name TEXT,
    product_brand TEXT,
    product_retail_price NUMERIC(18, 8) NOT NULL,
    product_department TEXT NOT NULL,
    product_sku TEXT NOT NULL,
    product_distribution_center_id BIGINT NOT NULL REFERENCES thelook_raw.distribution_centers(id)
);
CREATE TABLE IF NOT EXISTS thelook_raw.orders (
    order_id BIGINT PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES thelook_raw.users(id),
    status TEXT NOT NULL,
    gender TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    returned_at TIMESTAMPTZ,
    shipped_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    num_of_item INTEGER NOT NULL CHECK (num_of_item >= 0)
);
CREATE TABLE IF NOT EXISTS thelook_raw.order_items (
    id BIGINT PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES thelook_raw.orders(order_id),
    user_id BIGINT NOT NULL REFERENCES thelook_raw.users(id),
    product_id BIGINT NOT NULL REFERENCES thelook_raw.products(id),
    inventory_item_id BIGINT NOT NULL REFERENCES thelook_raw.inventory_items(id),
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    shipped_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    returned_at TIMESTAMPTZ,
    sale_price NUMERIC(18, 8) NOT NULL
);
CREATE TABLE IF NOT EXISTS thelook_raw.events (
    id BIGINT PRIMARY KEY,
    -- The frozen Kaggle CSV serializes non-null user IDs as `123.0`.
    -- Keep that representation losslessly in raw storage; the vetted view
    -- casts it only after the source-integrity check confirms whole values.
    user_id NUMERIC(20, 1) CHECK (user_id IS NULL OR user_id = trunc(user_id)),
    sequence_number INTEGER NOT NULL,
    session_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    ip_address TEXT,
    city TEXT,
    state TEXT,
    postal_code TEXT,
    browser TEXT,
    traffic_source TEXT,
    uri TEXT,
    event_type TEXT
);

CREATE INDEX IF NOT EXISTS inventory_items_product_id_idx ON thelook_raw.inventory_items(product_id);
CREATE INDEX IF NOT EXISTS orders_user_id_created_at_idx ON thelook_raw.orders(user_id, created_at);
CREATE INDEX IF NOT EXISTS order_items_order_id_idx ON thelook_raw.order_items(order_id);
CREATE INDEX IF NOT EXISTS order_items_product_id_idx ON thelook_raw.order_items(product_id);
CREATE INDEX IF NOT EXISTS order_items_created_at_idx ON thelook_raw.order_items(created_at);
CREATE INDEX IF NOT EXISTS events_user_id_created_at_idx ON thelook_raw.events(user_id, created_at);
CREATE INDEX IF NOT EXISTS events_event_type_created_at_idx ON thelook_raw.events(event_type, created_at);

-- Only these vetted views are visible to the model-facing reader role.
CREATE OR REPLACE VIEW analytics.distribution_centers AS SELECT id, name, latitude, longitude FROM thelook_raw.distribution_centers;
CREATE OR REPLACE VIEW analytics.products AS SELECT id, cost, category, name, brand, retail_price, department, sku, distribution_center_id FROM thelook_raw.products;
CREATE OR REPLACE VIEW analytics.users AS SELECT id, age, gender, state, city, country, traffic_source, created_at FROM thelook_raw.users;
CREATE OR REPLACE VIEW analytics.inventory_items AS SELECT id, product_id, created_at, sold_at, cost, product_category, product_name, product_brand, product_retail_price, product_department, product_sku, product_distribution_center_id FROM thelook_raw.inventory_items;
CREATE OR REPLACE VIEW analytics.orders AS SELECT order_id, user_id, status, gender, created_at, returned_at, shipped_at, delivered_at, num_of_item FROM thelook_raw.orders;
CREATE OR REPLACE VIEW analytics.order_items AS SELECT id, order_id, user_id, product_id, inventory_item_id, status, created_at, shipped_at, delivered_at, returned_at, sale_price FROM thelook_raw.order_items;
CREATE OR REPLACE VIEW analytics.events AS SELECT id, user_id::BIGINT AS user_id, sequence_number, session_id, created_at, city, state, browser, traffic_source, uri, event_type FROM thelook_raw.events;
