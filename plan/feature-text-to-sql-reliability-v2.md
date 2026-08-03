---
goal: "Deliver a research-backed second round of reliable Text-to-SQL for the trusted Vanna Olist agent"
version: "2.0"
date_created: "2026-08-03"
last_updated: "2026-08-03"
owner: "GengNSCUTer/data-analysis-agent"
status: "Planned"
tags: ["feature", "text-to-sql", "semantic-layer", "evaluation", "security", "research"]
---

# Introduction

![Status: Planned](https://img.shields.io/badge/status-Planned-blue)

This is the second-round execution plan for the trusted Olist Text-to-SQL path. The current
runtime already has Vanna 2.0.2, FastAPI/SSE, PostgreSQL, a `sqlglot` policy gate, a PostgreSQL
reader role, versioned metric evidence, persistent conversations, run records, and request-level
budgets. The remaining quality gap is semantic: the model still receives a mostly fixed prompt and
can produce an executable query that answers the wrong business question.

The target flow is deliberately bounded:

```text
question + server-resolved user
  -> role-scoped Catalog retrieval
  -> answerability/clarification route
  -> one SQL candidate
  -> AST policy + reader role + timeout/row budget
  -> at most one sanitized execution repair
  -> deterministic result validation
  -> evidence-backed answer, clarification, or refusal
```

The plan records what is already present in the working tree, what is only a design artifact, and
what must be proven before the capability can be described as a resume-quality result.

## 1. Requirements & Constraints

- **REQ-001**: Preserve Vanna as the Agent, SSE, and native `<vanna-chat>` base. Do not create a
  second frontend or replace the runtime with WrenAI, LangGraph, PandasAI, or another framework.
- **REQ-002**: Replace unbounded all-table prompt injection with a versioned, server-owned Catalog
  containing aliases, columns, metrics, grains, time fields, legal joins, and role visibility.
- **REQ-003**: Make Catalog retrieval deterministic, bounded, explainable, and reproducible. The
  first version must not require a vector database or an embedding service.
- **REQ-004**: Classify a request before SQL generation as `answerable`, `missing_time`,
  `missing_metric`, `missing_comparison`, `unauthorized`, or `unsupported`.
- **REQ-005**: Allow one execution-guided SQL repair only. A repaired string is untrusted and must
  pass the same AST, object allowlist, sensitive-column, reader-role, timeout, and row-limit checks.
- **REQ-006**: Separate SQL execution from semantic result validation. Empty results, missing metric
  columns, suspicious join amplification, missing time coverage, and row-limit truncation must be
  represented as explicit states.
- **REQ-007**: Record Catalog, prompt, metric, dataset, and policy versions together with route,
  SQL candidates, repair state, validation state, budget usage, and terminal reason.
- **REQ-008**: Build a local Olist evaluation contract and an optional SiliconFlow run report that
  separates executability, semantic correctness, metric correctness, security compliance,
  clarification correctness, latency, and tool/token cost.
- **SEC-001**: Server-resolved identity and role are the only authority. User text such as
  `role=admin` or `dataset=...` cannot expand Catalog visibility or SQL permissions.
- **SEC-002**: Repair prompts may contain only sanitized error categories and necessary Catalog
  context. They must not contain credentials, raw rows, stack traces, or another user's content.
- **SEC-003**: Ambiguous, unauthorized, unsupported, budget-exhausted, or validation-failed runs
  must end without an unvalidated numeric answer.
- **CON-001**: Use Python 3.12, the existing Conda environment, FastAPI, PostgreSQL, `sqlglot`,
  pytest, and the vendored Vanna version in `/disk2/gengnan/data-analysis-agent`.
- **CON-002**: PostgreSQL is the only SQL dialect in this round. Keep analytics data and app
  metadata on separate schemas and roles.
- **CON-003**: Do not add Redis, Celery/arq, MCP, multi-agent orchestration, Best-of-N sampling,
  arbitrary Python execution, or model fine-tuning.
- **GUD-001**: Implement project behavior under `src/data_analysis_agent/`; any change under
  `src/vanna/` requires a separate compatibility test and a documented reason.
- **GUD-002**: Treat public benchmark results and 2026 arXiv preprints as research signals, not as
  measurements of this Olist/SiliconFlow system.
- **PAT-001**: Every phase must have an automated check, an explicit evidence artifact, and a
  documented limitation before it is marked complete.

## 2. Implementation Steps

### Implementation Phase 1

- **GOAL-001**: Freeze the current baseline and make the Catalog artifact loadable before changing
  the Agent path.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Run the current trusted path inventory against `examples/trusted_olist_web_demo.py`, `metric_context.py`, `sql_policy.py`, `budget.py`, `chat_runtime.py`, and `run_recorder.py`; record model, versions, limits, and evidence fields in `docs/verification-text-to-sql-v2.md`. |  |  |
| TASK-002 | Keep `data/catalog/olist_catalog.yaml` server-owned and validate its tables/columns against `SqlPolicy`, its metrics against source columns, and its joins against known tables. Quote YAML keys such as `on` so `yaml.safe_load` cannot coerce them to booleans. | ✅ | 2026-08-03 |
| TASK-003 | Add `tests/test_text_to_sql_contracts.py` for Catalog versions, route-state enums, redaction invariants, stable case IDs, and no-secret/no-raw-data evaluation output. |  |  |

### Implementation Phase 2

- **GOAL-002**: Replace the fixed Schema prompt with a role-scoped Catalog slice and an auditable
  retrieval trace.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-004 | Complete `CatalogLoader`, `CatalogRetriever`, and `CatalogContextEnhancer` in `src/data_analysis_agent/semantic_catalog.py`; enforce hard limits on tables, columns, metrics, joins, and prompt characters. | ◐ | 2026-08-03 |
| TASK-005 | Add `tests/test_semantic_catalog.py` for GMV, order-count-by-state, GMV-by-category, stable ordering, zero-match behavior, Unicode normalization, prompt-injection text, duplicate/unknown Catalog entries, and analyst/admin visibility. |  |  |
| TASK-006 | Wire one shared `DemoAgentMemory`, `CatalogRetriever`, and `CatalogContextEnhancer` into `examples/trusted_olist_web_demo.py` while retaining a short safety prompt in `metric_context.py`; do not modify Vanna core. |  |  |
| TASK-007 | Persist the selected Catalog version and retrieval trace in run evidence without storing raw user text, raw result rows, credentials, or cross-user context. |  |  |

### Implementation Phase 3

- **GOAL-003**: Make ambiguity and unsupported work explicit, and make clarified answers survive
  the next turn.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-008 | Keep `QuestionRouter` as a pure, deterministic function first; cover time, metric, comparison, unauthorized, unsupported, remembered-state, and `should_generate_sql` behavior. | ◐ | 2026-08-03 |
| TASK-009 | Define a structured working-memory object for `metric_ids`, `time_range`, dimensions, filters, comparison baseline, and previous-result summary. Store it with the conversation/run, never by scraping arbitrary assistant prose. |  |  |
| TASK-010 | Add a clarification boundary to `BudgetedChatHandler`: one actionable question, no SQL budget consumption, `termination_reason=clarification_required`, and safe persistence of the original question plus the missing field. |  |  |
| TASK-011 | Add `tests/test_question_router.py`, trusted route tests, and a browser regression for “本月销售额” followed by an explicit date range and a subsequent metric follow-up. |  |  |

### Implementation Phase 4

- **GOAL-004**: Add one execution repair and deterministic result validation without weakening the
  trust boundary.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-012 | Implement `src/data_analysis_agent/sql_repair.py` with sanitized error categories, one repair attempt, original/repaired SQL retention, and a stable repair reason. |  |  |
| TASK-013 | Re-run `SqlPolicy`, PostgreSQL reader-role execution, statement timeout, and row limits for both the original and repaired candidate; never pass raw database errors to the model. |  |  |
| TASK-014 | Implement `src/data_analysis_agent/result_validator.py` for required metric columns, empty-result semantics, time coverage, row-limit truncation, and simple join-amplification checks. Return `valid`, `needs_clarification`, or `refuse`. |  |  |
| TASK-015 | Add focused repair/validator tests and expose only concise validation evidence to the existing embedded host. A failed check must not become a confident number. |  |  |

### Implementation Phase 5

- **GOAL-005**: Prove the improvement on a reproducible local contract and a manually reviewed
  online sample, then package only supported claims.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-016 | Add `evals/cases/text_to_sql_v2.yaml` with 60 cases: metrics, join/grain traps, ambiguity, multi-turn working memory, execution/empty-result repair, and RBAC/unsupported cases. Each case stores expected state and semantic assertions, not only a SQL string. |  |  |
| TASK-017 | Add `scripts/run_text_to_sql_evaluation.py` to run deterministic Policy/PostgreSQL goldens and optionally call SiliconFlow; redact secrets and result rows and preserve model/configuration metadata. |  |  |
| TASK-018 | Run the same 20--30 representative questions before and after Catalog/route/repair changes; manually label semantic and metric correctness, and report P50/P95 latency and tool/token cost. |  |  |
| TASK-019 | Write `docs/verification-text-to-sql-v2.md`, update `PROJECT.md`, `docs/TEXT_TO_SQL_RESEARCH.md`, and `docs/AGENT_PLATFORM_NEXT_PLAN.md`, then add the iteration to Feishu and push only reviewed project files. |  |  |

## 3. Alternatives

- **ALT-001**: Keep the entire fixed `SYSTEM_PROMPT`. Rejected because it does not scale, hides
  irrelevant objects from review, and makes context-budget behavior unverifiable.
- **ALT-002**: Introduce a vector database immediately. Deferred until the lexical Catalog baseline
  is measured; the current nine-table Olist scope benefits more from deterministic behavior.
- **ALT-003**: Generate many SQL candidates and vote. Deferred because current evidence favors
  self-correction over expensive self-consistency and because a single candidate is easier to audit.
- **ALT-004**: Use PandasAI or arbitrary Python after SQL. Rejected for this round because it widens
  the execution trust boundary and is not required to improve PostgreSQL query semantics.
- **ALT-005**: Fine-tune or reinforcement-train a Text-to-SQL model. Deferred until the project has
  a stable, manually labeled error set and an objective cost/quality baseline.

## 4. Dependencies

- **DEP-001**: Existing Olist PostgreSQL instance on loopback with `analytics` tables and separate
  `daa_analytics_reader`/`daa_app_writer` roles.
- **DEP-002**: Existing Conda environment `/disk2/gengnan/conda_envs/data-analysis-agent` with
  Vanna, FastAPI, PyYAML, sqlglot, pytest, and browser-test dependencies.
- **DEP-003**: Local SiliconFlow OpenAI-compatible configuration for
  `deepseek-ai/DeepSeek-V4-Flash`; the key remains outside Git and reports.
- **DEP-004**: Existing conversation/run/audit tables and the native Vanna conversation lifecycle.
- **DEP-005**: Public research references recorded in `docs/TEXT_TO_SQL_RESEARCH.md`; GitHub
  research caches under `github-research-output/` remain ignored and are not runtime dependencies.

## 5. Files

- **FILE-001**: `data/catalog/olist_catalog.yaml` — versioned, role-scoped semantic Catalog.
- **FILE-002**: `src/data_analysis_agent/semantic_catalog.py` — loader, validation, retrieval, and
  context enhancer; current implementation is present but not yet wired into the trusted Agent.
- **FILE-003**: `src/data_analysis_agent/question_router.py` — pure answerability classifier; current
  implementation is present but not yet part of the SSE boundary.
- **FILE-004**: `examples/trusted_olist_web_demo.py` and `src/data_analysis_agent/metric_context.py`
  — runtime prompt/context integration points.
- **FILE-005**: `src/data_analysis_agent/chat_runtime.py`, `budget.py`, `run_recorder.py`, and the
  existing app migrations — route, budget, and evidence integration.
- **FILE-006**: `src/data_analysis_agent/sql_repair.py` and `result_validator.py` — bounded repair
  and result semantics checks to be added.
- **FILE-007**: `evals/cases/text_to_sql_v2.yaml`, `scripts/run_text_to_sql_evaluation.py`, and
  `tests/test_text_to_sql_contracts.py` — versioned contract and evaluator.
- **FILE-008**: `docs/TEXT_TO_SQL_RESEARCH.md`, `docs/AGENT_PLATFORM_NEXT_PLAN.md`,
  `docs/verification-text-to-sql-v2.md`, `PROJECT.md`, and the Feishu project document — research,
  decision, and evidence records.

## 6. Testing

- **TEST-001**: The Conda Python compiler and `CatalogLoader().load()` report the expected Catalog
  version, 9 tables, 4 metrics, and 7 joins.
- **TEST-002**: Catalog tests prove role filtering, deterministic ordering, bounded prompt size,
  no-match refusal, Unicode normalization, and fail-closed malformed YAML handling.
- **TEST-003**: Router tests prove every state, no SQL generation for non-answerable requests, and
  working-memory carry-over without cross-user leakage.
- **TEST-004**: Repair/validator tests prove one attempt, policy re-check, sanitized errors,
  empty-result handling, required columns, join amplification, and safe refusal.
- **TEST-005**: Deterministic Olist/PostgreSQL goldens report security compliance separately from
  semantic correctness and preserve dataset/metric/policy version equality.
- **TEST-006**: Optional SiliconFlow runs record model, prompt/Catalog version, elapsed time, tool
  calls, and token usage (or `unknown`) with manual semantic labels; no accuracy is inferred from
  SQL execution alone.
- **TEST-007**: `git diff --check` and tracked-file inspection prove no `.env`, credentials, raw
  Olist data, database dump, query-result CSV, research clone, or build artifact is committed.

## 7. Risks & Assumptions

- **RISK-001**: Lexical matching may miss Chinese synonyms or select a similarly named column.
  Mitigation: record retrieval traces, add alias-focused tests, and measure recall before trying
  embeddings.
- **RISK-002**: An executable SQL query may still have the wrong metric grain or business meaning.
  Mitigation: keep semantic/metric labels separate, add result checks, and allow refusal.
- **RISK-003**: A repaired query may be unsafe or semantically worse. Mitigation: one attempt,
  sanitized input, complete policy re-check, and audit of both candidates.
- **RISK-004**: Clarification can lose the original question if working memory is only prose.
  Mitigation: persist structured fields and test the follow-up as a multi-turn contract.
- **RISK-005**: Vanna is archived upstream according to the 2026-08-03 GitHub API response.
  Mitigation: pin the vendored version, isolate adapters, and run compatibility tests for core changes.
- **RISK-006**: 2026 papers are mostly preprints and public benchmark environments differ from Olist.
  Mitigation: cite them as direction evidence only and publish local measurements separately.
- **ASSUMPTION-001**: The current Olist tables and four draft metrics are sufficient to demonstrate
  retrieval, clarification, repair, validation, and evidence before adding another dataset.
- **ASSUMPTION-002**: SiliconFlow is OpenAI-compatible; missing provider usage is recorded as
  `unknown`, not zero.
- **ASSUMPTION-003**: The signed demo identity is suitable for local evaluation only; real
  authentication and organization-level row security remain future work.

## 8. Related Specifications / Further Reading

- [`docs/TEXT_TO_SQL_RESEARCH.md`](../docs/TEXT_TO_SQL_RESEARCH.md) — current project evidence,
  open-source comparison, latest paper notes, and skill discovery.
- [`plan/feature-text-to-sql-reliability-v1.md`](feature-text-to-sql-reliability-v1.md) — first
  frozen reliability plan; this document is the second-round refinement.
- [`docs/AGENT_PLATFORM_NEXT_PLAN.md`](../docs/AGENT_PLATFORM_NEXT_PLAN.md) — session, context,
  budget, and platform roadmap.
- [BIRD-INTERACT](https://arxiv.org/abs/2510.05318) — dynamic interaction and tool-budget benchmark.
- [ABISS](https://arxiv.org/abs/2607.23340) — ambiguity taxonomy and clarification-conditioned failure.
- [Schema retrieval](https://arxiv.org/abs/2607.13311) and
  [Schema-First Retrieval](https://arxiv.org/abs/2606.28387) — schema selection as a first-class task.
- [Database Context Compression](https://arxiv.org/abs/2606.28601) — offline schema/context reduction.
- [RBAC Text-to-SQL](https://arxiv.org/abs/2607.22115) — utility and access compliance together.
- [DataClawEval](https://arxiv.org/abs/2607.28033) — deterministic end-to-end data-agent evaluation.
- [OpenChatBI](https://github.com/zhongyu09/openchatbi) and [WrenAI](https://github.com/Canner/WrenAI)
  — state-graph/result-gate and semantic-layer references.
