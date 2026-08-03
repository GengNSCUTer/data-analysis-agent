CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.conversations (
    conversation_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    user_role TEXT NOT NULL,
    title TEXT,
    dataset_version_id TEXT NOT NULL,
    metric_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    message_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (user_role IN ('analyst', 'admin')),
    CHECK (status IN ('active', 'deleted')),
    CHECK (message_count >= 0)
);

CREATE TABLE IF NOT EXISTS app.messages (
    message_id BIGSERIAL PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES app.conversations(conversation_id) ON DELETE CASCADE,
    message_index INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    user_role TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_calls_json JSONB,
    tool_call_id TEXT,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (conversation_id, message_index),
    CHECK (user_role IN ('analyst', 'admin')),
    CHECK (role IN ('user', 'assistant', 'tool', 'system')),
    CHECK (message_index >= 0)
);

CREATE TABLE IF NOT EXISTS app.agent_runs (
    run_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES app.conversations(conversation_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    user_role TEXT NOT NULL,
    question TEXT NOT NULL,
    model_name TEXT NOT NULL,
    dataset_version_id TEXT NOT NULL,
    metric_version TEXT NOT NULL,
    max_tool_iterations INTEGER NOT NULL,
    max_tool_calls INTEGER NOT NULL,
    max_sql_calls INTEGER NOT NULL,
    max_visualization_calls INTEGER NOT NULL,
    max_input_chars INTEGER NOT NULL,
    max_output_tokens INTEGER,
    tool_calls_used INTEGER NOT NULL DEFAULT 0,
    sql_calls_used INTEGER NOT NULL DEFAULT 0,
    visualization_calls_used INTEGER NOT NULL DEFAULT 0,
    llm_rounds_used INTEGER NOT NULL DEFAULT 0,
    input_chars INTEGER NOT NULL DEFAULT 0,
    context_chars INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    context_truncated BOOLEAN NOT NULL DEFAULT FALSE,
    termination_reason TEXT NOT NULL DEFAULT 'running',
    error_type TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMPTZ,
    CHECK (user_role IN ('analyst', 'admin')),
    CHECK (max_tool_iterations > 0),
    CHECK (max_tool_calls > 0),
    CHECK (max_sql_calls > 0),
    CHECK (max_visualization_calls > 0),
    CHECK (max_input_chars > 0),
    CHECK (max_output_tokens IS NULL OR max_output_tokens > 0),
    CHECK (tool_calls_used >= 0),
    CHECK (sql_calls_used >= 0),
    CHECK (visualization_calls_used >= 0),
    CHECK (llm_rounds_used >= 0),
    CHECK (input_chars >= 0),
    CHECK (context_chars >= 0),
    CHECK (termination_reason IN (
        'running', 'completed', 'clarification_required', 'tool_budget_exhausted',
        'context_truncated', 'sql_policy_rejected', 'query_timeout',
        'execution_error', 'unsupported_request', 'input_too_long'
    ))
);

CREATE TABLE IF NOT EXISTS app.query_audits (
    audit_id BIGSERIAL PRIMARY KEY,
    run_id TEXT REFERENCES app.agent_runs(run_id) ON DELETE SET NULL,
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

ALTER TABLE app.query_audits ADD COLUMN IF NOT EXISTS run_id TEXT;

CREATE INDEX IF NOT EXISTS conversations_user_updated_idx
    ON app.conversations (user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS messages_conversation_index_idx
    ON app.messages (conversation_id, message_index);
CREATE INDEX IF NOT EXISTS agent_runs_user_started_idx
    ON app.agent_runs (user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS agent_runs_conversation_started_idx
    ON app.agent_runs (conversation_id, started_at DESC);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'query_audits_run_id_fkey'
          AND conrelid = 'app.query_audits'::regclass
    ) THEN
        ALTER TABLE app.query_audits
            ADD CONSTRAINT query_audits_run_id_fkey
            FOREIGN KEY (run_id) REFERENCES app.agent_runs(run_id) ON DELETE SET NULL;
    END IF;
END $$;

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
