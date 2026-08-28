# Offline Post-Training Scripts

This directory is the canonical implementation layout for the offline
Text-to-SQL post-training subsystem. It is intentionally separate from the
production Vanna/PostgreSQL runtime in `src/data_analysis_agent/`.

| Directory | Responsibility | Examples |
| --- | --- | --- |
| `data/` | Build train-only Spider candidates and schema-disjoint splits. | `build_spider_sft_candidates.py`, `split_post_training_candidates.py` |
| `training/` | Freeze a model, run SFT, validate an Adapter, verify artifacts. | `run_post_training_sft_smoke.py` |
| `inference/` | Generate Base or Adapter SQL candidates without benchmark gold SQL. | `generate_post_training_text_to_sql.py` |
| `evaluation/` | Run SQLite, Test Suite, denotation and protected Olist transfer diagnostics. | `run_sqlite_benchmark.py`, `run_olist_candidate_sql_evaluation.py` |
| `launchers/` | Explicit `screen` launchers for approved long-running jobs. | `start_post_training_spider_sft_v2_screen.sh` |

The root-level names in `scripts/` are compatibility entry points. New code,
new documentation and new imports should use this capability-based layout. New
CLI invocations should use module mode, for example:

```bash
python -m scripts.post_training.data.split_post_training_candidates --help
```

The candidate generator remains an offline proposal component. It never owns
production database permissions, SQL policy, metric semantics or result
validation.
