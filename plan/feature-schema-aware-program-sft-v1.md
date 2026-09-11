---
goal: Implement a schema-aware auxiliary-program SFT pipeline for the Olist SQL candidate model
version: 1.0
date_created: 2026-09-11
last_updated: 2026-09-11
owner: Data Analysis Agent
status: 'In progress'
tags: [feature, post-training, sft, schema-linking, olist]
---

# Introduction

![Status: in progress](https://img.shields.io/badge/status-in_progress-yellow)

This plan adds deterministic, training-only schema-linking supervision to the
existing Olist candidate-SQL SFT pipeline. The runtime continues to make one
SQL-generation call and retains `SqlPolicy -> reader role ->
ResultContract/ResultValidator` governance. The first completed item is the
data/interface contract in `docs/post-training/data/`.

## 1. Requirements & Constraints

- **REQ-001**: Start the new LoRA experiment from `Qwen/Qwen2.5-Coder-1.5B@df3ce67c0e24480f20468b6ef2894622d69eb73b`, not from the existing Olist adapter.
- **REQ-002**: Preserve Task A as the current production-shaped `rendered_prompt -> canonical PostgreSQL SQL + EOS` supervision.
- **REQ-003**: Add Task B as a deterministic, versioned `SchemaLinkPlan` derived from an already validated Olist `QuerySpec`.
- **REQ-004**: The plan must cover table/column bindings, fixed joins, source grain, time owner, filters, deduplication, group keys, result aliases, and final CTE merge strategy.
- **SEC-001**: TheLook v2 questions, schema, QuerySpecs, Gold SQL, candidates, errors, result rows, and aggregates must not enter Olist materialization, prompts, training, validation, checkpoint choice, or error-driven seeds.
- **SEC-002**: Plan derivation may use only validated Olist QuerySpecs, frozen Olist registries, and the Olist Catalog; it must not call an LLM, execute SQL, or inspect model completions.
- **CON-001**: The online candidate prompt and one-call SQL interface remain unchanged for Task A and production inference.
- **CON-002**: Release v2 family isolation, final-test isolation, 3,072-token no-truncation policy, and external-artifact boundary remain in force.
- **CON-003**: Task B is an auxiliary supervision event, not an extra business query, semantic family, or replacement for Gold SQL.
- **GUD-001**: Every plan must have canonical JSON, a stable `slp_` ID, source `query_spec_id`, workspace pin, registry version, and deterministic equality tests.

## 2. Implementation Steps

### Implementation Phase 1

- GOAL-001: Freeze and implement deterministic schema-aware program labels from existing Olist construction artifacts.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Create `docs/post-training/data/schema-aware-program-sft-contract-v1.md`, specifying Task A/Task B boundaries, `SchemaLinkPlan`, sources, validation, and holdout isolation. | Yes | 2026-09-11 |
| TASK-002 | Add `src/data_analysis_agent/olist_schema_link_plan.py` with immutable plan/value objects, canonical serialization, static relation/join/filter registry, `derive_schema_link_plan()`, and fail-closed validation. | Yes | 2026-09-11 |
| TASK-003 | Add `tests/test_olist_schema_link_plan.py` covering all ten metrics, scalar/state/category/time shapes, AOV grain, exact IDs, workspace drift, tampering, and prohibited direct SQL fields. | Yes | 2026-09-11 |

### Implementation Phase 2

- GOAL-002: Materialize auditable Task A / Task B train and validation records without changing the production prompt.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-004 | Add `scripts/post_training/data/materialize_olist_schema_aware_program_sft.py`; accept only admitted external Olist Release v2 train/validation artifacts; atomically write Task A/Task B records and provenance outside Git. | Yes | 2026-09-11 |
| TASK-005 | Add `scripts/post_training/data/audit_olist_schema_aware_program_sft.py` plus tests for family isolation, Task A prompt identity, pairing, provenance, plan JSON, and no-truncation accounting. | Yes | 2026-09-11 |
| TASK-006 | Materialize Olist-only data and produce a bounded stratified review report without reading TheLook. |  |  |

### Implementation Phase 3

- GOAL-003: Train and evaluate a fresh Base-derived schema-aware LoRA adapter under matching evidence gates.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-007 | Add a Qwen2.5-Coder-1.5B bf16 LoRA Trainer profile that validates Task A/Task B manifests and reports separate SQL/program validation losses. |  |  |
| TASK-008 | Run a minimal GPU smoke from original Base, then run the full frozen Olist train/validation job only after materialization/layout audit passes. |  |  |
| TASK-009 | Run matching Olist and protected TheLook v2 evaluation only after generation evidence freezes; compare with original Base and existing SQL-only Olist adapter. |  |  |

## 3. Alternatives

- **ALT-001**: Add a runtime planning call before SQL generation. Rejected because it adds a second LLM call, latency, cost, and a new inconsistency surface.
- **ALT-002**: Concatenate plan JSON and SQL in one target completion. Rejected because production expects SQL only and this weakens the output/stopping contract.
- **ALT-003**: Continue training the existing SQL-only adapter. Rejected for this first experiment because it prevents clean attribution of changes to structural supervision.
- **ALT-004**: Build examples from TheLook failures. Rejected because TheLook v2 is a protected final holdout.

## 4. Dependencies

- **DEP-001**: Validated Olist `QuerySpec`, `METRIC_SQL_REGISTRY`, renderer, Catalog snapshot, and Release v2 admitted external artifacts.
- **DEP-002**: Existing runtime Prompt materializer and `olist-candidate-sql-v1` Task A contract.
- **DEP-003**: Existing generation isolation, SQL Policy, reader role, ResultContract, and post-generation Gold-denotation evaluators.

## 5. Files

- **FILE-001**: `docs/post-training/data/schema-aware-program-sft-contract-v1.md` — frozen interface and data boundary.
- **FILE-002**: `src/data_analysis_agent/olist_schema_link_plan.py` — deterministic program derivation and validation.
- **FILE-003**: `tests/test_olist_schema_link_plan.py` — unit/regression tests.
- **FILE-004**: `scripts/post_training/data/materialize_olist_schema_aware_program_sft.py` — external Task A/Task B materializer.
- **FILE-005**: `scripts/post_training/data/audit_olist_schema_aware_program_sft.py` — external layout/split/provenance audit.
- **FILE-006**: `scripts/post_training/training/run_qwen25coder_schema_aware_sft.py` — fresh-Base training entry.
- **FILE-007**: `docs/post-training/experiments/` — post-run training and matching evidence.

## 6. Testing

- **TEST-001**: Repeated derivation of supported QuerySpecs yields byte-identical canonical JSON and `slp_` IDs.
- **TEST-002**: Every plan relation, column, Join, time owner, filter, grain, dedup rule, group key, alias, and merge strategy matches the static registry and QuerySpec.
- **TEST-003**: Reject workspace/query-spec/version drift, forged IDs, unsupported fields, SQL fragments, invalid registry IDs, cross-split pairing, and TheLook inputs.
- **TEST-004**: Task A Prompt bytes/SQL target stay identical to current Release v2; Task B has an explicit training-only Prompt and JSON-only target.
- **TEST-005**: Re-audit exact token layouts with no truncation and report Task A/Task B losses separately.
- **TEST-006**: Require matching Base/new-Adapter evidence before Olist or protected TheLook execution/denotation comparisons.

## 7. Risks & Assumptions

- **RISK-001**: A deterministic plan can faithfully reproduce an incomplete registry; renderer/reader/ResultContract and business review remain independent gates.
- **RISK-002**: Auxiliary plan targets increase training events and may trade SQL-only capacity for structure; quality needs a fresh matching adapter comparison, not a loss-only claim.
- **RISK-003**: Release v2 shares Olist Catalog and renderer across splits, so in-domain accuracy remains protocol-fit evidence rather than open-domain accuracy.
- **ASSUMPTION-001**: The frozen Olist workspace and 1.5B Base revision remain available and unchanged for the fresh adapter run.
- **ASSUMPTION-002**: The existing 3,072-token cap can be re-audited for two task layouts; no sample is silently truncated.

## 8. Related Specifications / Further Reading

- `docs/post-training/data/schema-aware-program-sft-contract-v1.md`
- `docs/post-training/data/olist-domain-sft-release-v2-contract.md`
- `docs/post-training/data/olist-queryspec-renderer-design-v1.md`
- `docs/post-training/experiments/thelook-v2-matching-v1.md`
