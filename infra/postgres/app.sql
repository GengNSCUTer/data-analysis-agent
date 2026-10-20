CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.conversations (
    conversation_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    user_role TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    title TEXT,
    dataset_version_id TEXT NOT NULL,
    metric_version TEXT NOT NULL,
    working_memory JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'active',
    message_count INTEGER NOT NULL DEFAULT 0,
    state_version INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (user_role IN ('analyst', 'admin')),
    CHECK (status IN ('active', 'deleted')),
    CHECK (message_count >= 0),
    CHECK (state_version >= 0)
);

ALTER TABLE app.conversations
    ADD COLUMN IF NOT EXISTS working_memory JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE app.conversations
    ADD COLUMN IF NOT EXISTS workspace_id TEXT;
ALTER TABLE app.conversations
    ADD COLUMN IF NOT EXISTS state_version INTEGER NOT NULL DEFAULT 0;
UPDATE app.conversations
SET workspace_id = 'olist-demo'
WHERE workspace_id IS NULL OR btrim(workspace_id) = '';
ALTER TABLE app.conversations
    ALTER COLUMN workspace_id DROP DEFAULT,
    ALTER COLUMN workspace_id SET NOT NULL;
DO $$
BEGIN
    ALTER TABLE app.conversations DROP CONSTRAINT IF EXISTS conversations_state_version_check;
    ALTER TABLE app.conversations
        ADD CONSTRAINT conversations_state_version_check CHECK (state_version >= 0);
END $$;

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
    catalog_trace JSONB NOT NULL DEFAULT '{}'::jsonb,
    repair_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
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
        'execution_error', 'unsupported_request', 'catalog_answered', 'input_too_long',
        'state_conflict'
    ))
);

ALTER TABLE app.agent_runs
    ADD COLUMN IF NOT EXISTS catalog_trace JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE app.agent_runs
    ADD COLUMN IF NOT EXISTS repair_evidence JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Keep existing installations in sync with the deterministic Catalog-answer
-- route.  The named constraint is recreated so this migration is safe when
-- the table was created by an earlier version of this file.
DO $$
BEGIN
    ALTER TABLE app.agent_runs DROP CONSTRAINT IF EXISTS agent_runs_termination_reason_check;
    ALTER TABLE app.agent_runs
        ADD CONSTRAINT agent_runs_termination_reason_check CHECK (termination_reason IN (
            'running', 'completed', 'clarification_required', 'tool_budget_exhausted',
            'context_truncated', 'sql_policy_rejected', 'query_timeout',
            'execution_error', 'unsupported_request', 'catalog_answered', 'input_too_long',
            'state_conflict'
        ));
END $$;

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
    repair_evidence JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (user_role IN ('analyst', 'admin')),
    CHECK (policy_status IN ('allowed', 'rejected', 'execution_error')),
    CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
    CHECK (row_count IS NULL OR row_count >= 0)
);

ALTER TABLE app.query_audits ADD COLUMN IF NOT EXISTS run_id TEXT;
ALTER TABLE app.query_audits ADD COLUMN IF NOT EXISTS repair_evidence JSONB;

CREATE INDEX IF NOT EXISTS conversations_user_updated_idx
    ON app.conversations (user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS conversations_user_workspace_updated_idx
    ON app.conversations (user_id, workspace_id, updated_at DESC);
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
CREATE TABLE IF NOT EXISTS app.user_preferences (
    organization_id TEXT NOT NULL DEFAULT 'demo',
    subject_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    memory_kind TEXT NOT NULL,
    value_text TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization_id, subject_id, workspace_id, memory_kind)
);

CREATE TABLE IF NOT EXISTS app.saved_analyses (
    saved_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL DEFAULT 'demo',
    subject_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    metric_version TEXT NOT NULL,
    artifact_id TEXT,
    summary TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app.accounts (
    subject_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL DEFAULT 'demo',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (role IN ('analyst', 'admin')),
    CHECK (subject_id LIKE 'local:%')
);

CREATE TABLE IF NOT EXISTS app.export_audits (
    export_id BIGSERIAL PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    user_role TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    csv_sha256 TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (user_role IN ('analyst', 'admin')),
    CHECK (row_count >= 0),
    CHECK (char_length(csv_sha256) = 64)
);
