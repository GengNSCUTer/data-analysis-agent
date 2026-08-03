---
goal: "Make the trusted Vanna Text-to-SQL path semantic, self-checking, and measurable"
version: "1.0"
date_created: "2026-08-03"
last_updated: "2026-08-03"
owner: "GengNSCUTer/data-analysis-agent"
status: "Planned"
tags: ["feature", "text-to-sql", "semantic-layer", "evaluation", "security"]
---

# Introduction

![Status: Planned](https://img.shields.io/badge/status-Planned-blue)

This plan is the executable follow-up to `docs/TEXT_TO_SQL_RESEARCH.md`. It upgrades the current
trusted Olist path from a fixed prompt and single-candidate SQL loop into a bounded, explainable
Text-to-SQL pipeline. The plan starts from the code that is actually running today: Vanna 2.0.2,
`metric_context.SYSTEM_PROMPT`, `SecurePostgresRunner`, `SqlPolicy`, PostgreSQL reader/writer roles,
request budgets, persistent conversations, and the native `<vanna-chat>` host. It does not replace
Vanna or introduce a second frontend.

The target flow is:

```text
question + conversation state
  -> semantic Catalog retrieval
  -> deterministic answerability/ambiguity routing
  -> one SQL candidate
  -> AST policy + read-only execution
  -> at most one safe repair
  -> result-level validation
  -> evidence-backed answer, clarification, or refusal
```

This document is a future implementation plan. Only the baseline inventory task is complete when
this plan is created; all later tasks remain pending until their tests and evidence are recorded.

## 1. Requirements & Constraints

- **REQ-001**: Preserve the existing trusted Olist contract: PostgreSQL `analytics` is queried only by the reader role, `app` metadata is written only by the application role, and every SQL candidate is checked by `SqlPolicy` before execution.
- **REQ-002**: Replace the monolithic `metric_context.SYSTEM_PROMPT` input with a versioned, structured Catalog containing tables, columns, aliases, metric formulas, grains, time fields, allowed dimensions, join paths, and dataset/metric versions.
- **REQ-003**: Select a bounded Schema/metric context deterministically for each user question before SQL generation; the first implementation must use explainable alias/keyword/metric matching and must not require a vector database.
- **REQ-004**: Classify each request as answerable, clarification-required, unauthorized, unsupported, or execution-retryable before emitting a numeric conclusion.
- **REQ-005**: Support at most one execution-guided SQL repair. The repaired SQL must carry an explicit repair reason and must pass the complete policy, role, timeout, and row-limit chain again.
- **REQ-006**: Validate query results for empty results, missing expected metric columns, suspicious join multiplication, missing time coverage, and row-limit truncation before producing a confident answer.
- **REQ-007**: Persist the selected Catalog version, prompt/policy versions, routing state, candidate SQL, repaired SQL, result validation state, and terminal reason in the run/audit evidence needed for replay.
- **REQ-008**: Add a versioned single-turn and multi-turn evaluation set that separates SQL executability, business semantic correctness, metric-definition correctness, security compliance, clarification correctness, latency, and token/tool cost.
- **SEC-001**: Catalog retrieval and conversation context must be scoped by the server-resolved user and role; client-provided role, user, or dataset fields are never authority.
- **SEC-002**: Repair prompts may contain only sanitized error categories and necessary schema context; they must not expose credentials, raw database rows, stack traces, or another user's content.
- **SEC-003**: A failed validation, exhausted budget, ambiguous request, or unsafe SQL must end in a safe non-numeric state unless a validated result is available.
- **SEC-004**: Result validation must not weaken column/object allowlists, sensitive-column rules, PostgreSQL grants, statement timeout, or row limits.
- **CON-001**: Use Python 3.12, FastAPI, the vendored Vanna 2.0.2 runtime, PostgreSQL, `sqlglot`, and the existing native Web Component.
- **CON-002**: Keep PostgreSQL as the only supported dialect in this plan and keep the project in `/disk2/gengnan/data-analysis-agent`.
- **CON-003**: Do not add Redis, Celery/arq, MCP, multi-agent orchestration, Best-of-N sampling, arbitrary Python execution, or model fine-tuning in this plan.
- **CON-004**: Do not submit Olist raw files, query-result CSVs, `.env` files, API keys, or database credentials; submit only manifests, schemas, fixtures, and evaluation metadata.
- **GUD-001**: Implement project behavior at adapters and application boundaries; changes under `src/vanna/` require a separate compatibility regression test and a documented reason.
- **GUD-002**: Prefer deterministic rules and structured state for safety and routing. An LLM judge may be evaluated later but is not a prerequisite for the first implementation.
- **PAT-001**: Every phase must add focused tests before claiming completion and must record the exact command, model/configuration, and result in a verification document.

## 2. Implementation Steps

### Implementation Phase 1

- **GOAL-001**: Establish an online-model baseline and freeze the semantic contracts before changing generation behavior.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Inventory the current Text-to-SQL path in `examples/trusted_olist_web_demo.py`, `src/data_analysis_agent/metric_context.py`, `src/data_analysis_agent/sql_policy.py`, `src/data_analysis_agent/postgres_runner.py`, `src/data_analysis_agent/chat_runtime.py`, and `src/data_analysis_agent/run_recorder.py`; record current model, dataset/metric versions, budgets, policy states, and evidence fields. | ✅ | 2026-08-03 |
| TASK-002 | Add `evals/cases/text_to_sql_v1.yaml` with 60 versioned cases: 10 metric questions, 10 joins/grain traps, 10 time/definition ambiguities, 10 multi-turn conversations, 10 execution/empty-result cases, and 10 RBAC/unsupported cases. Each case must define expected state, semantic assertions, allowed clarification, and refusal expectation. |  |  |
| TASK-003 | Add `scripts/run_text_to_sql_evaluation.py` to run deterministic SQL/policy checks and optionally record SiliconFlow runs without storing secrets or raw result files. Emit separate executable, semantic, metric, security, clarification, latency, and budget fields. |  |  |
| TASK-004 | Add `tests/test_text_to_sql_contracts.py` for evaluation schema validation, stable case IDs, expected-state enums, and redaction invariants. |  |  |

### Implementation Phase 2

- **GOAL-002**: Replace the fixed all-table prompt with a versioned semantic Catalog and bounded context builder.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-005 | Create `data/catalog/olist_catalog.yaml` containing the eight exposed analytics tables, physical columns/types, Chinese and English aliases, sensitive flags, four current metric definitions, default filters, time fields, grains, allowed dimensions, legal join paths, and prohibited grain combinations. Include `catalog_version`. |  |  |
| TASK-006 | Implement `src/data_analysis_agent/semantic_catalog.py` with `CatalogLoader.load()`, schema validation, `CatalogRetriever.retrieve(question, user)`, deterministic alias/keyword scoring, role filtering, and a bounded result object containing only selected tables/columns/metrics. |  |  |
| TASK-007 | Implement `src/data_analysis_agent/context_builder.py` `TrustedContextBuilder` to combine the selected Catalog slice, current role/version evidence, and bounded conversation working memory. Preserve complete latest turns and mark `context_truncated` when older turns are summarized or removed. |  |  |
| TASK-008 | Update `examples/trusted_olist_web_demo.py` and `metric_context.py` to use the Catalog builder while retaining a compatibility fallback for the existing prompt during rollout. Persist `catalog_version`, `prompt_version`, and `policy_version` in `agent_runs` and query evidence. |  |  |
| TASK-009 | Add unit tests in `tests/test_semantic_catalog.py` and extend `tests/test_context_builder.py` to prove alias matching, role filtering, join-path selection, bounded context, version propagation, and no cross-user history leakage. |  |  |

### Implementation Phase 3

- **GOAL-003**: Route ambiguous and unsupported questions into explicit clarification or refusal states before SQL generation.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-010 | Create `src/data_analysis_agent/question_router.py` with `QuestionRouter.classify(question, catalog_slice, conversation_state)` and stable states `answerable`, `missing_time`, `missing_metric`, `missing_comparison`, `unauthorized`, and `unsupported`. Use deterministic rules first and return machine-readable missing fields. |  |  |
| TASK-011 | Add `ClarificationRequest` and `ClarificationResponse` DTOs to the trusted chat boundary. The host must display one actionable question, preserve the original request, and store the user's selected time/metric/comparison context in the conversation working memory. |  |  |
| TASK-012 | Update `BudgetedChatHandler` and run recording so clarification, unsupported, and unauthorized routes consume no SQL budget, are auditable, and never emit an unvalidated numeric answer. |  |  |
| TASK-013 | Add `tests/test_question_router.py`, route tests, and a browser regression for a missing-time question followed by a clarification answer. |  |  |

### Implementation Phase 4

- **GOAL-004**: Add one execution-guided repair and deterministic result validation without expanding the SQL trust boundary.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-014 | Implement `src/data_analysis_agent/sql_repair.py` `GuardedSqlRepair` to map sanitized error categories to one repair prompt, preserve the original candidate, and stop after one repair. Do not pass raw rows, credentials, stack traces, or unrestricted database messages to the model. |  |  |
| TASK-015 | Update `SecurePostgresRunner` integration at the application boundary so candidate and repaired SQL each pass `SqlPolicy`, the PostgreSQL reader role, `statement_timeout`, and the configured LIMIT. Record `repair_attempted`, `repair_reason`, and final policy state. |  |  |
| TASK-016 | Implement `src/data_analysis_agent/result_validator.py` `ResultValidator.validate(result, expected_semantics, sql_metadata)` with checks for empty results, required metric columns, join amplification heuristics, missing time coverage, and row-limit truncation. Return `valid`, `needs_clarification`, or `refuse` with stable reasons. |  |  |
| TASK-017 | Wire result validation into `BudgetedChatHandler` before final-answer emission and expose concise evidence fields to the existing host without exposing sensitive values or raw exceptions. |  |  |
| TASK-018 | Add tests in `tests/test_sql_repair.py`, `tests/test_result_validator.py`, `tests/test_postgres_runner.py`, and `tests/test_trusted_routes.py` for repair limits, policy re-checks, empty results, join multiplication, and safe refusal. |  |  |

### Implementation Phase 5

- **GOAL-005**: Prove the reliability changes on deterministic and SiliconFlow-backed evaluation runs and publish reproducible evidence.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-019 | Run deterministic policy/database goldens and the 60-case contract suite; require 100% security compliance and no schema/metric version drift. |  |  |
| TASK-020 | Run a documented SiliconFlow `deepseek-ai/DeepSeek-V4-Flash` baseline and post-change sample using the same questions, prompt/catalog versions, budget, and database snapshot. Manually label semantic and metric correctness; do not infer correctness from execution success. |  |  |
| TASK-021 | Add multi-turn cases such as `各州订单 -> 只看前五 -> 按品类拆开 -> 和上一个结果比较好评率`; verify working-memory carry-over, explicit resets, refresh recovery, and user isolation. |  |  |
| TASK-022 | Write `docs/verification-text-to-sql-v1.md` with commands, commit, model/configuration, per-category metrics, known failures, and residual risks; update `PROJECT.md`, `docs/TEXT_TO_SQL_RESEARCH.md`, and `docs/AGENT_PLATFORM_NEXT_PLAN.md`. |  |  |
| TASK-023 | Add the phase record to the Feishu project document, run `git diff --check`, inspect tracked files for secrets/data artifacts, create a Conventional Commit, and push `origin/main` only after the verification document is complete. |  |  |

## 3. Alternatives

- **ALT-001**: Keep the full fixed `SYSTEM_PROMPT` forever. Rejected because it increases irrelevant context, makes versioned semantic changes hard to review, and does not scale beyond the current eight-table demo.
- **ALT-002**: Add a vector database and embedding retrieval immediately. Deferred because the current Schema is small; deterministic retrieval is more explainable and gives a measurable baseline before introducing embedding drift.
- **ALT-003**: Generate multiple SQL candidates and vote. Deferred because current evidence shows cost and latency would increase before we have an online semantic baseline; a single candidate plus one bounded repair is easier to audit.
- **ALT-004**: Replace Vanna with WrenAI, LangGraph, or a new multi-agent framework. Rejected for this phase because the current project already has a working Vanna/Web Component/SSE boundary; only semantic Catalog and validation ideas are being adapted.
- **ALT-005**: Execute arbitrary Python or Pandas code after SQL. Rejected because it would widen the trust boundary and is not required to improve the current PostgreSQL Text-to-SQL bottleneck.

## 4. Dependencies

- **DEP-001**: Existing PostgreSQL 12.20 service on `127.0.0.1:35434` with loaded Olist analytics tables and `daa_analytics_reader`/`daa_app_writer` roles.
- **DEP-002**: Existing Conda environment `/disk2/gengnan/conda_envs/data-analysis-agent` with Python 3.12, Vanna, FastAPI, `sqlglot`, pytest, and Playwright dependencies.
- **DEP-003**: SiliconFlow OpenAI-compatible endpoint configured through the local `.env`; the API key must remain outside Git and evaluation reports.
- **DEP-004**: Current `app.conversations`, `app.messages`, `app.agent_runs`, and `app.query_audits` schema and the native Vanna conversation lifecycle.
- **DEP-005**: GitHub repository `GengNSCUTer/data-analysis-agent`, the single code/push location, and the Feishu project document used for iteration records.

## 5. Files

- **FILE-001**: `data/catalog/olist_catalog.yaml` — versioned tables, columns, aliases, metrics, joins, and policy metadata.
- **FILE-002**: `src/data_analysis_agent/semantic_catalog.py` — Catalog loading, validation, and deterministic retrieval.
- **FILE-003**: `src/data_analysis_agent/context_builder.py` — selected Catalog and bounded conversation context assembly.
- **FILE-004**: `src/data_analysis_agent/question_router.py` — answerability and clarification state machine.
- **FILE-005**: `src/data_analysis_agent/sql_repair.py` — one-attempt sanitized execution repair.
- **FILE-006**: `src/data_analysis_agent/result_validator.py` — deterministic result safety checks.
- **FILE-007**: `src/data_analysis_agent/chat_runtime.py`, `examples/trusted_olist_web_demo.py`, and `src/data_analysis_agent/metric_context.py` — runtime wiring and evidence versions.
- **FILE-008**: `evals/cases/text_to_sql_v1.yaml` and `scripts/run_text_to_sql_evaluation.py` — versioned evaluation contract and runner.
- **FILE-009**: `tests/test_semantic_catalog.py`, `tests/test_question_router.py`, `tests/test_sql_repair.py`, `tests/test_result_validator.py`, and related existing tests — automated acceptance coverage.
- **FILE-010**: `docs/verification-text-to-sql-v1.md`, `docs/TEXT_TO_SQL_RESEARCH.md`, `docs/AGENT_PLATFORM_NEXT_PLAN.md`, `PROJECT.md`, and the Feishu project document — evidence and decisions.

## 6. Testing

- **TEST-001**: `pytest -q tests/test_text_to_sql_contracts.py tests/test_semantic_catalog.py tests/test_question_router.py tests/test_sql_repair.py tests/test_result_validator.py` passes without PostgreSQL or model credentials.
- **TEST-002**: `RUN_PROJECT_DB=1 pytest -q tests/test_postgres_runner.py tests/test_trusted_routes.py tests/test_postgres_conversation_store.py tests/test_postgres_run_recorder.py` proves SQL policy, roles, routing, persistence, and evidence linkage.
- **TEST-003**: `RUN_PROJECT_DB=1 python scripts/run_text_to_sql_evaluation.py --deterministic` reports all security cases compliant and all dataset/metric versions equal to the checked-in manifest.
- **TEST-004**: `RUN_VANNA_E2E=1 RUN_PROJECT_DB=1 pytest -q -m integration tests/e2e/test_trusted_embedded_window.py` covers clarified multi-turn chat, history restore, new conversation, chart/table rendering, and safe terminal states.
- **TEST-005**: A manually reviewed SiliconFlow run report records SQL executability separately from semantic correctness, metric correctness, clarification correctness, latency, tool calls, and token usage; no unsupported accuracy percentage is inferred.
- **TEST-006**: `git diff --check` and a tracked-file scan prove no `.env`, API key, raw dataset, query-result CSV, build output, or temporary database file is committed.

## 7. Risks & Assumptions

- **RISK-001**: A deterministic Catalog matcher may miss Chinese synonyms or over-select similarly named fields. Mitigation: log retrieval explanations, add alias-focused cases, and only evaluate embeddings after the lexical baseline is measured.
- **RISK-002**: SQL execution success may still hide a wrong business interpretation. Mitigation: require semantic/metric human labels and result validation; never report execution rate as accuracy.
- **RISK-003**: Repair prompts can create a new unsafe candidate. Mitigation: one attempt only, sanitized error categories, full AST/role/timeout/limit re-check, and audit of both SQL strings.
- **RISK-004**: Long multi-turn history can exceed the model context window. Mitigation: latest-turn preservation, structured summaries, hard character/message budgets, and a visible truncation state.
- **RISK-005**: The vendored Vanna repository is archived upstream, so future API drift will not be supplied automatically. Mitigation: pin the local version, isolate project adapters, and run compatibility tests for every Vanna-core change.
- **ASSUMPTION-001**: The current Olist eight-table Schema and four draft metrics are sufficient to demonstrate Catalog retrieval, clarification, repair, and result validation before adding another dataset.
- **ASSUMPTION-002**: The SiliconFlow endpoint returns an OpenAI-compatible response; token usage may be absent and must be recorded as unknown rather than zero.
- **ASSUMPTION-003**: The current demo cookie identity is suitable for local evaluation only; real authentication and organization-level row security remain outside this plan.

## 8. Related Specifications / Further Reading

- [`docs/TEXT_TO_SQL_RESEARCH.md`](../docs/TEXT_TO_SQL_RESEARCH.md) — current project path, open-source comparison, papers, skills, and research-backed prioritization.
- [`docs/AGENT_PLATFORM_NEXT_PLAN.md`](../docs/AGENT_PLATFORM_NEXT_PLAN.md) — platform-level session, context, budget, and product roadmap.
- [`PROJECT.md`](../PROJECT.md) — project boundary, data policy, architecture, and iteration log.
- [WrenAI](https://github.com/Canner/WrenAI) — semantic context layer and governed analytics reference.
- [BIRD-INTERACT](https://arxiv.org/abs/2510.05318) — interactive Text-to-SQL benchmark reference.
- [Spider 2.0](https://arxiv.org/abs/2411.07763) — enterprise-scale Schema and workflow benchmark reference.
- [Finding the Right Tables and Columns](https://arxiv.org/abs/2607.13311) — Schema selection as a retrieval problem.
- [Benchmarking Text-to-SQL under RBAC](https://arxiv.org/abs/2607.22115) — utility and access compliance must be evaluated together.
