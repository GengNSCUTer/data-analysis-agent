# Frozen SQLite Baseline

## Purpose

This is the second post-training research task: produce reproducible local
Text-to-SQL candidates from a fixed small model, then evaluate those candidates
through the isolated SQLite adapter.  It is an offline research path.  It does
not add SQLite support to the PostgreSQL/Vanna product runtime, does not expose
the model through the embedded UI, and does not change any running service.

The first target is Spider 1.0 development data because it supplies native
SQLite databases and an established official evaluator boundary.  The runner is
also dataset-neutral enough to accept BIRD development cases later.  This is a
baseline for comparison before any SFT or QLoRA experiment, not a claim of
state-of-the-art accuracy.

## Frozen Experimental Contract

The checked-in configuration is
[`evals/manifests/sqlite_frozen_baseline_v1.yaml`](../evals/manifests/sqlite_frozen_baseline_v1.yaml).
It freezes the following local inference target:

| Field | Value |
| --- | --- |
| Provider | Local Ollama API only |
| Model tag | `qwen2.5-coder:3b` |
| Manifest digest | `f72c60cabf6237b07f6e632b2c48d533cef25eda2efbd34bed21c5e9c01e6225` |
| GGUF blob SHA-256 | `4a188102020e9c9530b687fd6400f775c45e90a0d7baafe65bd0a36963fbb7ba` |
| Quantization | `Q4_K_M` |
| Decode policy | temperature 0, seed 42, top-k 1, top-p 1, max 512 generated tokens |
| Prompt | `sqlite-frozen-baseline-v1` |
| Inference engine | Ollama server `0.13.1`; local CLI `0.31.2` |

The pulled Ollama package carries the **Qwen Research License Agreement** dated
2024-09-19, which permits non-commercial research/evaluation.  It is not an
Apache-2.0 model distribution.  Do not commit, redistribute, or describe the
weight blob as project code.  Any later public release of a model derived from
Qwen must honor its attribution and licensing requirements.

The local CLI and service currently report different Ollama versions.  This is
not a correctness claim or a reason to upgrade production infrastructure during
this task.  It is recorded because engine versions can affect decoding and must
remain part of any comparable baseline report.

## Data Acquisition Gate

The official Spider code repository is pinned at
`b7b5b8c890cd30e35427348bb9eb8c6d1350ca7c` and is Apache-2.0, but that code
license does not establish terms for the raw `spider.zip` dataset.  The official
Spider 1.0 task page itself resolves that boundary: it states that the linked
downloadable dataset is distributed under
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/legalcode).
The official task-page Google Drive package is the preferred source, but it is
not reachable from the current server network. This project therefore uses a
versioned Kaggle v1 mirror that explicitly cites the official task page. Its
own metadata declares an unknown license, so the manifest records that fact and
retains the official-source CC BY-SA attribution rather than treating the mirror
metadata as an independent grant. The code license must still never be used as
a substitute for data-license evidence. Archive and extracted data stay outside
Git.

Before the first benchmark execution, record all of the following in the
manifest and external acquisition log:

1. An explicit raw-data license/terms statement from the selected release.
2. Release name, source URL, retrieval date and source-repository/evaluator
   commit.
3. SHA-256 of the downloaded archive and the extracted `dev.json`/database tree.
4. Storage outside Git at
   `/disk2/gengnan/data-analysis-agent-data/text-to-sql/spider/<release>`.

The release identifier is `spider-1.0-kaggle-mirror-v1-2020-01-27`; its
required external storage location is
`/disk2/gengnan/data-analysis-agent-data/text-to-sql/spider/spider-1.0-kaggle-mirror-v1-2020-01-27`.
The mirror predates the official August 2020 correction release, so its results
are strictly internal diagnostics and must never be compared to the current
official Spider leaderboard.
The baseline code can be tested against synthetic SQLite fixtures without this
data. It must not be run against the downloaded release until hashes and layout
validation have completed.

## Candidate Generation

`scripts/run_frozen_sqlite_baseline.py` loads native benchmark items in their
original order, derives stable case IDs (`spider_dev:00000`, ...), reads **DDL
only** from each declared SQLite database, and calls Ollama without tools.  The
model sees the current question and schema in memory.  It never sees gold SQL,
gold answers, result rows, or a Vanna/PostgreSQL prompt.

Model responses are stripped only of ordinary Markdown/`SQLQuery:` wrappers.
They are not repaired or made safe by this runner.  DML/DDL or invalid text is
preserved as a candidate so the Adapter can diagnose a policy rejection or
execution error; multi-statement output is also preserved rather than truncated
at the first semicolon.  A schema over the configured character budget fails
closed; there is no silent truncation pretending to be a schema-retrieval
baseline.

Raw candidate JSONL must live outside the Git worktree.  Each line contains
only the Adapter contract:

```json
{
  "case_id": "spider_dev:00000",
  "candidate_sql": "SELECT COUNT(*) FROM orders",
  "candidate_index": 0,
  "generated_tokens": 31,
  "generation_elapsed_ms": 842
}
```

Questions, prompts, gold SQL, database rows, API keys and chat history are not
written to this file by project code.

The CLI durably appends and `fsync`s one candidate record immediately after each
successful completion, before requesting the next case. If a later request
times out or the model service fails, the completed prefix remains valid and
`--resume` generates only missing `candidate_index=0` cases. This does not
silently skip failures or alter the native case order; it only avoids losing
already generated external records during a long batch.

## First Native Smoke Run

On 2026-08-24, the frozen contract was run on the first 20 native cases of
`spider-1.0-kaggle-mirror-v1-2020-01-27` (`spider_dev:00000` through
`spider_dev:00019`). The immutable model/prompt configuration in the manifest
was unchanged. Prediction JSONL and diagnostic JSON remain outside Git at
`/disk2/gengnan/data-analysis-agent-data/experiments/spider-qwen25coder3b-v1-20260824/`.

| Measurement | Observed value |
| --- | --- |
| Generated candidates | 20 / 20 |
| Local secure SQLite execution | 15 / 20 |
| AST policy rejections | 0 / 20 |
| SQLite execution errors | 5 / 20 |
| Timeouts | 0 / 20 |
| Error category | 5 `no_such_column` (`sqlite_operational_error`) |
| Model generation elapsed time | 13,123 ms total; 656.1 ms mean; 483.5 ms median; 4,011 ms max |
| Generated tokens | 594 total; 29.7 mean; 83 max; 0 unknown |
| Prediction JSONL SHA-256 | `3ad1eb0db1bb1f19a7f436c3d66884e009b0245d21a0602bec76e147701487c7` |
| Diagnostic JSON SHA-256 | `92644ca43cffc54c22e094530777fc5ed18c18365c974e0dd405b6d007b8ffbe` |

This is **not** 15/1,034 benchmark accuracy: the Adapter reports the other
1,014 development cases as `missing_prediction`, because this is an intentional
20-case smoke batch. It is also not execution accuracy, exact match, Test Suite
Accuracy or a semantic-correctness measurement. The 15 successful executions
only establish that those candidates passed the SQLite read-only policy and
could execute. The five `no_such_column` failures are a concrete schema-linking
baseline error signal for later SFT evaluation; they do not justify a dataset-
specific prompt patch or a change to the production PostgreSQL safety path.

The existing Ollama service at `127.0.0.1:11434` automatically placed this small
inference run primarily on physical `nvidia-smi` GPU `0` (logical CUDA `2`, RTX
3090) and partly on physical GPU `2` (logical CUDA `0`, RTX 4090). This was
observed after launch, not selected by the experiment. Future training runs must
use an isolated service or process with explicit device binding and record the
same logical/physical mapping.

## Full Native Development Baseline

On 2026-08-24, the same frozen model, prompt and decode configuration generated
one primary candidate for all 1,034 native Spider development cases. The run used
a temporary, project-owned Ollama service on `127.0.0.1:11437`, started after an
occupancy check with `CUDA_VISIBLE_DEVICES=1`. Under the repository mapping this
is logical CUDA `1` and physical `nvidia-smi` GPU `3` (RTX 4090). The service
reused the existing local model store and was stopped after the run; no
model weight was copied, downloaded or committed.

All prediction and diagnostic artifacts remain outside Git at
`/disk2/gengnan/data-analysis-agent-data/experiments/spider-qwen25coder3b-full-v1-20260824/`.
The prediction JSONL has exactly 1,034 records, stable native order, unique case
IDs and exactly one `candidate_index=0` per case. The generated candidates were
then passed unchanged into the isolated Adapter with its existing single-query,
read-only, 1,000-row and 5,000-ms execution limits.

| Measurement | Observed value |
| --- | --- |
| Generated candidates | 1,034 / 1,034 |
| Local secure SQLite execution | 884 / 1,034 |
| AST policy rejections | 0 / 1,034 |
| SQLite execution errors | 150 / 1,034 |
| Timeouts | 0 / 1,034 |
| Execution-error categories | 138 `no_such_column`; 5 `no_such_table`; 5 `ambiguous_column`; 2 `sqlite_misuse` |
| Model generation elapsed time | 466,667 ms total; 451.3 ms mean; 429.0 ms median; 2,951 ms max; 0 unknown |
| Generated tokens | 32,662 total; 31.6 mean; 28.0 median; 125 max; 0 unknown |
| Prediction JSONL SHA-256 | `9459b8262d62983c6e40c92b8bcc3be756bc4f2afd19562c8c9b1b5f06d572b0` |
| Diagnostic JSON SHA-256 | `43561e69f6420bfc9f12a5d4822b65e0a96ff838e115eba8d5511e1e5f237744` |

`884/1,034` is an operational execution diagnostic only. It is not Spider
Execution Accuracy, exact match, Test Suite Accuracy or semantic accuracy:
the Adapter does not read gold SQL or gold answers, and it only establishes that
model SQL passed the local read-only policy and executed. The 138 nonexistent-
column failures are a broad pre-training schema-linking signal, not evidence for
a dataset-specific prompt patch or a production PostgreSQL policy change.

The current data artifact remains the third-party 2020-01 Spider mirror, which
predates the official 2020-08 corrections. The independently distributed official
Test Suite database archive and extracted tree are now hashed and structurally
checked against this mirror, but its independent terms and current official
release comparability are still not verified. The full candidate coverage now
satisfies the bridge's coverage guard; the resulting run is recorded below as an
internal reference only.

## Internal Test Suite Reference Run

After the Test Suite archive was downloaded through the temporary local-VPN SSH
reverse proxy, the pinned unmodified evaluator was run in a detached `screen`
session against the complete 1,034-case frozen predictions. The bridge folded
formatting whitespace in 41 multi-line model SQL candidates into one physical
line while preserving quoted literals; it rejected line comments rather than
changing their semantics. The evaluator ran with `PYTHONDONTWRITEBYTECODE=1`
so its pinned external checkout remained clean.

| Measurement | Observed value |
| --- | --- |
| Evaluated cases | 1,034 / 1,034 |
| Evaluator commit | `e97acc546ecbee8fa27fa8dbf025ef61493a876c` |
| Evaluation type | `exec` |
| Easy / medium / hard / extra | `0.820 / 0.620 / 0.437 / 0.300` |
| All execution diagnostic | `0.585` |
| Return code | `0` |
| External run directory | `/disk2/gengnan/data-analysis-agent-data/experiments/spider-qwen25coder3b-test-suite-v3-20260824/` |
| Gold SHA-256 | `d9b457826163d062f2efc3c8971d988d58dae9a8817ed76ada9f2f1df2a01cac` |
| Prediction SHA-256 | `ee9de5ddcf464151cd05a17df30b7bbb504df34cf6c953ade94f222129ada8c7` |
| Raw output SHA-256 | `7b4d4996a08591648de0cdbad00ff004adcb53a3351146ae6672bfe7046f6d03` |

This is a **full internal reference run**, not a current official Spider
leaderboard result: the selected Kaggle mirror predates the official correction
release and the independent Test Suite asset terms/release identity are not
separately verified. It is nevertheless a fixed baseline for comparing later
local SFT/QLoRA experiments under the same data, evaluator, prompt and
prediction contract. It must not be confused with the SQLite Adapter's
`884/1,034` operational execution diagnostic.

The original ordered smoke command is retained below for a smaller reproducible
run; it was completed before the full batch above:

```bash
/disk2/gengnan/conda_envs/data-analysis-agent/bin/python \
  scripts/run_frozen_sqlite_baseline.py \
  --dataset spider_dev \
  --cases /disk2/gengnan/data-analysis-agent-data/text-to-sql/spider/<release>/dev.json \
  --database-root /disk2/gengnan/data-analysis-agent-data/text-to-sql/spider/<release>/database \
  --model qwen2.5-coder:3b \
  --model-version f72c60cabf6237b07f6e632b2c48d533cef25eda2efbd34bed21c5e9c01e6225 \
  --max-cases 20 \
  --output /disk2/gengnan/data-analysis-agent-data/experiments/spider-qwen25coder3b-v1/predictions.jsonl
```

Then execute the output through the Adapter:

```bash
/disk2/gengnan/conda_envs/data-analysis-agent/bin/python \
  scripts/run_sqlite_benchmark.py \
  --dataset spider_dev \
  --cases /disk2/gengnan/data-analysis-agent-data/text-to-sql/spider/<release>/dev.json \
  --database-root /disk2/gengnan/data-analysis-agent-data/text-to-sql/spider/<release>/database \
  --predictions /disk2/gengnan/data-analysis-agent-data/experiments/spider-qwen25coder3b-v1/predictions.jsonl \
  --dataset-version spider-<release>-dev \
  --model-id qwen2.5-coder-3b-ollama-q4km \
  --model-version f72c60cabf6237b07f6e632b2c48d533cef25eda2efbd34bed21c5e9c01e6225 \
  --prompt-version sqlite-frozen-baseline-v1 \
  --output /disk2/gengnan/data-analysis-agent-data/experiments/spider-qwen25coder3b-v1/diagnostics.json
```

The report's `executed_candidates` is only a local operational diagnostic. It is
not Spider Execution Accuracy or Test Suite Accuracy. The current official
Spider task page identifies Test Suite Accuracy as its leaderboard metric.
It may be reported only after the exact unmodified official test-suite evaluator,
its required assets, version and input format have been separately verified and
its generated artifact is attached to the report.

## Official Test Suite Boundary

The official evaluator code is pinned outside Git at
`/disk2/gengnan/data-analysis-agent-data/text-to-sql/sources/test-suite-sql-eval`,
commit `e97acc546ecbee8fa27fa8dbf025ef61493a876c` (Apache-2.0 code). Its README
requires a **separate** Google Drive package of generated test-suite databases.
The code license does not establish terms for those database assets, so they are
not downloaded or run yet. Their terms, release identity, archive and extracted
tree hashes, and compatibility with this 2020-01 Spider mirror remain a required
gate. In particular, the test suites likely target the later official correction
release, while the current mirror predates it.

`scripts/run_official_spider_test_suite.py` is a narrow bridge to the unmodified
official `evaluation.py`, intended for a future full-coverage run. It verifies
the exact evaluator commit and a clean worktree, requires exactly one
`candidate_index=0` for every native case in original order, validates that all
referenced test-suite SQLite files exist, and places gold SQL, prediction text,
raw evaluator output and evidence outside Git. It invokes only `--etype exec`
and does not parse, recalculate or reinterpret the official score.

The full-coverage requirement is essential: the upstream evaluator treats the
Spider development file as one session and iterates with `zip(predictions,
gold)`. A short prediction file would otherwise silently receive a denominator
equal to its own length. The present 20-case smoke was independently verified
to fail before input preparation or evaluator invocation. The completed
1,034-case run is the only full-coverage result currently retained, and it
remains an internal reference, not a current official leaderboard score.

## Next Research Gate

The full native-data baseline is now frozen. Retain its model digest, prompt,
dataset checksum and external prediction hash unchanged. The next implementation
gate is a separate Python/Conda QLoRA/SFT environment with the existing holdout
isolation protocol; every later training run must compare with this frozen
baseline without altering the PostgreSQL safety/runtime contracts. This does not
remove the independent Test Suite asset gate, so no official Spider score may be
claimed unless its terms and release compatibility are separately verified.
