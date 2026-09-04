# Offline Post-Training Scripts

This directory is the canonical implementation layout for the offline
Text-to-SQL post-training subsystem. It is intentionally separate from the
production Vanna/PostgreSQL runtime in `src/data_analysis_agent/`.

| Directory | Responsibility | Examples |
| --- | --- | --- |
| `data/` | Build audited benchmark candidates/splits and Olist structural Gold intermediates. | `build_spider_sft_candidates.py`, `split_post_training_candidates.py`, `export_olist_protected_family_summary.py`, `materialize_olist_queryspecs.py` |
| `training/` | Freeze a model, run SFT, validate an Adapter, verify artifacts. | `run_post_training_sft_smoke.py` |
| `inference/` | Generate Base or Adapter SQL candidates without benchmark gold SQL. | `generate_post_training_text_to_sql.py` |
| `evaluation/` | Verify matching evidence, then run SQLite, Test Suite, denotation and protected Olist transfer diagnostics. | `verify_cspider_matching_generation.py`, `run_sqlite_benchmark.py` |
| `launchers/` | Explicit `screen` launchers for approved long-running jobs. | `start_post_training_cspider_base_adapter_evaluation_screen.sh` |

The root-level names in `scripts/` are compatibility entry points. New code,
new documentation and new imports should use this capability-based layout. New
CLI invocations should use module mode, for example:

```bash
python -m scripts.post_training.data.split_post_training_candidates --help
```

The candidate generator remains an offline proposal component. It never owns
production database permissions, SQL policy, metric semantics or result
validation.

`materialize_olist_queryspecs.py` is deliberately earlier than candidate-SQL
training: it accepts only structural coverage seeds and a protected family
summary, writes external QuerySpec/Gold intermediate artifacts atomically, and
does not read questions, execute SQL, build training JSONL, or load a model.

The committed `data/fixtures/olist_queryspec_coverage_seeds_v1.jsonl` is a small
review fixture for that structural input contract. It is not a training dataset,
is not accompanied by an in-repository protected summary, and must not be passed
to the materializer until the restricted protected-summary export boundary has
been separately approved.

`export_olist_protected_family_summary.py` implements that export boundary. It
does not read a holdout file: it accepts only an externally stored, manually
reviewed structural `family_id` list plus version/hash provenance, then writes a
non-reversible fingerprint summary and evidence sidecar outside the repository.
It is intentionally not run against the real holdout during its implementation
task. The evidence sidecar becomes mandatory materializer input only in the next
separate small-batch materialization task.
