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
license does not establish terms for the raw `spider.zip` dataset.  The current
official README points to the task page and requests citation, yet it does not
state a standalone raw-data license.  Therefore `data/manifest/datasets.yaml`
and the experiment manifest intentionally remain `pending` for raw Spider data.

Before downloading data, record all of the following in the manifest and an
external acquisition log:

1. An explicit raw-data license/terms statement from the selected release.
2. Release name, source URL, retrieval date and source-repository/evaluator
   commit.
3. SHA-256 of the downloaded archive and the extracted `dev.json`/database tree.
4. Storage outside Git at
   `/disk2/gengnan/data-analysis-agent-data/text-to-sql/spider/<release>`.

The baseline code can be tested against synthetic SQLite fixtures without this
data.  It must not be run against a downloaded Spider release until the gate is
complete.

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

After the data gate is complete, run a small ordered smoke batch first:

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

The report's `executed_candidates` is only a local operational diagnostic.  It
is not Spider Execution Accuracy or Test Suite Accuracy.  Those metrics may be
reported only after the exact official evaluator, version and input format have
been separately verified and its generated artifact is attached to the report.

## Next Research Gate

Once the first legal native-data baseline is recorded, retain its model digest,
prompt, dataset checksum and predictions unchanged.  Only then create a
separate Python/conda training environment for QLoRA/SFT; that training run
must use the existing holdout isolation protocol and compare against this
frozen baseline without altering the PostgreSQL safety/runtime contracts.
