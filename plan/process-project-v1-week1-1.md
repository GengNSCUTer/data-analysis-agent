---
goal: Project v1 Week 1 — Establish a Reviewed Baseline and Prioritized Gap Map
version: 1.0
date_created: 2026-09-18
last_updated: 2026-09-18
owner: gengnan
status: Completed
tags: [process, architecture, agent, post-training, review]
---

# Introduction

![Status: Completed](https://img.shields.io/badge/status-Completed-brightgreen)

This plan uses the first week to recover an accurate, reviewable understanding
of the current Data Analysis Agent before new training or major feature work.
The deliverable is a single project-v1 baseline: one runtime path, one primary
local model experiment, one evaluation interpretation, and a prioritized map
of product and training gaps for weeks 2–4.

This week does not start another full SFT run, DPO run, protected-final-test
run, dataset expansion, or unrelated frontend refactor.

## 1. Requirements & Constraints

- **REQ-001**: Produce a reviewed map of the user-facing Agent path from an incoming question through routing, semantic grounding, SQL generation, governance, result rendering, audit persistence, and follow-up context.
- **REQ-002**: Explain the responsibilities and current implementation status of `QuestionRouter`, `WorkingMemory`, Semantic Catalog, `QuerySpec`, `ResultContract`, `SqlPolicy`, PostgreSQL roles, `ResultValidator`, chart contracts, and audit records using the actual code and tests.
- **REQ-003**: Freeze a project-v1 primary experiment statement around Qwen2.5-Coder-1.5B Base plus the multi-schema SQL-only SFT `adapter_best`; historical 2B, 4B, CSpider, Spider, Task B, and DPO work remain supporting experiments.
- **REQ-004**: Reconcile the primary Olist and TheLook evaluation evidence into one comparison table that separates generation, governed execution, ResultContract, and Gold denotation outcomes.
- **REQ-005**: Create a prioritized gap map for product behavior, runtime reliability, data-analysis experience, model quality, evaluation, and documentation; every gap must cite code or evidence and have a proposed next action.
- **CON-001**: Do not rewrite frozen datasets, model artifacts, adapters, checkpoints, protected final tests, or existing experiment evidence during the review week.
- **CON-002**: Protected Olist and TheLook final tests remain excluded from training, DPO sources, few-shot prompts, synthetic seeds, and checkpoint choice.
- **CON-003**: Keep current worktree changes intact. Do not reset, revert, delete, or reformat unrelated files while reviewing.
- **CON-004**: Keep review sessions small and code-first: inspect one execution unit at a time, read its focused tests, run proportionate checks, record verified behavior and open questions, then continue.
- **SEC-001**: Preserve the runtime SQL chain `unwrap_sql_completion() -> SqlPolicy -> PostgreSQL readonly role -> ResultContract/ResultValidator`; no review task authorizes bypassing it.
- **SEC-002**: Keep secrets, raw database rows, full model completions, Gold SQL, model assets, and runtime logs outside Git under `/disk2/gengnan/data-analysis-agent-data/`.
- **GUD-001**: A test, loss, or a single demonstration is evidence for only the behavior it actually covers; document gaps rather than extending conclusions.
- **GUD-002**: At the end of each day, update the baseline review note with: reviewed files, verified behavior, evidence command, unresolved questions, and the next smallest review unit.

## 2. Implementation Steps

### Implementation Phase 1 — Product and Runtime Recovery

- GOAL-001: Reconstruct the actual product request path and verify the current demo baseline before judging gaps.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Read `PROJECT.md`, `docs/README.md`, `docs/architecture/repository-map.md`, and `AGENTS.md`. Create `docs/architecture/project-v1-baseline-review.md` with a one-page map of the current production-intended path and a separate list of historical/demo-only paths. | Yes | 2026-09-18 |
| TASK-002 | Review `examples/trusted_olist_web_demo.py` from request entry through SSE response. Trace the exact collaborators it constructs and record the request state transitions in the baseline review note. | Yes | 2026-09-18 |
| TASK-003 | Review `src/data_analysis_agent/question_router.py`, `src/data_analysis_agent/query_plan.py`, and their focused tests. Record whether each of these paths is implemented and tested: database query, catalog/business explanation, clarification, non-database response, and follow-up question. | Yes | 2026-09-18 |
| TASK-004 | Run the focused router/query-plan test set and one local deterministic demo smoke that does not invoke a protected evaluation. Record its command, result, and known test boundary in the review note. | Yes | 2026-09-18 |

### Implementation Phase 2 — Semantic, Safety, and Presentation Review

- GOAL-002: Verify the modules that determine whether a model candidate is safe, meaningful, and useful to a business user.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-005 | Review `src/data_analysis_agent/metric_context.py`, `src/data_analysis_agent/semantic_catalog.py`, and `src/data_analysis_agent/olist_queryspec.py`, including `QuerySpec`, `validate_query_spec()`, and `render_gold_sql()`. Document the boundary between Catalog, QuerySpec, deterministic renderer, and natural-language interpretation. | Yes | 2026-09-18 |
| TASK-006 | Review `src/data_analysis_agent/sql_policy.py`, `src/data_analysis_agent/postgres_runner.py`, `src/data_analysis_agent/result_validator.py`, `src/vanna/tools/run_sql.py`, and their focused tests. Produce a compact “candidate SQL to answer” evidence diagram with each rejection point and the data it may expose. | Yes | 2026-09-18 |
| TASK-007 | Review the current chart/result path in `examples/trusted_olist_web_demo.py`, `src/data_analysis_agent/chart_contract.py`, `src/data_analysis_agent/visualization.py`, `src/data_analysis_agent/chat_runtime.py`, and relevant web-component assets. Inspect one scalar, one grouped, and one time-series result path. Record the current chart selection behavior, table behavior, explanation boundary, and visible UX gaps. | Yes | 2026-09-18 |
| TASK-008 | Run focused Policy, PostgreSQL runner, ResultValidator, QuerySpec, and frontend rendering tests that cover the reviewed paths. Add only missing regression tests needed to capture an already-observed defect; do not write speculative tests. | Yes | 2026-09-18 |

### Implementation Phase 3 — Conversation State and Local-Model Review

- GOAL-003: Determine which parts of multi-turn analysis and local-model use are real runtime features, which are partial scaffolding, and which need implementation in week 2.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-009 | Trace `WorkingMemory` and session/audit code from `examples/trusted_olist_web_demo.py` and the relevant `src/data_analysis_agent/` modules. Write an explicit inventory of persisted fields, in-memory fields, update triggers, expiration/clear behavior, and whether follow-up questions reuse them. | Yes | 2026-09-18 |
| TASK-009A | Based on the Day 3 code audit, freeze a design proposal that separates `TrustedDemoHost` from an embeddable product Copilot, defines the host-auth/workspace contract, and specifies artifact-backed conversation/context state. Do not rewrite UI or add vector memory in this review task. | Yes | 2026-09-18 |
| TASK-009B | Read revision-pinned public code from Codex CLI and comparable open Agent projects. Record implementation patterns, provenance, license, reusable design principles and explicit non-adoptions; do not claim this discloses hosted Codex internals or import a framework. | Yes | 2026-09-18 |
| TASK-010 | Review `src/data_analysis_agent/candidate_sql_generator.py`, `scripts/post_training/training/run_post_training_sft_smoke.py`, `src/data_analysis_agent/multi_schema_sql_only_sft.py`, and the local-model evaluation launchers. Document the current runtime integration status of the 1.5B `adapter_best`: available as offline candidate generation, available behind a runtime switch, or not yet wired into the product path. | Yes | 2026-09-18 |
| TASK-011 | Run a tokenizer/model-artifact/read-only loading preflight for the chosen 1.5B `adapter_best` without changing runtime defaults. Record model ID, adapter SHA-256, prompt version, Catalog/Policy boundary, and the exact missing integration points. | Yes | 2026-09-18 |
| TASK-012 | Review the current fallback and one-pass repair behavior in `TrustedRunSqlTool` and `SecurePostgresRunner`. Classify each failure outcome as user clarification, safe refusal, one constrained repair, online-model fallback, or unimplemented. | Yes | 2026-09-18 |

### Implementation Phase 4 — Post-training Evidence and Gap Prioritization

- GOAL-004: Turn the training history into one defensible v1 statement and turn review findings into small, ordered work for weeks 2–4.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-013 | Review `docs/post-training/data/multi-schema-ecommerce-sql-only-sft-v1.md`, `docs/post-training/experiments/log.md`, the latest Olist/TheLook safe reports, and their Gold-denotation summaries. Create `docs/post-training/experiments/project-v1-model-baseline.md` with the primary model, data split, validation-best rule, matched evaluation fields, supported claims, and unsupported claims. | Yes | 2026-09-18 |
| TASK-014 | Review the 48-sample train-only and 48-sample validation-only preference diagnostics. Record why they did not authorize DPO and define the explicit evidence threshold required before revisiting DPO. | Yes | 2026-09-18 |
| TASK-015 | Create `docs/architecture/project-v1-gap-map.md`. Score each proposed improvement by user impact, evidence strength, implementation size, and dependency. The initial categories are: intent/clarification, WorkingMemory, runtime local-model integration, fallback/repair, answer explanation, table/chart UX, evaluation harness, and documentation/demo. | Yes | 2026-09-18 |
| TASK-016 | Convert the top approved gaps into three week-level implementation cards: week 2 Agent product loop, week 3 execution-guided model improvement, and week 4 evaluation/demo/interview preparation. Use the already frozen M1--M6 sequence as the product-loop dependency chain. Each card must state inputs, outputs, exclusions, acceptance evidence, and stop condition. | Yes | 2026-09-18 |

## 3. Alternatives

- **ALT-001**: Start DPO immediately from the existing SFT train or validation release. Rejected because the completed diagnostics produced `48/48` Gold matches from train and `48/48` Gold matches from validation, leaving no model-generated A/B/C preference pairs.
- **ALT-002**: Start another broad SQL-only SFT expansion. Deferred because the review must first determine whether the product weakness is model capability, repeated renderer/program distribution, prompt grounding, runtime integration, or user-facing result handling.
- **ALT-003**: Treat the Olist in-domain result as the sole project score. Rejected because it shares the Olist workspace, Catalog, metrics, Prompt shape, and deterministic renderer family with training; TheLook remains the necessary cross-schema evidence.

## 4. Dependencies

- **DEP-001**: Existing local application environment, PostgreSQL workspaces, and the trusted Olist demo configuration must remain available for smoke-level review.
- **DEP-002**: Frozen external release `/disk2/gengnan/data-analysis-agent-data/evals/multi-schema-ecommerce-sql-only-v1-r1/` and its train/validation hashes must remain readable.
- **DEP-003**: Primary adapter `/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-multischema-sql-only-sft-v1-20260916/adapter_best` and its SHA-256 evidence must remain readable.
- **DEP-004**: Existing focused pytest suites provide the first verification layer; browser verification is only required when a reviewed UI behavior is changed.

## 5. Files

- **FILE-001**: `PROJECT.md` — source of project purpose, intended runtime architecture, historical decisions, and current known limitations.
- **FILE-002**: `docs/architecture/repository-map.md` — source of module ownership and review entry points.
- **FILE-003**: `examples/trusted_olist_web_demo.py` — current trusted web request composition and SSE handoff.
- **FILE-004**: `src/data_analysis_agent/question_router.py` — request intent and clarification routing behavior.
- **FILE-005**: `src/data_analysis_agent/query_plan.py` — grounded query/result intent representation.
- **FILE-006**: `src/data_analysis_agent/sql_policy.py`, `src/data_analysis_agent/postgres_runner.py`, and `src/data_analysis_agent/result_validator.py` — candidate governance and answerability gates.
- **FILE-007**: `src/data_analysis_agent/candidate_sql_generator.py` and `src/data_analysis_agent/multi_schema_sql_only_sft.py` — local-model candidate generation and SQL-only SFT boundary.
- **FILE-008**: `docs/post-training/data/multi-schema-ecommerce-sql-only-sft-v1.md` and `docs/post-training/experiments/log.md` — primary training/evaluation evidence.
- **FILE-009**: `docs/architecture/project-v1-baseline-review.md` — new week-1 review record.
- **FILE-010**: `docs/post-training/experiments/project-v1-model-baseline.md` — new primary-model evidence summary.
- **FILE-011**: `docs/architecture/project-v1-gap-map.md` — new evidence-based backlog and week-2 to week-4 route.

## 6. Testing

- **TEST-001**: Run the focused tests for routing, query planning, semantic/QuerySpec validation, SQL Policy, PostgreSQL runner, ResultValidator, and SQL completion unwrapping after each reviewed code boundary.
- **TEST-002**: Run one trusted-demo smoke against a known non-protected local request after Phase 1, recording the observable result path and no-claim boundary.
- **TEST-003**: Recompute and verify the frozen multi-schema train/validation SHA-256 values before reading model evidence; do not regenerate release JSONL.
- **TEST-004**: Load the selected Base and `adapter_best` in read-only inference mode and compare recorded adapter SHA-256 to the project-v1 baseline record.
- **TEST-005**: Run `git diff --check` after any week-1 documentation or focused test change.

## 7. Risks & Assumptions

- **RISK-001**: `PROJECT.md` contains historical checkpoints mixed with current facts. The new baseline review must label historical evidence and current selected evidence separately.
- **RISK-002**: Existing evaluation reports may use different frozen suites or normalization revisions. The primary-model baseline must cite exact artifact directories and avoid merging incomparable scores.
- **RISK-003**: A local-model preflight may reveal that the adapter is currently offline-only. That is a valid gap and becomes a week-2 integration task, not a reason to claim runtime local inference is finished.
- **RISK-004**: Some frontend behavior requires a browser or a configured online model. If unavailable, record the behavior as unverified rather than manufacturing an end-to-end result.
- **ASSUMPTION-001**: The selected project-v1 model remains Qwen2.5-Coder-1.5B Base plus the multi-schema SQL-only SFT `adapter_best` until the week-1 evidence review recommends otherwise.
- **ASSUMPTION-002**: The month plan treats execution-guided repair as a candidate week-3 improvement only after week-1 and week-2 review identifies a runtime-safe error taxonomy and a new non-protected challenge source.

## 8. Related Specifications / Further Reading

- [Project baseline](../PROJECT.md)
- [Repository map](../docs/architecture/repository-map.md)
- [Multi-schema SQL-only SFT contract](../docs/post-training/data/multi-schema-ecommerce-sql-only-sft-v1.md)
- [DPO small-batch contract](../docs/post-training/data/multi-schema-dpo-v1-contract.md)
- [Experiment log](../docs/post-training/experiments/log.md)
- [Post-training learning entry](../docs/post-training/README.md)
