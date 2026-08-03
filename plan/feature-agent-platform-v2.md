---
goal: "Implement the second-round trusted agent platform foundation"
version: "1.0"
date_created: "2026-08-03"
last_updated: "2026-08-03"
owner: "GengNSCUTer/data-analysis-agent"
status: "In progress"
tags: ["feature", "architecture", "testing", "text-to-sql", "security"]
---

# Introduction

![Status: In progress](https://img.shields.io/badge/status-In%20progress-yellow)

This plan converts the second-round research into an executable implementation sequence. The
scope is the trusted Olist Vanna application in `/disk2/gengnan/data-analysis-agent`; it does not
replace Vanna's frontend or introduce a second repository. The first delivery slice makes
conversation state, request budgets, and run evidence explicit. Later slices add Text-to-SQL
clarification, execution repair, and result validation only after the foundation is measured.

## 1. Requirements & Constraints

- **REQ-001**: Persist user-scoped conversation metadata and messages in the project PostgreSQL `app` schema through a Vanna `ConversationStore` implementation.
- **REQ-002**: Preserve Vanna's `conversation_id` and `request_id` across SSE, tool context, audit rows, and run records.
- **REQ-003**: Enforce request budgets for LLM rounds, total tool calls, SQL calls, visualization calls, input length, and output tokens without silently returning an incomplete answer.
- **REQ-004**: Build model context from current request, role, versioned semantic context, and bounded conversation history; do not replay another user's messages or raw audit errors.
- **REQ-005**: Record a replayable Agent Run with model name, dataset/metric versions, budget configuration, termination reason, and token usage when the provider returns usage data; independent Prompt/Policy version columns remain a follow-up.
- **REQ-006**: Add deterministic Text-to-SQL clarification and result-safety checks only after P0 persistence and budget contracts are in place.
- **SEC-001**: Every conversation, message, run, and API response must be scoped to the resolved server-side user; client headers and client-provided role fields are never authority.
- **SEC-002**: Every SQL candidate and any repaired SQL must pass the existing `SqlPolicy` and PostgreSQL reader role before execution.
- **SEC-003**: Never persist or return API keys, database passwords, raw exception stacks, sensitive result values, or another user's conversation content.
- **SEC-004**: A budget exhaustion or context truncation event must be observable and must produce a safe user-facing completion state.
- **CON-001**: Use Python 3.12, FastAPI, Vanna 2.0.2 source already in this repository, PostgreSQL, and the native `<vanna-chat>` component.
- **CON-002**: Do not add Redis, Celery, MCP, multi-agent orchestration, arbitrary Python execution, or a second SQL dialect in this plan.
- **CON-003**: Existing `app.query_audits` remains the SQL-level fact table; the new run/message tables reference it rather than replacing it.
- **CON-004**: Local integration tests require `RUN_PROJECT_DB=1`; deterministic unit tests must remain runnable without external model credentials or PostgreSQL.
- **GUD-001**: Prefer adapters at project boundaries over edits to Vanna core; any Vanna-core change requires a separate compatibility test.
- **GUD-002**: Use explicit state and enums instead of inferring completion from rendered text or frontend events.
- **PAT-001**: Follow Arrange-Act-Assert tests with isolated fixtures and adversarial parametrization for ownership, malformed IDs, limits, and budget edges.

## 2. Implementation Steps

### Implementation Phase 1

- GOAL-001: Freeze the P0 data model, request-budget contract, and adversarial acceptance tests before changing runtime behavior.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Add `plan/feature-agent-platform-v2.md` with requirements, dependencies, affected files, tests, and risks. | ✅ | 2026-08-03 |
| TASK-002 | Add PostgreSQL DDL for `app.conversations`, `app.messages`, and `app.agent_runs`; include ownership indexes, status/termination checks, nonnegative budget checks, and writer-role grants. | ✅ | 2026-08-03 |
| TASK-003 | Define `RequestBudget`, `BudgetUsage`, and stable termination enums in `src/data_analysis_agent/budget.py`; validate positive limits and reject unknown termination states. | ✅ | 2026-08-03 |
| TASK-004 | Add unit tests for malformed identifiers, cross-user access, limit boundaries, context truncation, budget exhaustion, and redaction invariants before integration wiring. | ✅ baseline/API coverage | 2026-08-03 |

### Implementation Phase 2

- GOAL-002: Make the trusted application use PostgreSQL-backed conversation storage and expose safe history/run evidence APIs.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-005 | Implement `PostgresConversationStore` under `src/data_analysis_agent/conversation_store.py`; adapt Vanna `Conversation`/`Message` models, append only new messages, scope every read/delete by `user.id`, and use async thread offloading for psycopg2. | ✅ | 2026-08-03 |
| TASK-006 | Configure `examples/trusted_olist_web_demo.py` to inject the PostgreSQL store; deterministic unit tests use isolated adapters and the trusted demo has no implicit in-memory production fallback. | ✅ | 2026-08-03 |
| TASK-007 | Add `/api/project/conversations`, `/api/project/conversations/{id}`, and delete endpoints with server-side session resolution, bounded pagination, and safe DTOs that omit tool arguments and sensitive content. | ✅ | 2026-08-03 |
| TASK-008 | Add a project run recorder that links request, conversation, user, model/data/metric versions, budget usage, and terminal state; extend `query_audits` with `run_id` without breaking old rows. | ✅ | 2026-08-03 |
| TASK-009 | Add frontend-host state for current/new conversation and safe history loading while retaining native `<vanna-chat>` as the chat surface. | | |

### Implementation Phase 3

- GOAL-003: Enforce context and tool/output budgets at the application boundary and make termination observable.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-010 | Build a bounded context assembler that retains current request, role/version evidence, recent messages, and structured older-turn summaries; record an explicit truncation flag. | ◐ bounded filter; summaries deferred | 2026-08-03 |
| TASK-011 | Add a lifecycle/tool budget guard that counts LLM rounds, every individual tool call (including multiple calls in one model response), SQL calls, and visualization calls. | ✅ | 2026-08-03 |
| TASK-012 | Set trusted-demo `max_tokens` from configuration with a safe default; propagate request metadata and record provider usage when `LlmResponse.usage` is available. | ✅ | 2026-08-03 |
| TASK-013 | Add explicit completion/termination UI components for completed, clarification-required, budget-exhausted, policy-rejected, timeout, and execution-error states. | | |
| TASK-014 | Add adversarial tests proving a model response containing five tool calls cannot bypass the total-call budget and that no incomplete numeric answer is emitted after exhaustion. | | |

### Implementation Phase 4

- GOAL-004: Improve Text-to-SQL reliability with deterministic semantic routing, one repair attempt, and result validation.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-015 | Split `metric_context.py` into structured metric/schema catalog data plus rendered prompt sections; preserve versioned evidence and join/grain constraints. | | |
| TASK-016 | Implement a deterministic question preflight for missing time range, metric, comparison baseline, unsupported request, and forbidden scope; return a clarification state before SQL generation. | | |
| TASK-017 | Implement one repair path for classified database errors only; re-run full SQL Policy, role checks, timeout, and row limits; persist candidate/final SQL and repair reason. | | |
| TASK-018 | Add result-level checks for empty results, unexpected sensitive columns, row/column bounds, and known metric grain amplification; refuse or clarify rather than invent values. | | |
| TASK-019 | Add a 60-case online-model review manifest separated from deterministic policy/golden cases; record execution correctness, semantic correctness, clarification correctness, safety, latency, and token cost independently. | | |

### Implementation Phase 5

- GOAL-005: Verify the second-round release against adversarial, integration, browser, and CI evidence and update project records.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-020 | Run deterministic unit tests, PostgreSQL integration tests, security role tests, and browser E2E for refresh/new/history/budget states. | ◐ unit/DB/role/run linkage verified; browser history/budget E2E remains | 2026-08-03 |
| TASK-021 | Run project evaluation and new multi-turn/clarification contracts; preserve exact command output in a dated verification document. | ◐ verification document added; P1 contracts remain | 2026-08-03 |
| TASK-022 | Update `PROJECT.md`, `docs/AGENT_PLATFORM_NEXT_PLAN.md`, `docs/TEXT_TO_SQL_RESEARCH.md`, README/demo script, and the Feishu project record with verified facts and limitations. | ◐ local/Feishu records updated; README/demo follow-up remains | 2026-08-03 |
| TASK-023 | Review staged files for secrets, raw data, generated artifacts, and unsafe SQL before a Conventional Commit and `origin/main` push; verify GitHub Actions. | | |

## 3. Alternatives

- **ALT-001**: Keep Vanna's `MemoryConversationStore`; rejected because restart/reload loses multi-turn state and cannot support a credible history feature.
- **ALT-002**: Use `FileSystemConversationStore`; rejected for the trusted server because ownership, concurrent writes, retention, and database audit joins are weaker than the existing PostgreSQL boundary.
- **ALT-003**: Add Redis first for sessions and quotas; rejected because the project has one local process and PostgreSQL is already the durable source of truth.
- **ALT-004**: Add a vector database for memory immediately; rejected because the current schema and metric catalog are small and deterministic retrieval is easier to audit.
- **ALT-005**: Generate multiple SQL candidates and vote; deferred because current evidence favors one bounded self-correction pass at lower token cost.
- **ALT-006**: Modify Vanna's internal Agent loop directly; deferred because adapter/lifecycle boundaries preserve upstream compatibility and make the security behavior project-owned.

## 4. Dependencies

- **DEP-001**: Project PostgreSQL instance on `127.0.0.1:35434` with `app` writer role and existing `analytics` reader role.
- **DEP-002**: `psycopg2`, pandas, FastAPI, Pydantic, pytest, pytest-asyncio, and the repository's Vanna source.
- **DEP-003**: Existing `infra/postgres/security.sql`, `src/data_analysis_agent/postgres_runner.py`, and `app.query_audits` schema.
- **DEP-004**: Native Vanna request field `conversation_id`, `RequestContext.metadata`, and `LlmResponse.usage` compatibility.
- **DEP-005**: SiliconFlow OpenAI-compatible model for representative online review only; unit/CI tests must not require the key.

## 5. Files

- **FILE-001**: `plan/feature-agent-platform-v2.md` — executable second-round plan and acceptance contract.
- **FILE-002**: `infra/postgres/security.sql` — app conversation/message/run tables, constraints, indexes, and writer grants.
- **FILE-003**: `src/data_analysis_agent/budget.py` — request budget state and terminal reason types.
- **FILE-004**: `src/data_analysis_agent/conversation_store.py` — PostgreSQL Vanna ConversationStore adapter.
- **FILE-005**: `src/data_analysis_agent/run_recorder.py` — request run and budget evidence persistence.
- **FILE-006**: `src/data_analysis_agent/context_builder.py` — bounded, role-scoped LLM context assembly.
- **FILE-007**: `examples/trusted_olist_web_demo.py` — production-like dependency wiring and safe project routes.
- **FILE-008**: `examples/embedded_analyst_host.html` — current/new/history and budget status host behavior.
- **FILE-009**: `tests/test_budget.py`, `tests/test_conversation_store.py`, `tests/test_context_builder.py` — deterministic adversarial tests.
- **FILE-010**: `tests/test_postgres_conversation_store.py`, `tests/test_postgres_run_recorder.py`, `tests/test_trusted_routes.py` — opt-in database/API integration tests.
- **FILE-011**: `docs/verification-2026-08-03-v2.md` — release evidence and known gaps.

## 6. Testing

- **TEST-001**: `pytest tests/test_budget.py tests/test_context_builder.py tests/test_conversation_store.py` passes without PostgreSQL or model credentials.
- **TEST-002**: Property/parameterized tests cover zero/negative/over-limit budgets, multiple tool calls per model response, duplicate messages, malformed conversation IDs, and cross-user access.
- **TEST-003**: `RUN_PROJECT_DB=1 pytest tests/test_postgres_conversation_store.py tests/test_postgres_runner.py tests/test_postgres_run_recorder.py tests/test_trusted_routes.py` verifies DDL, persistence, ownership isolation, run/audit foreign-key linkage, and role grants.
- **TEST-004**: `RUN_PROJECT_DB=1 pytest tests/test_trusted_routes.py` verifies analyst/admin history scope, pagination caps, delete ownership, and safe DTO redaction.
- **TEST-005**: `RUN_VANNA_E2E=1 pytest -m integration tests/e2e/test_trusted_embedded_window.py` is extended for new conversation, restored conversation, and budget termination states.
- **TEST-006**: `python scripts/run_project_evaluation.py --database` and the demo-scenario golden evaluator remain green; no online accuracy number is reported without a saved model-review manifest.
- **TEST-007**: `git diff --check`, Python compile, staged-secret scan, and GitHub Actions pass before release.

## 7. Risks & Assumptions

- **RISK-001**: Persisting full tool messages can expose SQL arguments or result details; store safe summaries and enforce DTO redaction.
- **RISK-002**: Vanna may call multiple tools in one response; budget enforcement must count individual calls, not only loop iterations.
- **RISK-003**: A PostgreSQL store using blocking psycopg2 can block the event loop; all database operations must run through `asyncio.to_thread` or an equivalent pool boundary.
- **RISK-004**: A repaired SQL may be syntactically valid but semantically wrong; cap repair at one attempt and require result/metric checks plus auditable status.
- **RISK-005**: Model providers may omit token usage on streaming; represent unknown usage as null, never as zero measured cost.
- **ASSUMPTION-001**: The current local PostgreSQL instance and transformed Olist data remain available for opt-in integration tests.
- **ASSUMPTION-002**: Vanna's public `ConversationStore` and `RequestContext.metadata` interfaces remain stable for the locked repository version.
- **ASSUMPTION-003**: The native web component can carry a conversation ID through its existing SSE request path; if not, the host will expose a minimal new-session/history control without replacing the component.

## 8. Related Specifications / Further Reading

- [`docs/AGENT_PLATFORM_NEXT_PLAN.md`](../docs/AGENT_PLATFORM_NEXT_PLAN.md)
- [`docs/TEXT_TO_SQL_RESEARCH.md`](../docs/TEXT_TO_SQL_RESEARCH.md)
- [`docs/architecture/data-model.md`](../docs/architecture/data-model.md)
- [`docs/sql-policy.md`](../docs/sql-policy.md)
- [`docs/first-round-acceptance.md`](../docs/first-round-acceptance.md)
- [`src/vanna/core/storage/base.py`](../src/vanna/core/storage/base.py)
- [`src/vanna/core/agent/config.py`](../src/vanna/core/agent/config.py)
