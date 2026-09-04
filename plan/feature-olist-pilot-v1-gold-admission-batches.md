---
goal: Admit the 40-family Olist Pilot v1 structural materialization in explicit, auditable batches of at most six Gold rows
version: 1.0
date_created: 2026-09-04
owner: Data Analysis Agent
status: In progress
tags: [feature, post-training, gold-sql, admission, batch-selection]
---

# Olist Pilot v1 Gold Admission Batches

## Goal

Extend the existing six-row Olist Gold admission gate so that it can consume a
larger, already-verified structural materialization without weakening the
per-batch review budget. Each run must select an explicit, bounded set of seed
IDs from the full materialization and preserve evidence of both the full source
and the selected subset.

## Non-goals

- Do not increase an admission batch above six rows.
- Do not create Chinese questions, runtime prompts, SFT JSONL, tokenizer input,
  training jobs, or Base/Adapter evaluation.
- Do not bypass SqlPolicy, the PostgreSQL reader role, ResultValidator, the
  advisory reviewer, protected-family checks, or human metric/grain review.
- Do not commit raw SQL, query results, protected cases, provider responses, or
  credentials.

## Inputs and Outputs

Input is an external materialization directory whose manifest, QuerySpec JSONL
and Gold SQL JSONL all validate as a complete unit, plus one to six explicit
seed IDs. The output remains a new external admission directory. Its aggregate
must bind to the full materialization manifest and record the exact requested
and selected seed IDs.

## Invariants

1. Validate the whole source materialization before selecting a subset; a valid
   selected row cannot hide corruption in an unselected row.
2. A selection is one to six unique IDs, each present exactly once in the
   source. Unknown, duplicate, empty, or oversized selections fail closed.
3. Selection preserves source materialization order so reruns are deterministic
   even when command-line seed IDs are passed in another order.
4. Omitting selection preserves the historical small-batch behavior: a source
   directory itself must contain at most six rows.
5. One selected batch proves only the selected frozen Gold rows passed the
   gates. It is neither final SFT data nor a model-quality result.

## Acceptance Evidence

- Unit tests cover valid subset selection, source-order determinism, unknown or
  duplicate IDs, over-limit selections, and the historical unselected limit.
- The admission aggregate records full-source row count and selection evidence.
- No database, model provider, tokenizer, GPU, or natural-language prompt is
  touched while implementing this selection boundary.
