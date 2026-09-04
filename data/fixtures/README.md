# Synthetic Fixtures

`sales_daily.csv` is a tiny, deterministic fixture for local smoke tests. It is not
third-party data and does not represent a real business. The Vanna Phase 1 launcher
currently creates the equivalent SQLite table at runtime; later iterations may load
this CSV instead so that the fixture has one canonical source.

The fixture is intentionally small enough to commit. It must not be used to claim
business accuracy, model accuracy, or production performance.

`olist_queryspec_coverage_seeds_v1.jsonl` is also a commit-safe fixture, but it is
not a dataset. Each row declares only a static Olist QuerySpec coverage shape for
the offline materializer: metric IDs, result shape, permitted dimension, frozen
time contract, join program, and split. It contains no question, Prompt, SQL,
result, protected-holdout content, or raw Olist rows. See
`docs/post-training/data/olist-queryspec-coverage-seed-manifest-v1.md` before
using it; it is deliberately not materialized during its design iteration.
