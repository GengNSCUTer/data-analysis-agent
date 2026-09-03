# Runtime Contract Preflight v1

## Task Card

- **Goal:** align the Olist Catalog, request routing, SQL policy, database row budget, and result contract before any Olist domain SFT materialization.
- **Non-goals:** no QuerySpec/renderer implementation, no training data, no split or token audit, no GPU task, and no expansion of business metrics.
- **Inputs:** `olist_catalog.yaml`, the trusted runtime QueryPlan/Router, SQL policy limits, and ResultValidator.
- **Output/interface impact:** unsupported or over-budget query shapes return a deterministic clarification before SQL generation; candidate SQL must return exactly the server-owned result columns and metric values must meet declared range constraints.
- **Invariants:** a sensitive physical column cannot be an analyst-visible output dimension; the router must not silently drop requested metrics beyond its prompt limit; the runtime validator must use the SQL policy's effective row limit; no layer may claim general join-amplification detection without observed evidence.
- **Acceptance evidence:** focused unit tests plus PostgreSQL integration coverage show that each invariant is enforced at its owning boundary.

## Frozen v1 Decisions

1. Candidate SQL and future Gold SQL share a maximum of four requested metrics per query. Requests above that require a smaller query rather than partial metric retrieval.
2. `seller_id` is never an analyst-visible dimension. Its use remains allowed only inside permitted joins or aggregates under SQL Policy.
3. `customer_city` is not a supported grouped output until a separate Top-N, ordering, row-budget, and truncation contract exists.
4. Grouped temporal output must fit the 200-row analyst result budget according to Catalog-declared dimension bounds. When an ISO absolute range is available, preflight uses its conservative inclusive day/week/month/quarter/year bucket count; otherwise it uses the global bound. Daily series additionally require an explicit bounded date range.
5. Exact result columns and value ranges are server-owned contracts. Formula correctness and arbitrary join multiplication remain outside value-only result validation and require deterministic SQL construction plus semantic review.

## Historical Evaluation Migration

`evals/cases/text_to_sql_v2.yaml` remains a protected `olist-catalog-v1` / `0.1-draft` artifact and is not edited to fit this v2 Catalog. Replaying it against the new runtime yields `57/60` expected matches: `data_014` and `multi_006` now require an unfrozen category-order attribution rule, while `multi_005` is rejected because the conservative `customer_state × month` estimate is `27 × 36 = 972` rows, above the analyst limit of 200. These are explicit contract migrations, not model-quality or v2 regression scores.

## Verification Scope

- Unit coverage verifies Catalog fail-closed displayability, router preflight, metric-count refusal, QueryPlan row-shape bounds, ResultContract metadata, ResultValidator exact columns/value ranges, and the SQL Policy's effective row limit.
- PostgreSQL integration coverage verifies that a SQL Policy-allowed extra result column is refused by the server result contract, and that a query reaching the Policy-added 200-row limit becomes a clarification rather than a partial answer. `SecurePostgresRunner` enables `ResultValidator` by default, so this boundary does not depend on a composition root remembering to inject it.
- `PolicyDecision.policy_limit_applied` distinguishes an absent/over-budget SQL `LIMIT` that Policy inserted or tightened from a smaller `LIMIT` already present in candidate SQL. Only the former is deterministic evidence of a policy-truncated answer. An unjustified candidate-side `LIMIT` remains a QueryPlan/renderer semantic risk; this preflight does not claim to solve it.
- The checks do not prove a metric formula, a generated SQL program, arbitrary join multiplicity, business attribution, or online-model semantic accuracy.
