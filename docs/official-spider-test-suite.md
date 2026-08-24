# Official Spider Test Suite Evaluation Boundary

## Purpose

This document specifies the only permitted path for a future official Spider
Test Suite run. The production application remains PostgreSQL plus Vanna. The
bridge exists only for offline model evaluation and does not expose SQLite or
the evaluator through the product API.

## Pinned Code and Separate Assets

The official evaluator code is cloned outside this repository from
[`taoyds/test-suite-sql-eval`](https://github.com/taoyds/test-suite-sql-eval),
commit `e97acc546ecbee8fa27fa8dbf025ef61493a876c`. That repository is Apache-2.0.
Its README separately directs users to download generated test-suite databases
from Google Drive. The source-code license does not automatically cover those
database assets, so the assets remain `not_downloaded` until their explicit
terms, release identity and checksums are verified.

The current Spider data is a 2020-01 Kaggle mirror which predates the official
August 2020 correction release. The official Test Suite package was downloaded
through a temporary local-VPN SSH reverse proxy on 2026-08-24 and extracted
outside Git. Its archive SHA-256 is
`9ec24ea8debc6bd04abfe137b5f1a739b5a8836f32c0464e4dfc94eb7f41da96`, its
extracted SQLite tree SHA-256 is
`c9529ce837eeb68a7eb98af9dfa1caf721ff566ebb871835a9910e96b3d963bd`, and its
layout contains 3,194 SQLite files in 28 task directories. All 20 database IDs
used by the current 1,034-case dev mirror are present. A table/column/type
comparison against the current mirror found no mismatch for those 20 databases.
These checks establish structural compatibility only. The independent asset
license/terms are not separately verified, and this older mirror is still not
proven comparable to the current official leaderboard release, so no official
evaluator score has been run or published.

## Bridge Contract

[`scripts/run_official_spider_test_suite.py`](../scripts/run_official_spider_test_suite.py)
uses [`spider_test_suite.py`](../src/data_analysis_agent/spider_test_suite.py) to
prepare one complete external run. It enforces these rules before the evaluator
is started:

1. The expected evaluator commit must equal `HEAD`, and the checkout must be
   clean; the official `evaluation.py` is not copied, patched or wrapped.
2. Every native Spider case must have exactly one `candidate_index=0` prediction.
   Missing, unknown, repeated and non-primary candidates are rejected.
3. The native case order becomes the official gold/prediction file order.
   Candidate generation completes before gold SQL is read for evaluation.
4. The separate test-suite database root must contain every required
   `<db_id>/<db_id>.sqlite` file. The downloaded root is
   `/disk2/gengnan/data-analysis-agent-data/text-to-sql/test-suite-databases-official-2020-12-27/database`.
5. Gold SQL, prediction SQL, unmodified evaluator stdout/stderr and the evidence
   JSON are written only outside this Git worktree. The evidence JSON contains
   hashes and run configuration, not SQL text or a locally parsed score.

The upstream evaluator contains a silent partial-run trap: for Spider, it reads
the complete gold file and a prediction file as single sessions, then loops over
`zip(predictions, gold)`. A prediction file shorter than `dev.json` therefore
changes the denominator without an upstream error. The bridge rejects partial
coverage before it writes evaluator input files. The recorded 20-case frozen
smoke exits with code 2 at this guard and produces no evaluator artifacts.

## Future Run Gate

Before the command below can be used for a reported official score, update the
data manifest and provenance with the test-suite asset license/terms, source
URL, release identifier, archive SHA-256, extracted database-tree SHA-256 and an
explicit official-release compatibility decision. The downloaded asset
metadata is recorded outside Git in
`/disk2/gengnan/data-analysis-agent-data/text-to-sql/test-suite-databases-official-2020-12-27/acquisition-manifest.json`.
Generate all 1,034 frozen predictions using an unchanged model/prompt contract;
do not fill missing cases with fabricated SQL.

```bash
/disk2/gengnan/conda_envs/data-analysis-agent/bin/python \
  scripts/run_official_spider_test_suite.py \
  --cases /external/spider/dev.json \
  --predictions /external/complete-predictions.jsonl \
  --evaluator-root /external/test-suite-sql-eval \
  --evaluator-commit e97acc546ecbee8fa27fa8dbf025ef61493a876c \
  --test-suite-database-root /external/test-suite-databases \
  --output-directory /external/official-test-suite-run
```

The bridge preserves the unmodified evaluator output for audit but does not
extract a score from it. Any later reported Test Suite Accuracy must cite that
external output, complete case count, evaluator revision, test-suite asset
manifest, model digest and prediction digest. It must remain distinct from the
secure SQLite Adapter's operational `executed_candidates` diagnostic.
