---
title: Result Budget v2 Performance Baseline
status: measured
date: 2026-10-13
---

# Result Budget v2 Performance Baseline

## Test scope

This is a database and transport benchmark for the synthetic `perf_shop`
workspace. It does not call an LLM, does not generate SQL from natural
language, and does not measure semantic accuracy. The database is rebuilt from
the deterministic [`perf_shop.sql`](../../infra/postgres/perf_shop.sql)
generator before each scale run.

The same checks were run at 100,000 and 1,000,000 orders. The latter is the
scale left materialized in the local `perf_shop` database after the test.

## Results

| Mode | 100k orders | 1m orders | Meaning |
| --- | ---: | ---: | --- |
| Scalar aggregate rows | 1 | 1 | Large fact table can still produce a small complete result |
| Monthly time-series rows | 36 | 36 | Result cardinality is independent of raw order count here |
| Detail page rows | 200 | 200 | Bounded page, with a sentinel proving another page exists |
| Keyset next-page rows | 200 | 200 | Stable continuation after `order_id > 200` |
| High-cardinality group rows | 100,000 | 100,000 | Must not be silently presented as a complete 1,000-row result |
| Export simulation rows | 100,000 | 100,000 | Full CSV path is measured separately from model context |

The 1m run produced approximately:

- scalar aggregate: 83 ms, 37 CSV bytes;
- monthly series: 178 ms, 630 CSV bytes;
- detail page: 1.7 ms, 9.8 KB CSV, `has_next_page=true`;
- high-cardinality grouping: 407 ms, 889 KB CSV;
- export simulation: 346 ms, 889 KB CSV;
- `EXPLAIN` aggregate plan: 1 estimated row, total cost 15,468.57;
- `EXPLAIN` high-cardinality plan: approximately 100,282 rows, total cost 68,759.2.

The 100k evidence is in the external file
`/disk2/gengnan/data-analysis-agent-data/perf/perf-shop-v1-100000/benchmark-v2.json`.
The 1m evidence is in
`/disk2/gengnan/data-analysis-agent-data/perf/perf-shop-v1-1000000/benchmark-v2.json`.

## What this proves

1. A global result-row cap is the wrong proxy for database workload. A 1m-row
   fact table can safely return one aggregate row or 36 time buckets.
2. A 200-row detail page can be kept fast while a sentinel row communicates
   that continuation exists. The sentinel is not part of the page payload.
3. High-cardinality grouping and full export need a separate result contract.
   Their CSV size and result cardinality are observable even when execution is
   fast enough for this synthetic database.
4. `EXPLAIN` gives a useful preflight signal, but it does not prove semantic
   correctness and should not be used as a replacement for ResultContract or
   ResultValidator.

## What this does not prove

- It does not establish production thresholds for timeout, cost or export size.
- It does not prove that an arbitrary Olist/TheLook query is safe at the same
  scale; indexes, joins, skew and text columns differ.
- It does not authorize changing the online 1,000-row policy cap.
- It does not open a full export route or change authentication.

## Next implementation gate

Use these measurements to add a server-owned `query_mode` and result budget to
`QueryPlan`/`ResultContract`. The first runtime change should be a fail-closed
classification and evidence field, not a larger global cap. A later change can
allow a complete bounded aggregate result, explicit detail pagination and an
audited asynchronous export independently.
