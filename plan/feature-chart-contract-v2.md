---
goal: Expand the server-owned chart contract without widening SQL or result semantics
version: 2.0
date_created: 2026-09-20
last_updated: 2026-09-20
owner: Data Analysis Agent
status: 'Completed'
tags: [feature, charts, trusted-runtime, phase-b]
---

# Introduction

![Status: Completed](https://img.shields.io/badge/status-Completed-brightgreen)

Expand the trusted Olist runtime from two chart forms (`bar`, `line`) to five
server-selected forms (`bar`, `horizontal_bar`, `line`, `area`, `donut`). The
change must remain a presentation-only extension: it cannot introduce a second
LLM call, change generated SQL, aggregate the validated result again, or turn a
successful SQL execution into a claim of business-semantic correctness.

## 1. Requirements & Constraints

- **REQ-001**: Keep `ChartContract` server-owned. The model may generate one SQL candidate only; it cannot choose a trace, axis, title, series, or chart type.
- **REQ-002**: Support exactly `bar`, `horizontal_bar`, `line`, `area`, and `donut` in the v2 public contract. `horizontal_bar` and `area` are safe visual forms of existing grouped and time-series results; `donut` is an explicit composition view only.
- **REQ-003**: Default unspecified chart requests to `line` for a time axis, `horizontal_bar` for `product_category_name`, and `bar` for every other supported business dimension.
- **REQ-004**: Permit `area` only for a time-series plan with no business series dimension; render the already-validated values directly and do not calculate cumulative values, percentages, moving averages, or fills from missing data.
- **REQ-005**: Permit `donut` only when the user explicitly asks for a 饼图/环形图/占比构成图, the QueryPlan has exactly one supported dimension and one additive metric, and the result frame has no more than eight categories and no negative values. The renderer must keep raw metric values and use them only as Plotly slice values.
- **SEC-001**: Preserve the existing chain `SqlPolicy -> PostgreSQL reader role -> ResultContract/ResultValidator -> request-local validated DataFrame -> ChartContract renderer`; charts cannot read files, execute SQL, use prior-turn results, or call an LLM.
- **SEC-002**: Preserve frame rejection for unknown or missing columns, nulls, non-numeric metric fields, duplicate x/series keys, more than 200 result rows, more than 120 x values, and more than eight series. Add donut-specific non-negative and eight-slice checks.
- **CON-001**: Do not add scatter, heatmap, map, histogram, funnel, waterfall, or stacked-bar support in v2. Those require a new two-variable, spatial, raw-distribution, stage-attribution, or multi-dimensional ResultContract rather than a Plotly-only change.
- **CON-002**: Do not change `QueryPlan`, Catalog metric formulas, SQL Policy, ResultValidator semantics, SQL prompt shape, or database permissions in this feature.
- **GUD-001**: Unsupported requests must be rejected with a Chinese explanation; no silent conversion to a different visual form is allowed.
- **PAT-001**: `ChartContract.from_query_plan()` selects presentation semantics before SQL; `ChartContract.validate_frame()` validates the resulting trusted artifact; `render_chart_data()` creates direct Plotly traces only.

## 2. Implementation Steps

### Implementation Phase 1

- GOAL-001: Freeze the v2 chart selection and safety contract before changing rendering code.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Update `src/data_analysis_agent/chart_contract.py`: introduce the five-value `ChartType`, v2 keyword recognition, deterministic default selection, explicit-type conflict handling, and title suffixes. Keep metadata deserialization allowlisted to the same five values. | ✅ | 2026-09-20 |
| TASK-002 | Add contract tests in `tests/test_chart_contract.py` covering default category horizontal bars, explicit area validity/rejection, donut eligibility/rejection, and retained rejection for unsupported chart classes. | ✅ | 2026-09-20 |

### Implementation Phase 2

- GOAL-002: Render every newly approved chart directly from the validated current result frame.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-003 | Update `src/data_analysis_agent/visualization.py`: use Plotly horizontal bars, filled scatter traces for areas, and donut pie traces; do not add any groupby, sorting beyond the existing contract time ordering, value transformation, or LLM/tool registration. | ✅ | 2026-09-20 |
| TASK-004 | Extend `ChartContract.validate_frame()` with donut-specific category-count and non-negative-value checks, while retaining existing frame invariants for all chart types. | ✅ | 2026-09-20 |
| TASK-005 | Extend `tests/test_visualization_policy.py` to assert Plotly trace properties for each new type and frame rejection for invalid donut values. | ✅ | 2026-09-20 |

### Implementation Phase 3

- GOAL-003: Verify the runtime contract and real embedded page path without changing product phase scope.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-006 | Update `examples/trusted_olist_web_demo.py` runtime-contract metadata and `docs/architecture/product-capability-baseline-v1.md` so the observable capability list matches ChartContract v2 and documents the intentionally excluded forms. | ✅ | 2026-09-20 |
| TASK-007 | Run focused chart tests, affected Phase B contract tests, compile checks, Ruff and diff checks; request one real Olist category horizontal-bar and one valid donut response through the embedded demo. Store raw SSE and screenshots outside the repository. The runtime environment's full test collection remains blocked by pre-existing missing `torch` training dependencies. | ✅ | 2026-09-20 |

## 3. Alternatives

- **ALT-001**: Add every Plotly chart type immediately. Rejected because a rendering library capability does not provide a corresponding trusted business-result contract.
- **ALT-002**: Let the model choose chart type after SQL execution. Rejected because the previous `visualize_data` path already demonstrated that a valid result could be left unvisualized or described with the wrong contract state.
- **ALT-003**: Add only `horizontal_bar` and `area`. Rejected because the product still lacks a bounded composition view; the explicit, additive, small-category donut contract is sufficiently constrained to include in this release.

## 4. Dependencies

- **DEP-001**: Existing `QueryPlan`, `ResultContract`, and `ResultValidator` continue to produce a request-local validated DataFrame.
- **DEP-002**: Existing Plotly dependency and web component `ChartComponent` rendering support the output JSON traces without a frontend API change.
- **DEP-003**: The trusted embedded demo must be running with the project Python environment for live SSE/browser evidence.

## 5. Files

- **FILE-001**: `src/data_analysis_agent/chart_contract.py` — selection, metadata and frame-validation contract.
- **FILE-002**: `src/data_analysis_agent/visualization.py` — deterministic Plotly trace builder.
- **FILE-003**: `tests/test_chart_contract.py` — pre-SQL selection tests.
- **FILE-004**: `tests/test_visualization_policy.py` — post-validation rendering and rejection tests.
- **FILE-005**: `examples/trusted_olist_web_demo.py` — public runtime capability metadata.
- **FILE-006**: `docs/architecture/product-capability-baseline-v1.md` — product capability boundary.

## 6. Testing

- **TEST-001**: Contract selection tests prove defaults, explicit requests, conflicts and unsupported forms have deterministic outcomes.
- **TEST-002**: Frame tests prove all traces use only declared columns and donut rejects negative / over-eight-slice data.
- **TEST-003**: Focused `pytest` verifies ChartContract, visualization, Router and Phase B contracts; `compileall`, Ruff and `git diff --check` catch integration/regression issues. The service Conda environment's full test collection currently cannot import seven existing post-training modules because it deliberately lacks `torch`.
- **TEST-004**: Live embedded-demo requests proved server-owned horizontal-bar and donut components appear after one SQL call and zero visualization-tool calls; raw event evidence remains under `/disk2/gengnan/data-analysis-agent-data/`.

## 7. Risks & Assumptions

- **RISK-001**: A model may return more than eight rows for an explicit donut request. The data answer remains subject to ResultContract; only the chart is rejected with a scope-reduction message.
- **RISK-002**: A metric that is not additive cannot be used for composition. v2 uses a static allowlist of additive metric IDs and rejects other donut requests before SQL.
- **ASSUMPTION-001**: Olist grouped categories and time-series outputs already fit existing SQL/result contracts; this feature only changes their visual presentation.
- **ASSUMPTION-002**: Phase C remains out of scope until this feature has focused and live verification evidence.

## 8. Related Specifications / Further Reading

- [Product capability baseline v1](../docs/architecture/product-capability-baseline-v1.md)
- [Chart contract implementation](../src/data_analysis_agent/chart_contract.py)
- [Trusted visualization implementation](../src/data_analysis_agent/visualization.py)
