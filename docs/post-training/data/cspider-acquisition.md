# CSpider Full Release Acquisition

## Scope

This record covers only the local acquisition and structural preflight of the
official CSpider full release. It does not construct SFT records, train an
adapter, run generation, or claim Text-to-SQL quality.

## Source And Local Layout

- Task site: <https://taolusi.github.io/CSpider-explorer/>
- License declared by the task site: `CC BY-SA 4.0`.
- Release root: `/disk2/gengnan/data-analysis-agent-data/text-to-sql/cspider/cspider-1.0-official-2026-09-01/`.
- Archive: `archives/full_CSpider.zip`.
- Archive SHA-256: `edea769706e91bd71741e61de30c41ab8da97b365e15eb492c02ed281f3ebaf8`.
- Extracted files and machine-readable evidence: `extracted/` and `extracted/acquisition-manifest.json`.

The archive is not copied into the repository. The extracted directory is
created atomically only after member-path validation and structural checks pass.

## Verified Split Contract

| Official split | Intended role | Records | Schemas | SQLite root |
| --- | --- | ---: | ---: | --- |
| `train.json` | Parameter updates only | 8,659 | 146 | `database/` |
| `dev.json` | Validation only | 1,034 | 20 | `database/` |
| `test_data/test.json` | Final evaluation only | 2,147 | 40 | `test_database/` |

The preflight verified that the three `db_id` sets have no overlap. It also
opened all 166 train/dev databases and all 40 test databases through SQLite
read-only mode. `tables.json` covers train/dev `db_id` values; `tables_test.json`
covers test `db_id` values.

## Safety And Isolation

`scripts/post_training/data/acquire_cspider.py` rejects an archive when it has
an absolute path, `..`, a backslash path, a symbolic link, a duplicate member,
an unexpected root, missing required assets, malformed records, cross-split
schema overlap, incomplete table metadata, or a missing/unreadable SQLite file.

The test split and its gold SQL are never a training input. They must not enter
SFT JSONL construction, few-shot prompts, data synthesis, model selection, or
hyperparameter decisions. The dev split is not a parameter-update source.

## Verification

- `pytest -q tests/test_acquire_cspider.py`: `3 passed`.
- `ruff check scripts/post_training/data/acquire_cspider.py tests/test_acquire_cspider.py`: passed.
- `python -m py_compile scripts/post_training/data/acquire_cspider.py`: passed.
- Real archive preflight completed without a rejected member or missing asset.

## Next Boundary

The next approved task may build versioned CSpider train/dev/test JSONL inputs
and a split audit using this frozen release. It must preserve the roles above
and must not start model training.
