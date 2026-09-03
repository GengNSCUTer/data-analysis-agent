---
goal: Implement the versioned Olist QuerySpec validator and deterministic PostgreSQL Gold SQL renderer
version: 1.0
date_created: 2026-09-03
last_updated: 2026-09-03
owner: Data Analysis Agent
status: 'Completed'
tags: [feature, post-training, data-contract, sql]
---

# Introduction

![Status: completed](https://img.shields.io/badge/status-completed-brightgreen)

This plan implements the already-frozen Olist QuerySpec and deterministic Gold SQL design without materializing training data or changing the online Agent runtime.

## 1. Requirements & Constraints

- **REQ-001**: QuerySpec must be immutable, versioned, JSON-canonical, and independent of natural language and SQL text.
- **REQ-002**: Validation must fail closed for version drift, unknown/duplicate metrics, unsupported shapes, invalid time contracts, attribution requirements, sensitive dimensions, and unsupported features.
- **SEC-001**: Renderer must accept only validated static identifiers and registry fragments; it must not interpolate user/model SQL or execute a database query.
- **CON-001**: Olist v2 supports at most four metrics and the frozen workspace snapshot in `olist-query-spec-v1`.
- **CON-002**: Supported shapes are scalar, customer-state grouped, item-category grouped for three item metrics, and same-time-field series.
- **CON-003**: This change must not create training JSONL, read protected holdouts, run PostgreSQL, start GPU work, or modify online routing.
- **GUD-001**: Renderer output must have stable CTE names, aliases, column order, SQL text, and evidence hashes.

## 2. Implementation Steps

### Implementation Phase 1

- GOAL-001: Implement and test the offline QuerySpec contract, metric registry, and deterministic renderer.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Add immutable QuerySpec, time/workspace models, canonical ID calculation, and fail-closed validator in `src/data_analysis_agent/olist_queryspec.py`. | Yes | 2026-09-03 |
| TASK-002 | Add the read-only ten-metric PostgreSQL expression registry and static coverage/program mapping. | Yes | 2026-09-03 |
| TASK-003 | Add renderer output/evidence types and canonical SQL generation for scalar, state, category, and same-time series shapes. | Yes | 2026-09-03 |
| TASK-004 | Add deterministic unit tests for all supported metrics/shapes and every frozen rejection reason; verify SQL parses under `SqlPolicy` without executing it. | Yes | 2026-09-03 |
| TASK-005 | Update project ledger and document implementation evidence, limitations, and next review unit. | Yes | 2026-09-03 |

## 3. Alternatives

- **ALT-001**: Parse natural language directly into QuerySpec; rejected because QuerySpec must remain a reviewed offline construction artifact.
- **ALT-002**: Let an LLM compose Gold SQL; rejected because reproducibility and metric semantics would be non-deterministic.
- **ALT-003**: Execute SQL inside the renderer; rejected because database trust, ResultContract, and reader-role checks are separate gates.

## 4. Dependencies

- **DEP-001**: Olist v2 Catalog and frozen coverage matrix.
- **DEP-002**: `sqlglot` PostgreSQL parser and the existing `SqlPolicy` parser-only compatibility check.

## 5. Files

- **FILE-001**: `src/data_analysis_agent/olist_queryspec.py` — contract, registry, validator, renderer.
- **FILE-002**: `tests/test_olist_queryspec.py` — deterministic and rejection tests.
- **FILE-003**: `PROJECT.md` — implementation evidence and boundaries.

## 6. Testing

- **TEST-001**: Validate canonical IDs, workspace/version pinning, derived result columns, time ranges, and supported shape/program combinations.
- **TEST-002**: Reject every documented unsupported feature and attribution/sensitivity condition.
- **TEST-003**: Assert metric-specific expressions, default filters, denominator logic, CTE grain separation, stable aliases/order, and repeat-render byte equality.
- **TEST-004**: Parse rendered SQL through `SqlPolicy` without executing it.

## 7. Risks & Assumptions

- **RISK-001**: Fixed SQL expressions can still contain a semantic bug; renderer tests are not a substitute for reader-role execution and human business review.
- **RISK-002**: SQL Policy parsing does not prove denotation or all possible join semantics.
- **ASSUMPTION-001**: The current Olist Catalog versions and coverage IDs remain the intended v1 snapshot during implementation.

## 8. Related Specifications / Further Reading

- `docs/post-training/data/olist-queryspec-renderer-design-v1.md`
- `docs/post-training/data/olist-domain-sft-coverage-matrix-v2.md`
- `docs/metric-contracts/olist-metrics-v2.md`
