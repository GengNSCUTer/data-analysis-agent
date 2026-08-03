-- Fail fast when the loaded Olist release or metric semantics drift.
DO $$
DECLARE
    actual_gmv NUMERIC(14, 2);
    actual_order_count BIGINT;
    actual_delivery_days NUMERIC;
    actual_positive_review_rate NUMERIC;
BEGIN
    IF (SELECT COUNT(*) FROM analytics.dim_customers) <> 99441
       OR (SELECT COUNT(*) FROM analytics.dim_sellers) <> 3095
       OR (SELECT COUNT(*) FROM analytics.dim_category_translation) <> 71
       OR (SELECT COUNT(*) FROM analytics.dim_products) <> 32951
       OR (SELECT COUNT(*) FROM analytics.fact_orders) <> 99441
       OR (SELECT COUNT(*) FROM analytics.fact_order_items) <> 112650
       OR (SELECT COUNT(*) FROM analytics.fact_payments) <> 103886
       OR (SELECT COUNT(*) FROM analytics.fact_reviews) <> 99224 THEN
        RAISE EXCEPTION 'loaded table row counts differ from the recorded Olist release';
    END IF;

    IF EXISTS (
        SELECT 1 FROM analytics.fact_order_items AS item
        LEFT JOIN analytics.fact_orders AS orders USING (order_id)
        LEFT JOIN analytics.dim_products AS product USING (product_id)
        LEFT JOIN analytics.dim_sellers AS seller USING (seller_id)
        WHERE orders.order_id IS NULL OR product.product_id IS NULL OR seller.seller_id IS NULL
    ) OR EXISTS (
        SELECT 1 FROM analytics.fact_payments AS payment
        LEFT JOIN analytics.fact_orders AS orders USING (order_id)
        WHERE orders.order_id IS NULL
    ) OR EXISTS (
        SELECT 1 FROM analytics.fact_reviews AS review
        LEFT JOIN analytics.fact_orders AS orders USING (order_id)
        WHERE orders.order_id IS NULL
    ) THEN
        RAISE EXCEPTION 'loaded analytics tables contain orphan foreign keys';
    END IF;

    SELECT SUM(item.price)::NUMERIC(14, 2)
    INTO actual_gmv
    FROM analytics.fact_order_items AS item
    JOIN analytics.fact_orders AS orders USING (order_id)
    WHERE orders.order_status NOT IN ('canceled', 'unavailable');

    SELECT COUNT(*)
    INTO actual_order_count
    FROM analytics.fact_orders
    WHERE order_status NOT IN ('canceled', 'unavailable');

    SELECT ROUND((AVG(EXTRACT(EPOCH FROM (order_delivered_customer_date - order_purchase_timestamp)) / 86400.0)::NUMERIC), 6)
    INTO actual_delivery_days
    FROM analytics.fact_orders
    WHERE order_delivered_customer_date IS NOT NULL;

    SELECT ROUND(AVG((review_score >= 4)::INT), 6)
    INTO actual_positive_review_rate
    FROM analytics.fact_reviews;

    IF actual_gmv <> 13494400.74 THEN
        RAISE EXCEPTION 'GMV drift: expected 13494400.74, got %', actual_gmv;
    END IF;
    IF actual_order_count <> 98207 THEN
        RAISE EXCEPTION 'order count drift: expected 98207, got %', actual_order_count;
    END IF;
    IF actual_delivery_days <> 12.558702 THEN
        RAISE EXCEPTION 'delivery days drift: expected 12.558702, got %', actual_delivery_days;
    END IF;
    IF actual_positive_review_rate <> 0.770680 THEN
        RAISE EXCEPTION 'review rate drift: expected 0.770680, got %', actual_positive_review_rate;
    END IF;
END $$;
