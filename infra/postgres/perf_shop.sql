-- Deterministic, synthetic performance workspace. This schema is not a
-- business dataset and is never added to a default business catalog.
-- The build script supplies :order_count and :dataset_version.

DROP SCHEMA IF EXISTS perf CASCADE;
CREATE SCHEMA perf;

CREATE TABLE perf.dataset_meta (
    dataset_version TEXT PRIMARY KEY,
    order_count BIGINT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE perf.dim_customers (
    customer_id BIGINT PRIMARY KEY,
    customer_segment TEXT NOT NULL,
    customer_state TEXT NOT NULL
);

CREATE TABLE perf.dim_products (
    product_id BIGINT PRIMARY KEY,
    category TEXT NOT NULL,
    unit_price NUMERIC(12, 2) NOT NULL
);

CREATE TABLE perf.fact_orders (
    order_id BIGINT PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES perf.dim_customers(customer_id),
    order_ts TIMESTAMPTZ NOT NULL,
    order_status TEXT NOT NULL,
    order_amount NUMERIC(14, 2) NOT NULL
);

CREATE TABLE perf.fact_order_items (
    order_id BIGINT NOT NULL REFERENCES perf.fact_orders(order_id),
    item_no SMALLINT NOT NULL,
    product_id BIGINT NOT NULL REFERENCES perf.dim_products(product_id),
    quantity SMALLINT NOT NULL,
    item_amount NUMERIC(14, 2) NOT NULL,
    PRIMARY KEY (order_id, item_no)
);

INSERT INTO perf.dim_customers (customer_id, customer_segment, customer_state)
SELECT
    customer_id,
    CASE customer_id % 4
        WHEN 0 THEN 'new'
        WHEN 1 THEN 'active'
        WHEN 2 THEN 'returning'
        ELSE 'vip'
    END,
    CASE customer_id % 8
        WHEN 0 THEN 'SP'
        WHEN 1 THEN 'RJ'
        WHEN 2 THEN 'MG'
        WHEN 3 THEN 'BA'
        WHEN 4 THEN 'RS'
        WHEN 5 THEN 'PR'
        WHEN 6 THEN 'SC'
        ELSE 'PE'
    END
FROM generate_series(1, GREATEST(10000, LEAST(:order_count, 100000))) AS customer_id;

INSERT INTO perf.dim_products (product_id, category, unit_price)
SELECT
    product_id,
    CASE product_id % 10
        WHEN 0 THEN 'electronics'
        WHEN 1 THEN 'home'
        WHEN 2 THEN 'beauty'
        WHEN 3 THEN 'sports'
        WHEN 4 THEN 'fashion'
        WHEN 5 THEN 'grocery'
        WHEN 6 THEN 'office'
        WHEN 7 THEN 'toys'
        WHEN 8 THEN 'books'
        ELSE 'garden'
    END,
    (10 + (product_id % 9900) / 100.0)::numeric(12, 2)
FROM generate_series(1, 50000) AS product_id;

INSERT INTO perf.fact_orders (order_id, customer_id, order_ts, order_status, order_amount)
SELECT
    order_id,
    ((order_id - 1) % GREATEST(10000, LEAST(:order_count, 100000))) + 1,
    TIMESTAMPTZ '2022-01-01 00:00:00' + ((order_id - 1) % 1095) * INTERVAL '1 day',
    CASE order_id % 10
        WHEN 0 THEN 'cancelled'
        WHEN 1 THEN 'pending'
        ELSE 'completed'
    END,
    (20 + ((order_id * 37) % 19800) / 100.0)::numeric(14, 2)
FROM generate_series(1, :order_count) AS order_id;

INSERT INTO perf.fact_order_items (order_id, item_no, product_id, quantity, item_amount)
SELECT
    order_id,
    item_no,
    ((order_id * 17 + item_no * 31) % 50000) + 1,
    ((order_id + item_no) % 3) + 1,
    (5 + ((order_id * 19 + item_no * 23) % 9900) / 100.0)::numeric(14, 2)
FROM generate_series(1, :order_count) AS order_id
CROSS JOIN generate_series(1, 2) AS item_no;

INSERT INTO perf.dataset_meta (dataset_version, order_count)
VALUES (:'dataset_version', :order_count);

CREATE INDEX fact_orders_customer_idx ON perf.fact_orders (customer_id);
CREATE INDEX fact_orders_time_idx ON perf.fact_orders (order_ts);
CREATE INDEX fact_orders_status_idx ON perf.fact_orders (order_status);
CREATE INDEX fact_items_product_idx ON perf.fact_order_items (product_id);

ANALYZE perf.dim_customers;
ANALYZE perf.dim_products;
ANALYZE perf.fact_orders;
ANALYZE perf.fact_order_items;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'perf_shop_reader') THEN
        CREATE ROLE perf_shop_reader NOLOGIN;
    END IF;
END $$;

GRANT CONNECT ON DATABASE perf_shop TO perf_shop_reader;
GRANT USAGE ON SCHEMA perf TO perf_shop_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA perf TO perf_shop_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA perf GRANT SELECT ON TABLES TO perf_shop_reader;
