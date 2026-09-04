---
goal: Admit a six-record Olist QuerySpec/Gold SQL batch through protected-summary evidence, trusted execution, and model-assisted semantic review
version: 1.0
date_created: 2026-09-04
owner: Data Analysis Agent
status: Completed for the six-record admission batch
tags: [feature, post-training, gold-sql, protected-holdout, semantic-review]
---

# Olist Small-Batch Gold Admission v1

## 1. Goal

Run one deliberately small, externally stored admission batch before any Prompt,
JSONL, tokenizer, or training work. The batch proves that the structural
QuerySpec/Gold pipeline can consume a real protected-family summary with its
evidence, and that every admitted Gold SQL can pass the existing trusted
PostgreSQL boundary and a bounded semantic review.

## 2. Scope

The batch contains at most six static coverage seeds that do not collide with
the protected QuerySpec families mapped in the restricted review. It covers
scalar, state-grouped, and time-series forms across train, validation, and
in-domain-test labels. These labels only exercise structural isolation; this is
not a train/validation/test dataset and no natural-language query is created.

The materializer must require the protected summary's evidence sidecar and
verify its version, current WorkspacePin, family count, and summary SHA-256.
All materialized and execution artifacts remain under the external project data
root. Git receives only code, contracts, tests, and a redacted aggregate report.

## 3. Non-goals

- Do not create Chinese questions, runtime Prompts, SFT JSONL, tokenizer input,
  split audit, or model-training jobs.
- Do not expose or commit protected case text, case IDs, family IDs, raw SQL,
  database rows, credentials, or provider responses.
- Do not let the LLM execute SQL, change a QuerySpec, approve policy violations,
  or replace human metric review.

## 4. Inputs and Outputs

Inputs are an external approved protected family-ID file, its exported
fingerprint summary/evidence, the committed static seed fixture, the frozen
WorkspacePin, PostgreSQL reader-role access, and a SiliconFlow API key.

Outputs are external protected-summary artifacts, external materialized
QuerySpec/Gold records, external per-record trusted-execution and LLM-review
artifacts, and a redacted aggregate admission report. The Git-side admission
script reports only hashes, counts, status codes, and version pins.

## 5. Invariants

1. The exporter never reads the protected holdout; restricted review is the
   only stage that reads it and emits approved structural family IDs.
2. Materialization fails closed unless summary and evidence agree exactly.
3. SQL must pass `SqlPolicy`, run only through `daa_analytics_reader`, and pass
   `ResultValidator` under the QuerySpec-derived result contract.
4. DeepSeek receives only bounded public-data review context and returns a
   structured advisory verdict. Invalid/unavailable output is
   `needs_human_review`, never an approval.
5. A successful small batch proves only this frozen batch passed its gates; it
   does not prove model quality, corpus readiness, or business generalization.

## 6. Acceptance Evidence

- Unit tests cover summary-evidence mismatch, workspace drift, and valid binding.
- The external manifest records six-or-fewer accepted structural rows and no
  protected collision.
- Every accepted row has Policy allow, reader-role execution, valid result
  contract, and an LLM verdict or explicit `needs_human_review` failure state.
- The redacted aggregate report can be inspected without recovering protected
  or raw result content.

## 7. 2026-09-04 Result

Restricted review mapped the protected suite's currently QuerySpec-expressible
data-query families to 17 approved external family IDs. The exporter produced
an external fingerprint summary with SHA-256
`a75012b9c4e328b00c9d0586c2107b25843e0e54636e209ee4f9b54860c3e855` and
evidence SHA-256
`019fc382ab4fa11c8469e8b15a43d85c66952a9a671a153dcc154a1c13d3dfc9`.

Six non-colliding coverage seeds were materialized externally. All six passed
the SQL AST Policy, `daa_analytics_reader` execution, QuerySpec-derived
ResultValidator contract, and DeepSeek-V4-Flash's advisory structured semantic
review. The redacted aggregate reports `6 admitted`, `0 needs_human_review`,
and `0 rejected`; detailed SQL, validated result summaries, and review prose
remain external. The DeepSeek review is model-assisted, not a human sign-off;
before expanding the corpus, a future sample must still receive human metric
and grain review. This is batch admission evidence only, not a completed
corpus, natural-language dataset, training result, or model-quality claim.
