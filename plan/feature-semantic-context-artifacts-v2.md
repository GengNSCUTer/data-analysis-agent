---
goal: Implement semantic history compression, verified analysis-state carry-over, durable result artifacts, and token-budgeted context
version: 2.0
date_created: 2026-09-20
last_updated: 2026-09-20
owner: gengnan
status: In progress
tags: [feature, runtime, context, history, artifacts, token-budget]
---

# Introduction

![Status: In progress](https://img.shields.io/badge/status-In%20progress-yellow)

Replace HistorySummary v1's provenance-only boundary with a bounded semantic summary that is useful for conversation continuity, while retaining server-owned structured state as the only authority for SQL. Add durable, access-controlled result-artifact references outside the repository. Move the context decision from characters to a configurable token counter with a 64K effective context target.

## 1. Requirements & Constraints

- **REQ-001**: When old complete turns no longer fit, inject a bounded LLM-generated semantic history summary into the next LLM request and cache it by the omitted source digest.
- **REQ-002**: Persist the latest reusable semantic summary as bounded conversation state with its source range, digest, model/version, token count, and generation status; retain original messages separately.
- **REQ-003**: Preserve SQL safety: prose summary must never directly supply metric IDs, dimensions, filters, dates, permissions, SQL, ResultContract fields, or database facts. Follow-up SQL state must be reconstructed only from validated WorkingMemory, QueryPlan, ResultContract and ResultArtifact records.
- **REQ-004**: Persist validated full result CSV and optional deterministic Plotly JSON as immutable, user/workspace-scoped files outside Git; persist only opaque references, hashes, byte counts and version pins in conversation metadata.
- **REQ-005**: Make the filter choose complete turns by a token budget and message budget. Production target is a 64,000-token total prompt ceiling with explicit reservation for system prompt, Catalog/tool context and output.
- **SEC-001**: Do not store raw SQL, raw result rows, raw chart payloads, user messages or LLM history-summary prose in `agent_runs.catalog_trace` or safe reports.
- **SEC-002**: Do not claim exact token accounting unless `DATA_ANALYSIS_TOKENIZER_PATH` points to the exact tokenizer used by the provider. A fallback counter must be marked `estimated` in evidence.
- **CON-001**: Long-term memory, cross-conversation retrieval, vector storage and user profiles remain out of scope.
- **CON-002**: A semantic-summary failure must fall back to the existing provenance boundary; it must not fail a valid data-analysis request.

## 2. Implementation Steps

### Implementation Phase 1

- GOAL-001: Define token-accounting and semantic-summary contracts without changing SQL authority.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Add `token_budget.py` with exact-local-tokenizer and explicitly estimated fallback counters; add `max_prompt_tokens=64000`, prompt reservations, history token budget and semantic-summary token limits to `RequestBudget`. |  |  |
| TASK-002 | Extend `history_summary.py` with a versioned semantic summary record, source-digest reuse and a strict LLM summary prompt that treats conversation text as data, not instructions. |  |  |
| TASK-003 | Refactor `ContextBudgetFilter` to select complete turns by token/message budget, inject semantic summary when available, and fall back to provenance boundary on summarizer failure. |  |  |

### Implementation Phase 2

- GOAL-002: Persist bounded semantic state and durable validated artifacts.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-004 | Extend `WorkingMemory` and `BudgetUsage` with bounded semantic-summary provenance, but keep summary prose out of run evidence and SQL condition authority. |  |  |
| TASK-005 | Add `result_artifact_store.py`; atomically persist validated CSV and optional server-rendered Plotly JSON under `/disk2/gengnan/data-analysis-agent-data/`, then extend `ResultArtifact` with opaque references and integrity hashes. |  |  |
| TASK-006 | Wire `BudgetedChatHandler`, `TrustedRunSqlTool` and trusted-demo endpoints so only server-validated results enter the artifact store and authorized conversation owners can retrieve them. |  |  |

### Implementation Phase 3

- GOAL-003: Verify semantic continuity, artifact integrity and budget accounting.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-007 | Add isolated unit tests for token accounting, source-digest summary reuse/failure fallback, SQL-state non-authority, artifact checksum and owner/workspace isolation. |  |  |
| TASK-008 | Add a small live long-conversation regression after the exact/estimated counter mode is recorded; report token budget, prompt reservation, summary generation latency and artifact references without leaking content. |  |  |
| TASK-009 | Update `AGENTS.md`, architecture documentation, `PROJECT.md` and the safe-report contract to distinguish semantic summary, structured SQL state, result artifacts and token-accounting accuracy. |  |  |

## 3. Alternatives

- **ALT-001**: Parse natural-language summaries into SQL clauses directly. Rejected because an LLM summary can hallucinate or preserve stale facts; validated structured state remains the authority.
- **ALT-002**: Treat `tiktoken` as the provider's exact tokenizer. Rejected because the production SiliconFlow model tokenizer/version is not locally available.
- **ALT-003**: Put result CSV/Plotly data in conversation JSONB. Rejected because it inflates transactional metadata and bypasses object-level retention/access controls.

## 4. Dependencies

- **DEP-001**: `ContextBudgetFilter`, `BudgetedChatHandler`, `WorkingMemory`, `ResultArtifact`, `TrustedRunSqlTool`, `PostgresConversationStore` and the trusted demo remain the integration points.
- **DEP-002**: Exact accounting requires a local official tokenizer directory supplied through `DATA_ANALYSIS_TOKENIZER_PATH`; the current provider does not expose it in this workspace.

## 5. Files

- **FILE-001**: `src/data_analysis_agent/token_budget.py` — token counters and budget profile.
- **FILE-002**: `src/data_analysis_agent/history_summary.py` and `context_builder.py` — semantic summary and complete-turn selection.
- **FILE-003**: `src/data_analysis_agent/working_memory.py`, `budget.py`, `chat_runtime.py` — bounded state and persistence wiring.
- **FILE-004**: `src/data_analysis_agent/result_artifact.py`, `result_artifact_store.py`, `trusted_sql_tool.py` and `examples/trusted_olist_web_demo.py` — durable artifacts and authorized retrieval.
- **FILE-005**: `tests/test_context_builder.py`, `tests/test_working_memory.py`, `tests/test_result_artifact.py` and new artifact/token tests.

## 6. Testing

- **TEST-001**: A token over-budget history preserves a newest contiguous suffix and uses cached semantic summary only when its source digest matches.
- **TEST-002**: A failed summary provider produces the v1 provenance boundary and does not terminate the request.
- **TEST-003**: Prompt prose cannot update `WorkingMemory` metrics, dates, dimensions or filters without existing validated server paths.
- **TEST-004**: A validated CSV/Plotly artifact has atomic external storage, immutable SHA-256 evidence and cannot be resolved by another user or workspace.
- **TEST-005**: Context evidence states `exact` or `estimated` token mode and never exposes message/SQL/result content.

## 7. Risks & Assumptions

- **RISK-001**: A semantic summary adds one model call when the omitted source changes; caching and bounded incremental input limit repeat latency.
- **RISK-002**: Current provider tokenization cannot be proven exact without its official tokenizer; fallback operation remains conservative and visibly estimated.
- **ASSUMPTION-001**: The provider has at least a 128K context window; the runtime reserves half of it rather than filling the whole window with history.

## 8. Related Specifications / Further Reading

- [Context budget and HistorySummary v1](feature-context-budget-history-compression-v1.md)
- [Embedded Copilot and memory design](../docs/architecture/embedded-copilot-and-memory-design-v1.md)
