# Synthetic Fixtures

The obsolete `sales_daily.csv` and early SQLite product launcher were removed on
2026-10-20. The current installable demonstration uses the self-constructed
PostgreSQL data in `infra/postgres/synthetic.sql`; see `docs/deployment.md`.
Offline benchmark fixtures below are retained for reproducibility and do not
represent online business quality.

`olist_queryspec_coverage_seeds_v1.jsonl` is also a commit-safe fixture, but it is
not a dataset. Each row declares only a static Olist QuerySpec coverage shape for
the offline materializer: metric IDs, result shape, permitted dimension, frozen
time contract, join program, and split. It contains no question, Prompt, SQL,
result, protected-holdout content, or raw Olist rows. See
`docs/post-training/data/olist-queryspec-coverage-seed-manifest-v1.md` before
using it; it is deliberately not materialized during its design iteration.

`thelook_v3_coverage_contract_v2.json` and
`thelook_v3_coverage_seeds_v2.jsonl` are commit-safe coverage design assets,
and remain the reproducible source for the external protected release. The
JSON contract fixes the 750-case fact-domain, shape, time-grain, dimension,
scenario-family, risk-tag and Chinese-surface quotas while retaining the
existing twenty TheLook v2 metrics. It explicitly records the four approved
`inventory_unsold_snapshot` QuerySpec reuses from v2; other QuerySpec/Gold/
question overlap remains a release failure. The JSONL has 750 deterministic
`QuerySpec`-backed seeds with separate `scenario_family_id`, derived
`program_signature`, risk tags and date-window candidates. It intentionally
contains no natural-language question, Prompt, Gold SQL, database result, raw
TheLook row or model output. These seeds have now been rendered, executed and
admitted into the external release
`/disk2/gengnan/data-analysis-agent-data/evals/thelook-cross-schema-final-test-v3-20260915-r3/`;
the release is evaluation-only and does not enter training. The v1 files remain
historical design artifacts.

`thelook_sft_coverage_contract_v1.json` is separate from the protected v3
contract. It freezes the current TheLook SQL-only SFT seed boundary: 750
split-disjoint programs, five fixed window instances per program, and `3,000
train / 750 validation` candidate seeds across the 20 non-snapshot families.
Its admission contract is explicitly **1,000 fetched rows** with an
independent **200-row UI preview budget**: 201--999 rows are complete
executions with display truncation, while reaching 1,000 remains potentially
incomplete. Its authoritative external evidence is
`/disk2/gengnan/data-analysis-agent-data/evals/thelook-sql-only-sft-v1-r4/`.
It contains no natural-language question or final Trainer JSONL yet.
