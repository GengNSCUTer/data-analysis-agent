-- Regression checks for the frozen ten-metric Olist Catalog v2 snapshot.
-- These checks validate a data/formula baseline, not model-generated SQL quality.
DO $$
DECLARE
    actual_item_count BIGINT;
    actual_average_order_value NUMERIC(14, 2);
    actual_average_review_score NUMERIC(10, 4);
    actual_on_time_delivery_rate NUMERIC(10, 6);
    actual_cancellation_rate NUMERIC(10, 6);
    actual_freight_amount NUMERIC(14, 2);
BEGIN
    SELECT COUNT(*)
    INTO actual_item_count
    FROM analytics.fact_order_items AS item
    JOIN analytics.fact_orders AS orders USING (order_id)
    WHERE orders.order_status NOT IN ('canceled', 'unavailable');

    SELECT ROUND(SUM(item.price) / NULLIF(COUNT(DISTINCT item.order_id), 0), 2)
    INTO actual_average_order_value
    FROM analytics.fact_order_items AS item
    JOIN analytics.fact_orders AS orders USING (order_id)
    WHERE orders.order_status NOT IN ('canceled', 'unavailable');

    SELECT ROUND(AVG(review_score)::NUMERIC, 4)
    INTO actual_average_review_score
    FROM analytics.fact_reviews
    WHERE review_score BETWEEN 1 AND 5;

    SELECT ROUND(
        (COUNT(*) FILTER (WHERE order_delivered_customer_date <= order_estimated_delivery_date))::NUMERIC
        / NULLIF(COUNT(*), 0),
        6
    )
    INTO actual_on_time_delivery_rate
    FROM analytics.fact_orders
    WHERE order_status = 'delivered'
      AND order_purchase_timestamp IS NOT NULL
      AND order_delivered_customer_date IS NOT NULL
      AND order_estimated_delivery_date IS NOT NULL;

    SELECT ROUND(
        (COUNT(*) FILTER (WHERE order_status = 'canceled'))::NUMERIC
        / NULLIF(COUNT(*), 0),
        6
    )
    INTO actual_cancellation_rate
    FROM analytics.fact_orders
    WHERE order_purchase_timestamp IS NOT NULL;

    SELECT SUM(item.freight_value)::NUMERIC(14, 2)
    INTO actual_freight_amount
    FROM analytics.fact_order_items AS item
    JOIN analytics.fact_orders AS orders USING (order_id)
    WHERE orders.order_status NOT IN ('canceled', 'unavailable');

    IF actual_item_count <> 112101 THEN
        RAISE EXCEPTION 'item_count drift: expected 112101, got %', actual_item_count;
    END IF;
    IF actual_average_order_value <> 137.42 THEN
        RAISE EXCEPTION 'average_order_value drift: expected 137.42, got %', actual_average_order_value;
    END IF;
    IF actual_average_review_score <> 4.0864 THEN
        RAISE EXCEPTION 'average_review_score drift: expected 4.0864, got %', actual_average_review_score;
    END IF;
    IF actual_on_time_delivery_rate <> 0.918876 THEN
        RAISE EXCEPTION 'on_time_delivery_rate drift: expected 0.918876, got %', actual_on_time_delivery_rate;
    END IF;
    IF actual_cancellation_rate <> 0.006285 THEN
        RAISE EXCEPTION 'cancellation_rate drift: expected 0.006285, got %', actual_cancellation_rate;
    END IF;
    IF actual_freight_amount <> 2241126.29 THEN
        RAISE EXCEPTION 'freight_amount drift: expected 2241126.29, got %', actual_freight_amount;
    END IF;
END $$;
