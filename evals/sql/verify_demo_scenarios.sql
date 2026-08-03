-- Fail fast if the fixed interview-demo scenario results or semantics drift.
DO $$
DECLARE
    state_rows TEXT;
    category_rows TEXT;
    overview_row TEXT;
BEGIN
    SELECT string_agg(customer_state || ':' || paid_order_count, ',' ORDER BY rank)
    INTO state_rows
    FROM (
        SELECT c.customer_state, COUNT(DISTINCT o.order_id)::TEXT AS paid_order_count,
               ROW_NUMBER() OVER (ORDER BY COUNT(DISTINCT o.order_id) DESC, c.customer_state ASC) AS rank
        FROM analytics.fact_orders AS o
        JOIN analytics.dim_customers AS c USING (customer_id)
        WHERE o.order_status NOT IN ('canceled', 'unavailable')
        GROUP BY c.customer_state
        ORDER BY COUNT(DISTINCT o.order_id) DESC, c.customer_state ASC
        LIMIT 5
    ) AS ranked_states;
    IF state_rows <> 'SP:41127,RJ:12698,MG:11496,RS:5417,PR:4983' THEN
        RAISE EXCEPTION 'state top five drift: %', state_rows;
    END IF;

    SELECT string_agg(category || ':' || paid_order_count, ',' ORDER BY rank)
    INTO category_rows
    FROM (
        SELECT COALESCE(t.product_category_name_english, p.product_category_name, 'uncategorized') AS category,
               COUNT(DISTINCT o.order_id)::TEXT AS paid_order_count,
               ROW_NUMBER() OVER (ORDER BY COUNT(DISTINCT o.order_id) DESC, COALESCE(t.product_category_name_english, p.product_category_name, 'uncategorized') ASC) AS rank
        FROM analytics.fact_orders AS o
        JOIN analytics.fact_order_items AS i USING (order_id)
        JOIN analytics.dim_products AS p USING (product_id)
        LEFT JOIN analytics.dim_category_translation AS t USING (product_category_name)
        WHERE o.order_status NOT IN ('canceled', 'unavailable')
        GROUP BY 1
        ORDER BY COUNT(DISTINCT o.order_id) DESC, category ASC
        LIMIT 10
    ) AS ranked_categories;
    IF category_rows <> 'bed_bath_table:9399,health_beauty:8800,sports_leisure:7673,computers_accessories:6654,furniture_decor:6425,housewares:5847,watches_gifts:5604,telephony:4183,auto:3872,toys:3855' THEN
        RAISE EXCEPTION 'category top ten drift: %', category_rows;
    END IF;

    WITH gmv AS (
        SELECT SUM(i.price)::NUMERIC(14, 2) AS value
        FROM analytics.fact_order_items AS i JOIN analytics.fact_orders AS o USING (order_id)
        WHERE o.order_status NOT IN ('canceled', 'unavailable')
    ), orders AS (
        SELECT COUNT(*) AS value FROM analytics.fact_orders WHERE order_status NOT IN ('canceled', 'unavailable')
    ), delivery AS (
        SELECT ROUND((AVG(EXTRACT(EPOCH FROM (order_delivered_customer_date - order_purchase_timestamp)) / 86400.0)::NUMERIC), 6) AS value
        FROM analytics.fact_orders WHERE order_delivered_customer_date IS NOT NULL
    ), reviews AS (
        SELECT ROUND(AVG((review_score >= 4)::INT), 6) AS value FROM analytics.fact_reviews
    )
    SELECT gmv.value || ':' || orders.value || ':' || delivery.value || ':' || reviews.value
    INTO overview_row FROM gmv CROSS JOIN orders CROSS JOIN delivery CROSS JOIN reviews;
    IF overview_row <> '13494400.74:98207:12.558702:0.770680' THEN
        RAISE EXCEPTION 'metric overview drift: %', overview_row;
    END IF;
END $$;
