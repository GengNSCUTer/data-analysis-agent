-- Minimum-privilege reader policy for the isolated TheLook workspace.
-- The role can read only this explicit analytics view whitelist.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'daa_thelook_reader') THEN
        CREATE ROLE daa_thelook_reader LOGIN NOINHERIT;
    END IF;
END $$;

ALTER ROLE daa_thelook_reader SET default_transaction_read_only = on;
ALTER ROLE daa_thelook_reader SET statement_timeout = '5s';
ALTER ROLE daa_thelook_reader SET search_path = analytics, pg_catalog;

REVOKE ALL ON SCHEMA thelook_raw FROM PUBLIC, daa_thelook_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA thelook_raw FROM PUBLIC, daa_thelook_reader;
REVOKE ALL ON SCHEMA analytics FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA analytics FROM PUBLIC, daa_thelook_reader;
REVOKE TEMPORARY ON DATABASE thelook_analytics FROM PUBLIC;

GRANT CONNECT ON DATABASE thelook_analytics TO daa_thelook_reader;
GRANT USAGE ON SCHEMA analytics TO daa_thelook_reader;
GRANT SELECT ON analytics.distribution_centers,
                analytics.products,
                analytics.users,
                analytics.inventory_items,
                analytics.orders,
                analytics.order_items,
                analytics.events
TO daa_thelook_reader;
