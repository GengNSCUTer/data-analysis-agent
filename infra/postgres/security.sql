
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
GRANT INSERT, SELECT, UPDATE, DELETE ON app.conversations, app.messages, app.agent_runs TO daa_app_writer;
GRANT INSERT, SELECT ON app.query_audits TO daa_app_writer;
GRANT USAGE, SELECT ON SEQUENCE app.query_audits_audit_id_seq TO daa_app_writer;
GRANT USAGE, SELECT ON SEQUENCE app.messages_message_id_seq TO daa_app_writer;

GRANT INSERT, SELECT, UPDATE, DELETE ON app.user_preferences, app.saved_analyses TO daa_app_writer;

GRANT INSERT, SELECT, UPDATE, DELETE ON app.accounts TO daa_app_writer;
GRANT INSERT, SELECT ON app.export_audits TO daa_app_writer;
GRANT USAGE, SELECT ON SEQUENCE app.export_audits_export_id_seq TO daa_app_writer;
