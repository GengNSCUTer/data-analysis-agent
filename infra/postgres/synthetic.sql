-- Self-generated demo records, no redistributed Olist data. Run only on empty DB.
INSERT INTO analytics.dataset_versions
    (dataset_version_id,dataset_id,source_url,source_license,source_version,archive_sha256,transform_version)
VALUES ('synthetic-olist-v1','synthetic-commerce','generated:synthetic.sql','CC0-1.0','v1',repeat('0',64),'synthetic-v1');
INSERT INTO analytics.dim_customers
SELECT 'customer-'||n, 'buyer-'||n, '01000', 'demo_city_'||n,
       (ARRAY['SP','RJ','MG'])[1+(n%3)], 'synthetic-olist-v1'
FROM generate_series(1,300) n;
INSERT INTO analytics.dim_sellers
SELECT 'seller-'||n,'02000','demo_seller_city_'||n,'SP','synthetic-olist-v1'
FROM generate_series(1,6) n;
INSERT INTO analytics.dim_category_translation VALUES
('cama_mesa_banho','bed_bath_table','synthetic-olist-v1'),
('esporte_lazer','sports_leisure','synthetic-olist-v1'),
('informatica_acessorios','computers_accessories','synthetic-olist-v1');
INSERT INTO analytics.dim_products
SELECT 'product-'||n,(ARRAY['cama_mesa_banho','esporte_lazer','informatica_acessorios'])[1+(n%3)],
       20,100,1,200,10,5,10,'synthetic-olist-v1'
FROM generate_series(1,30) n;
INSERT INTO analytics.fact_orders
SELECT 'order-'||n,'customer-'||(1+((n-1)%300)),
       CASE WHEN n%20=0 THEN 'canceled' ELSE 'delivered' END,
       t, t+interval '1 hour', t+interval '1 day',
       CASE WHEN n%20=0 THEN NULL ELSE t+interval '3 days 12 hours' END,
       t+interval '7 days','synthetic-olist-v1'
FROM (SELECT n, timestamp '2017-01-01'+((n-1)%365)*interval '1 day' AS t
      FROM generate_series(1,1200) n) source;
INSERT INTO analytics.fact_order_items
SELECT 'order-'||n,1,'product-'||(1+(n%30)),'seller-'||(1+(n%6)),
       timestamp '2017-01-01'+((n-1)%365)*interval '1 day'+interval '2 days',
       10+(n%100),5,'synthetic-olist-v1'
FROM generate_series(1,1200) n;
INSERT INTO analytics.fact_payments
SELECT 'order-'||n,1,CASE WHEN n%2=0 THEN 'credit_card' ELSE 'boleto' END,
       1,15+(n%100),'synthetic-olist-v1'
FROM generate_series(1,1200) n;
INSERT INTO analytics.fact_reviews
SELECT 'review-'||n,'order-'||n,1+(n%5),
       timestamp '2017-01-01'+((n-1)%365)*interval '1 day'+interval '4 days',
       'synthetic-olist-v1'
FROM generate_series(1,1200) n WHERE n%20<>0;
