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
database assets. The package is now stored and hashed outside Git; its terms
and official-release comparability remain separate publication gates.

The historical experiments in this repository use a 2020-01 Kaggle mirror,
which predates the official August 2020 correction release. The official Spider
data package was subsequently downloaded directly from the task-page Google
Drive source on 2026-08-25, verified, and stored separately outside Git. Its
official `dev.json` differs from the historical mirror in 101 ordered records,
so the historical predictions and results cannot be reused for this official
package.

The official Test Suite package was downloaded through a temporary local-VPN
SSH reverse proxy on 2026-08-24 and extracted outside Git. Its archive SHA-256 is
`9ec24ea8debc6bd04abfe137b5f1a739b5a8836f32c0464e4dfc94eb7f41da96`, its
extracted SQLite tree SHA-256 is
`c9529ce837eeb68a7eb98af9dfa1caf721ff566ebb871835a9910e96b3d963bd`, and its
layout contains 3,194 SQLite files in 28 task directories. All 20 database IDs
used by the official 1,034-case dev package are present. A table/column/type
comparison against the official package found no mismatch for those 20
databases. These checks establish structural compatibility only. The independent
asset license/terms are not separately verified, and no fresh full-coverage run
has used the official package, so no official-package evaluator score has been
run or published.

An internal full-coverage run was subsequently completed with the unmodified
evaluator at the pinned commit. It evaluated all 1,034 cases and returned the
following execution diagnostic: easy `0.820`, medium `0.620`, hard `0.437`,
extra `0.300`, all `0.585`. The run is stored outside Git at
`/disk2/gengnan/data-analysis-agent-data/experiments/spider-qwen25coder3b-test-suite-v3-20260824/`;
its gold/prediction/raw-output hashes are recorded in the manifest and evidence
file. These numbers are an internal reference for the selected historical mirror
and asset, not an official-package or current official leaderboard claim.

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
6. The raw model completion remains in its external prediction JSONL. Before
   the bridge writes its one-line prediction file, it applies the shared
   Text-to-SQL candidate normalizer used by the SQLite diagnostic: only an SQL
   code fence, `SQLQuery:`/`SQL:` prefix, or `### Answer`/`### Explanation`
   presentation tail may be removed. It never repairs SQL. A genuine SQL `--`
   line comment is converted to an equivalent block comment before physical
   newlines are folded, preserving its scope.

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
