---
goal: "Complete semantic query quality and runtime acceptance phases"
version: "1.0"
date_created: "2026-10-14"
last_updated: "2026-10-14"
owner: "GengNSCUTer/data-analysis-agent"
status: "Completed"
tags: ["feature", "semantic-intent", "runtime", "evaluation"]
---

# Introduction

![Status: Completed](https://img.shields.io/badge/status-Completed-brightgreen)

This plan continues the online `SemanticIntent` work after Phase 0/1. It focuses on
two observed failures: a state-level request acquiring an unintended city grouping,
and a customer-state filter being treated as an unresolved seller/customer choice.
It measures structured scope separately from JSON validity and validates the same
scope through the authenticated PostgreSQL SSE path.

## 1. Requirements & Constraints

- **REQ-001**: Natural-language routing uses structured intent in the default online path; old lexical routing remains only for frozen offline compatibility.
- **REQ-002**: A dimension is a grouping only when the user requests that level. A filter never becomes a grouping.
- **REQ-003**: Default geography is customer geography unless the user explicitly says seller.
- **SEC-001**: Semantic intent cannot grant identity, workspace, table, column, or write permission.
- **CON-001**: Existing SQL Policy, reader role, ResultContract, ResultArtifact and result budgets remain unchanged.
- **CON-002**: This phase does not train a model, change the default model, or evaluate protected final tests.
- **GUD-001**: Provider errors are recorded as interpretation failures; they are not semantic passes.

## 2. Implementation Steps

### Implementation Phase 2 — Semantic quality loop

- **GOAL-001**: Make scope errors reproducible and correct the two observed interpretations.

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-001 | Add scoped few-shot examples, typed field descriptions, and regression cases for state-only, state filter, explicit city, and explicit seller requests. | ✅ | 2026-10-14 |
| TASK-002 | Add semantic expectation scoring so parsed JSON and intended dimensions/filters are reported separately. | ✅ | 2026-10-14 |
| TASK-003 | Run real authenticated scope probes through QueryPlan, ResultContract, PostgreSQL reader, and artifact creation. | ✅ | 2026-10-14 |

### Implementation Phase 3 — Runtime and acceptance

- **GOAL-002**: Confirm the corrected semantic path is the code actually served and remains testable after restart.

| Task | Description | Completed | Date |
| --- | --- | --- | --- |
| TASK-004 | Add bounded provider retry only for transient interpretation failure, with no SQL permission on final failure. | ✅ | 2026-10-14 |
| TASK-005 | Run focused Python, lint, diff, and endpoint checks; preserve external raw evidence and sanitized repository summaries. | ✅ | 2026-10-14 |
| TASK-006 | Update project, architecture, learning records and the Feishu project document. | ✅ | 2026-10-14 |

### Completion evidence

- Eight live regression cases parsed and passed all declared semantic expectations in
  `semantic-intent-phase23-20261014/intent-regressions-r3-retry.json`.
- Authenticated PostgreSQL scope probes for state grouping and São Paulo customer-state
  filtering each passed with one SQL call and a valid result contract.
- Focused regression: `246 passed, 8 skipped`; skipped tests require explicit database opt-in.
- Ruff, diff check and `GET /embedded-copilot` (`200`) passed. The existing product service
  was restarted in `data-analysis-agent-m4` on port 32013.
- The interpreter retries once only after a provider exception/timeout. A second failure is an
  interpretation failure and cannot reach QueryPlan or SQL.

This evidence validates the semantic boundary and runtime wiring, not general SQL correctness,
business accuracy, throughput or a production SLA.

## 3. Alternatives

- **ALT-001**: Add more keyword regexes. Rejected because it recreates the fixed-sentence gate.
- **ALT-002**: Let the model generate SQL before scope validation. Rejected because it weakens the plan and security boundary.

## 4. Dependencies

- **DEP-001**: Existing Olist Catalog and authenticated PostgreSQL workspace.
- **DEP-002**: SiliconFlow semantic-intent provider for live probes.

## 5. Files

- **FILE-001**: `src/data_analysis_agent/semantic_intent.py`
- **FILE-002**: `scripts/evals/run_semantic_router_baseline.py`, `scripts/evals/smoke_semantic_runtime.py`
- **FILE-003**: `tests/test_semantic_intent.py`
- **FILE-004**: `evals/cases/semantic_intent_regressions_v1.yaml`

## 6. Testing

- **TEST-001**: Parser rejects unknown or role-invisible objects.
- **TEST-002**: Scope expectations detect extra dimensions even when JSON is valid.
- **TEST-003**: Authenticated state/filter probes complete with one SQL call and a valid result contract.
- **TEST-004**: Provider failure cannot invoke SQL.

## 7. Risks & Assumptions

- **RISK-001**: A provider may return valid but semantically wrong intent; explicit expectations and SQL/Gold review are required.
- **RISK-002**: One local provider sample cannot establish overall online accuracy or latency SLA.
- **ASSUMPTION-001**: Existing result artifacts and reader permissions are already versioned and usable.

## 8. Related Specifications / Further Reading

- [`feature-semantic-intent-phase01.md`](feature-semantic-intent-phase01.md)
- [`process-agent-internship-improvement-v1.md`](process-agent-internship-improvement-v1.md)
- [`product-request-architecture-v1.md`](../docs/architecture/product-request-architecture-v1.md)
