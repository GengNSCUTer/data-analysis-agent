# SQLite Benchmark Adapter

## 1. Purpose and boundary

This adapter measures a Text-to-SQL candidate generator on native SQLite
benchmark databases such as BIRD and Spider. It exists for reproducible model
research and paper-comparable evaluation. It is **not** a second production
database path:

- The embedded Vanna Agent and trusted Olist workflow remain PostgreSQL-only.
- `WorkspaceProfile`, `SqlPolicy`, `SecurePostgresRunner`, reader roles,
  `ResultContract`, `ChartContract`, attribution rules, audit and request
  budgets remain the authoritative production controls.
- SQLite execution is offline diagnostic evidence. It must not be described as
  business metric correctness, PostgreSQL compatibility, permission validation,
  or official execution accuracy (EX).

The adapter consumes SQL predictions generated elsewhere. It neither downloads
benchmark data nor calls an LLM; model serving and post-training environments
will be built in later iterations.

## 2. Dataset layout and provenance

Raw benchmark releases remain outside Git. The planned local layout is:

```text
/disk2/gengnan/data-analysis-agent-data/text-to-sql/
├── bird/<release>/
│   ├── dev.json
│   └── dev_databases/<db_id>/<db_id>.sqlite
└── spider/<release>/
    ├── dev.json
    └── database/<db_id>/<db_id>.sqlite
```

Before downloading a release, update
[`data/manifest/datasets.yaml`](../data/manifest/datasets.yaml) with its exact
release identifier, data license/terms, download date, archive checksum and
storage path. A repository code license is not assumed to be a dataset license.
Do not commit benchmark databases, raw `dev.json`, model downloads, prediction
inputs, evaluation reports or result rows.

The normalizers expect native `dev.json` list items containing `db_id`:

- `bird_dev` maps item index `0` to `bird_dev:00000` and database path
  `<db_id>/<db_id>.sqlite` below the supplied BIRD `dev_databases` root.
- `spider_dev` maps item index `0` to `spider_dev:00000` and the same database
  path shape below the supplied Spider `database` root.

Question text, BIRD evidence and gold SQL are read only to preserve native list
order. They do not enter the normalized case records or generated report.

## 3. Read-only execution contract

`ReadOnlySqliteExecutor` applies all of the following before recording an
outcome:

1. Parses candidate SQL with `sqlglot` using the SQLite dialect.
2. Allows exactly one `SELECT` or read-only `WITH ... SELECT` query; rejects
   DDL, DML, transaction statements, `ATTACH`, `DETACH`, direct `PRAGMA`,
   `load_extension`, `readfile`, `writefile` and `pragma_*` table functions.
3. Adds or caps a literal `LIMIT` to the configured `max_rows` value.
4. Opens the database with SQLite `mode=ro`, disables extension loading and sets
   `query_only` before model SQL is executed.
5. Registers a SQLite authorizer that denies write/schema/attach/pragma actions
   even if a parser rule were to regress.
6. Uses SQLite's progress handler for a per-statement timeout.

Each candidate yields one of `executed`, `policy_rejected`, `execution_error`,
`timeout`, or `missing_prediction`. An execution result includes only row count
and column names, never result rows.

## 4. Prediction and report contracts

Predictions are an external JSONL file. Every non-empty line contains one
candidate:

```json
{
  "case_id": "bird_dev:00000",
  "candidate_sql": "SELECT COUNT(*) FROM accounts",
  "candidate_index": 0,
  "generated_tokens": 73,
  "generation_elapsed_ms": 412
}
```

`candidate_index`, `generated_tokens` and `generation_elapsed_ms` are optional.
Multiple candidates may share a `case_id`, but every `(case_id, candidate_index)`
pair must be unique. Candidate SQL, errors and timings are written only to the
ignored local report because they are evaluation artifacts, not versioned source
data.

Every report records:

- dataset/dataset-version, `sqlite` dialect, model ID/version and prompt version;
- case/database IDs, candidate index/count, generation token/timing telemetry;
- original and normalized SQL, policy/execution status, row limit, row count,
  column names, error category/message and execution elapsed time;
- a separate official-evaluation block.

Reports must not contain benchmark question text, gold SQL, external evidence
text, raw result rows, credentials or model secrets.

## 5. Running local diagnostics

Once a benchmark release and a prediction file exist outside the repository:

```bash
/disk2/gengnan/conda_envs/data-analysis-agent/bin/python \
  scripts/run_sqlite_benchmark.py \
  --dataset bird_dev \
  --cases /disk2/gengnan/data-analysis-agent-data/text-to-sql/bird/<release>/dev.json \
  --database-root /disk2/gengnan/data-analysis-agent-data/text-to-sql/bird/<release>/dev_databases \
  --predictions /disk2/gengnan/data-analysis-agent-data/experiments/<run>/predictions.jsonl \
  --dataset-version bird-<release>-dev \
  --model-id <model-id> \
  --model-version <model-or-adapter-revision> \
  --prompt-version <prompt-revision> \
  --statement-timeout-ms 5000 \
  --max-rows 1000 \
  --output evals/reports/sqlite-benchmark/<run>.json
```

For Spider, use `--dataset spider_dev`, its native `dev.json`, and its
`database` directory. Exit code `2` means invalid input/configuration. Candidate
SQL failures do not stop the run; they are expected evaluation observations and
are recorded in the report.

## 6. Official EX boundary

The runner deliberately does not implement or infer BIRD/Spider execution
accuracy. After running the unmodified official evaluator for the exact native
release and prediction format it expects, attach a local JSON summary:

```json
{
  "dataset_id": "bird_dev",
  "evaluator_name": "bird-official-evaluator",
  "evaluator_version": "<pinned-release-or-commit>",
  "execution_accuracy": 0.0,
  "evaluated_cases": 1534,
  "source": "local official evaluator artifact path or run identifier"
}
```

Pass it with `--official-ex <path>`. The adapter validates that its dataset ID
matches the experiment. Without this artifact, the report states `not_run`; its
local `executed_candidates` count must never be reported as official EX.

## 7. Next milestones

1. Verify BIRD/Spider terms, then download one fixed release outside Git with
   checksums and a completed manifest entry.
2. Add a pinned, native official-evaluator invocation adapter after validating
   its exact input/output contract for that release.
3. Run a frozen small-model baseline, then use the existing post-training data
   protocol and holdout isolation before any QLoRA/SFT experiment.
