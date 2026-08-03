-- Golden metric queries. Run only after infra/postgres/load_olist.sh succeeds.
-- Each query keeps fact-table aggregation separate to avoid fanout overcounting.

-- gmv: effective-order item price only; freight and payment_value are excluded.
SELECT SUM(item.price)::NUMERIC(14, 2) AS gmv
FROM analytics.fact_order_items AS item
JOIN analytics.fact_orders AS orders USING (order_id)
WHERE orders.order_status NOT IN ('canceled', 'unavailable');

-- paid_order_count: distinct effective orders.
SELECT COUNT(*) AS paid_order_count
FROM analytics.fact_orders
WHERE order_status NOT IN ('canceled', 'unavailable');

-- average_delivery_days: only delivered orders with both timestamps.
SELECT AVG(EXTRACT(EPOCH FROM (order_delivered_customer_date - order_purchase_timestamp)) / 86400.0)
  AS average_delivery_days
FROM analytics.fact_orders
WHERE order_delivered_customer_date IS NOT NULL;

-- positive_review_rate: review-level rate, independent of item and payment fanout.
SELECT AVG((review_score >= 4)::INT)::NUMERIC(10, 6) AS positive_review_rate
FROM analytics.fact_reviews;
