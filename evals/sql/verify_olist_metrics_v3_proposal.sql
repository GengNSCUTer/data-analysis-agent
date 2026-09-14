-- Regression checks for the isolated Olist metric v3 proposal snapshot.
-- These checks validate the frozen data/formula baseline, not generated SQL
-- semantic accuracy and not a production runtime release.
DO $$
DECLARE
    actual_unique_customer_count BIGINT;
    actual_review_count BIGINT;
    actual_canceled_order_count BIGINT;
    actual_delivered_order_count BIGINT;
    actual_unavailable_order_count BIGINT;
    actual_average_items_per_order NUMERIC(16, 12);
    actual_average_item_price NUMERIC(16, 12);
    actual_approval_latency_days NUMERIC(16, 12);
    actual_carrier_handoff_days NUMERIC(16, 12);
BEGIN
    SELECT COUNT(DISTINCT customers.customer_unique_id)
    INTO actual_unique_customer_count
    FROM analytics.fact_orders AS orders
    JOIN analytics.dim_customers AS customers USING (customer_id)
    WHERE orders.order_status NOT IN ('canceled', 'unavailable')
      AND customers.customer_unique_id IS NOT NULL;

    SELECT COUNT(*)
    INTO actual_review_count
    FROM analytics.fact_reviews
    WHERE review_score BETWEEN 1 AND 5;

    SELECT COUNT(DISTINCT order_id)
    INTO actual_canceled_order_count
    FROM analytics.fact_orders
    WHERE order_status = 'canceled'
      AND order_purchase_timestamp IS NOT NULL;

    SELECT COUNT(DISTINCT order_id)
    INTO actual_delivered_order_count
    FROM analytics.fact_orders
    WHERE order_status = 'delivered'
      AND order_purchase_timestamp IS NOT NULL;

    SELECT COUNT(DISTINCT order_id)
    INTO actual_unavailable_order_count
    FROM analytics.fact_orders
    WHERE order_status = 'unavailable'
      AND order_purchase_timestamp IS NOT NULL;

    SELECT AVG(order_item_counts.item_count)
    INTO actual_average_items_per_order
    FROM (
        SELECT orders.order_id, COUNT(items.order_item_id) AS item_count
        FROM analytics.fact_orders AS orders
        JOIN analytics.fact_order_items AS items USING (order_id)
        WHERE orders.order_status NOT IN ('canceled', 'unavailable')
        GROUP BY orders.order_id
    ) AS order_item_counts;

    SELECT AVG(items.price)
    INTO actual_average_item_price
    FROM analytics.fact_orders AS orders
    JOIN analytics.fact_order_items AS items USING (order_id)
    WHERE orders.order_status NOT IN ('canceled', 'unavailable');

    SELECT AVG(EXTRACT(EPOCH FROM (order_approved_at - order_purchase_timestamp)) / 86400.0)
    INTO actual_approval_latency_days
    FROM analytics.fact_orders
    WHERE order_purchase_timestamp IS NOT NULL
      AND order_approved_at IS NOT NULL
      AND order_approved_at >= order_purchase_timestamp;

    SELECT AVG(EXTRACT(EPOCH FROM (order_delivered_carrier_date - order_purchase_timestamp)) / 86400.0)
    INTO actual_carrier_handoff_days
    FROM analytics.fact_orders
    WHERE order_purchase_timestamp IS NOT NULL
      AND order_delivered_carrier_date IS NOT NULL
      AND order_delivered_carrier_date >= order_purchase_timestamp;

    IF actual_unique_customer_count <> 94990 THEN
        RAISE EXCEPTION 'unique_customer_count drift: expected 94990, got %', actual_unique_customer_count;
    END IF;
    IF actual_review_count <> 99224 THEN
        RAISE EXCEPTION 'review_count drift: expected 99224, got %', actual_review_count;
    END IF;
    IF actual_canceled_order_count <> 625 THEN
        RAISE EXCEPTION 'canceled_order_count drift: expected 625, got %', actual_canceled_order_count;
    END IF;
    IF actual_delivered_order_count <> 96478 THEN
        RAISE EXCEPTION 'delivered_order_count drift: expected 96478, got %', actual_delivered_order_count;
    END IF;
    IF actual_unavailable_order_count <> 609 THEN
        RAISE EXCEPTION 'unavailable_order_count drift: expected 609, got %', actual_unavailable_order_count;
    END IF;
    IF ROUND(actual_average_items_per_order, 12) <> 1.141569669752 THEN
        RAISE EXCEPTION 'average_items_per_order drift: expected 1.141569669752, got %', actual_average_items_per_order;
    END IF;
    IF ROUND(actual_average_item_price, 12) <> 120.377166483796 THEN
        RAISE EXCEPTION 'average_item_price drift: expected 120.377166483796, got %', actual_average_item_price;
    END IF;
    IF ROUND(actual_approval_latency_days, 12) <> 0.434128929247 THEN
        RAISE EXCEPTION 'approval_latency_days drift: expected 0.434128929247, got %', actual_approval_latency_days;
    END IF;
    IF ROUND(actual_carrier_handoff_days, 12) <> 3.241404306346 THEN
        RAISE EXCEPTION 'carrier_handoff_days drift: expected 3.241404306346, got %', actual_carrier_handoff_days;
    END IF;
END $$;
