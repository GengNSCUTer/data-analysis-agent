CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.query_audits (
    audit_id BIGSERIAL PRIMARY KEY,
    request_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    user_role TEXT NOT NULL,
    question TEXT,
    original_sql TEXT NOT NULL,
    final_sql TEXT,
    policy_status TEXT NOT NULL,
    policy_reason TEXT,
    model_name TEXT NOT NULL,
    dataset_version_id TEXT NOT NULL,
    metric_version TEXT NOT NULL,
    elapsed_ms INTEGER,
    row_count INTEGER,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (user_role IN ('analyst', 'admin')),
    CHECK (policy_status IN ('allowed', 'rejected', 'execution_error')),
    CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
    CHECK (row_count IS NULL OR row_count >= 0)
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'daa_analytics_reader') THEN
        CREATE ROLE daa_analytics_reader LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'daa_app_writer') THEN
        CREATE ROLE daa_app_writer LOGIN;
    END IF;
END $$;

ALTER ROLE daa_analytics_reader SET default_transaction_read_only = on;
ALTER ROLE daa_analytics_reader SET statement_timeout = '5s';
ALTER ROLE daa_analytics_reader SET search_path = analytics, pg_catalog;
ALTER ROLE daa_app_writer SET search_path = app, pg_catalog;

REVOKE ALL ON SCHEMA analytics FROM PUBLIC;
REVOKE ALL ON SCHEMA app FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA analytics FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA app FROM PUBLIC;
REVOKE TEMPORARY ON DATABASE data_analysis_agent FROM PUBLIC;

GRANT CONNECT ON DATABASE data_analysis_agent TO daa_analytics_reader, daa_app_writer;
GRANT USAGE ON SCHEMA analytics TO daa_analytics_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO daa_analytics_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT ON TABLES TO daa_analytics_reader;

GRANT USAGE ON SCHEMA app TO daa_app_writer;
GRANT INSERT, SELECT ON app.query_audits TO daa_app_writer;
GRANT USAGE, SELECT ON SEQUENCE app.query_audits_audit_id_seq TO daa_app_writer;
