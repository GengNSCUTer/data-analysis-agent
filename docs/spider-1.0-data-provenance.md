# Spider 1.0 Data Provenance

## Purpose and Scope

This record governs the historical Kaggle mirror used by completed offline
experiments and the separately stored official Spider 1.0 package acquired on
2026-08-25. It does not add SQLite to the product runtime, modify the
PostgreSQL/Vanna service, or authorize committing any raw benchmark artifact to
Git.

## License Decision

On 2026-08-24, the official [Spider 1.0 task page](https://yale-lily.github.io/spider)
was reviewed. Its "Getting Started" section states that the linked Spider
dataset is distributed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/legalcode).
That statement is the license evidence for the original raw data package. The
repository at `taoyds/spider` is separately Apache-2.0 and provides code only;
its license is not used to justify dataset use.

When the frozen baseline was established on 2026-08-24, the official Google
Drive package was not reachable from the server network. The historical
baseline therefore used Kaggle dataset
`jeromeblanchet/yale-universitys-spider-10-nlp-dataset`, version 1, published
2020-01-27. Its description identifies `https://yale-lily.github.io/spider` as
the data source and its file list exposes the native `spider/dev.json` and
`spider/database/<db_id>/<db_id>.sqlite` layout. The mirror marks its own
metadata license as `unknown`; this project does not reinterpret that as a new
license and instead retains the original-source CC BY-SA attribution.

The project uses the release only for non-commercial research/evaluation and
will retain the required citation and CC BY-SA attribution. Raw data, SQLite
databases, database contents, gold SQL, model predictions and reports remain
outside this Git repository.

## Official Package Acquisition

On 2026-08-25, the user provided a temporary local-VPN proxy path and the
server downloaded the official Google Drive archive directly. `unzip -tq`
passed, the member list contained no absolute or parent-traversal path, and only
the `spider_data/` members were extracted to a new external directory. The old
Kaggle mirror remains intact and remains the input for all existing historical
base/adapter reports.

| Field | Value |
| --- | --- |
| Dataset | Spider 1.0 official data package |
| Release ID | `spider-1.0-official-v1-20260825` |
| Official archive page | <https://drive.google.com/file/d/1403EGqzIDoHMdQF4c9Bkyl7dZLZ5Wt6J/view?usp=sharing> |
| Retrieved | 2026-08-25 via temporary user-provided VPN proxy |
| Storage | `/disk2/gengnan/data-analysis-agent-data/text-to-sql/spider/spider-1.0-official-v1-20260825/spider_data` |
| Archive | `spider_data.zip`, 205,800,266 bytes, SHA-256 `00636695dabed6b5f4b8328a16b13e069a2f16591d5efcce57660669c85b121b` |
| Archive validation | `unzip -tq` passed; 2,625 members had no absolute or `..` paths; 1,313 `spider_data/` members retained |
| `train_spider.json` | 7,000 cases, SHA-256 `c43d0d72e59e1a9e1a60837da9bf70d5a6277226bdb7f634d544f380646f527a` |
| `dev.json` | 1,034 cases, SHA-256 `30d64a3fccde493226df79687aed9e4a1c0129525baf44f29c0573d914d758a4` |
| `tables.json` | 166 schemas, SHA-256 `61bb20aa401f03164e2d7f3b16509b7b5f79cc9c943ca7bd159046df1159e2ed` |
| `database/` | 166 SQLite files, tree SHA-256 `67d29c2285095e39c15d08632605bb0b94945aac5aef38fca2e15540548a5aba` |
| Development databases | 20 SQLite files, tree SHA-256 `deef29d6daa75a5fd184d5e7b196bea149b7e52f329feced25d68c2819d175ca` |

The external acquisition manifest at
`/disk2/gengnan/data-analysis-agent-data/text-to-sql/spider/spider-1.0-official-v1-20260825/acquisition-manifest.json`
contains the full source, hash and compatibility record. The 20 databases used
by official `dev.json` all exist in the downloaded Test Suite asset and a
table/column/type comparison found zero mismatches. This establishes structural
compatibility, not a completed score.

The official and historical package have identical train/dev case counts but
different contents: 933 of 1,034 ordered dev records and 6,327 of 7,000 ordered
train records are identical. All existing candidate generations, diagnostics,
Test Suite outputs and paired results remain historical-mirror evidence. A new
official-package result requires new full-coverage generation and evaluation;
it must not reuse an old prediction file or score.

## Historical Mirror Acquisition Record

| Field | Value |
| --- | --- |
| Dataset | Spider 1.0 development data mirror |
| Release ID | `spider-1.0-kaggle-mirror-v1-2020-01-27` |
| Official task page | <https://yale-lily.github.io/spider> |
| Official archive page | <https://drive.google.com/file/d/1403EGqzIDoHMdQF4c9Bkyl7dZLZ5Wt6J/view?usp=sharing> (unreachable when this mirror was acquired) |
| Actual distributor | Kaggle v1 mirror: <https://www.kaggle.com/datasets/jeromeblanchet/yale-universitys-spider-10-nlp-dataset> |
| Distributor release date | 2020-01-27T20:22:40Z |
| Raw-data license | CC BY-SA 4.0 from original official Spider source; Kaggle mirror metadata = `unknown` |
| License verified | 2026-08-24 |
| Intended retrieval date | 2026-08-24 |
| Storage | `/disk2/gengnan/data-analysis-agent-data/text-to-sql/spider/spider-1.0-kaggle-mirror-v1-2020-01-27` |
| Code/evaluator source | `taoyds/spider` commit `b7b5b8c890cd30e35427348bb9eb8c6d1350ca7c` (Apache-2.0 code only) |

The mirror predates the official August 2020 correction release. It is therefore
not an official-current-release or leaderboard-comparable artifact. After
retrieval, fill the following fields with measured values. The archive and
extracted contents must be verified before model generation starts.

| Required measured artifact | SHA-256 / result |
| --- | --- |
| Downloaded archive | `archive.zip`, 100,663,520 bytes, `ed2a34b84e9665606da73497f4166b1c8d94056517614c33f9dcdca45346be0f` |
| Extracted `dev.json` | `7770d539e4093291a9e7dc383deeb6c09410be566b5542c044b65a291c28eb0e` |
| Extracted `tables.json` | `b68de9166952871d64554486fca4b25ff88509e983857a6af428250ffd58b67f` |
| Extracted `database/` tree | `f85f3482bfb1ad5fb93ac49dc83c288d57c8d3da7f4871493f97c348aad5248d` (166 `.sqlite` files) |
| Extracted development-database tree | `29504fcf305416369d18d04ee645a59c3efa2312a47796dce1631c884c820a13` (20 SQLite databases) |
| Native layout validation | Passed: 398 safe `spider/` archive members, 1,034 dev cases, 20 dev database IDs, all required `<db_id>/<db_id>.sqlite` files present |
| Official evaluator code | `taoyds/test-suite-sql-eval` commit `e97acc546ecbee8fa27fa8dbf025ef61493a876c`, Apache-2.0 code, clean checkout verified outside Git |
| Official test-suite database assets | Pending explicit asset terms, release identity, hashes and compatibility decision; not downloaded |

The database-tree digest is calculated by lexicographically sorting every
`.sqlite` path relative to `database/`, then hashing each relative path, a NUL
separator, that file's SHA-256, and a newline into one SHA-256 digest. This makes
the tree record independent of file-system traversal order.

## Metric Boundary

`scripts/run_sqlite_benchmark.py` reports secure local candidate-execution
diagnostics only. It does not consume gold SQL or calculate any benchmark score.
The official Spider task page identifies Test Suite Accuracy as its official
leaderboard metric. A future score requires the unmodified official test-suite
evaluator and its assets; it must be preserved as a distinct external artifact.
The project bridge additionally requires full case coverage to prevent the
upstream evaluator's `zip(predictions, gold)` behavior from silently shortening
the denominator.
