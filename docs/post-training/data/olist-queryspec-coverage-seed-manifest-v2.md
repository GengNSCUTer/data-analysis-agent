# Olist QuerySpec Coverage Seed Manifest v2

## Purpose

`data/fixtures/olist_queryspec_coverage_seeds_v2.jsonl` is a small, static
coverage manifest for constructing the Olist Pilot v1. It is a structural
施工图 (construction plan), not a training dataset and not evidence that the
generated SQL is business-correct.

Each row freezes the query shape, metric IDs, dimension, time contract and the
approved join program. It deliberately contains no natural-language question,
runtime prompt, generated result, or user-provided SQL.

## Frozen coverage

| Split | Families | Scope |
| --- | ---: | --- |
| train | 24 | scalar, state and category queries plus purchase time series |
| validation | 8 | category and multi-metric purchase time series |
| in_domain_test | 8 | daily/monthly/quarterly time series and multi-metric state/review queries |
| **Total** | **40** | **40 unique semantic families** |

All ten frozen Olist metrics are covered:
`gmv`, `paid_order_count`, `average_delivery_days`,
`positive_review_rate`, `item_count`, `average_order_value`,
`average_review_score`, `on_time_delivery_rate`, `cancellation_rate`, and
`freight_amount`.

All four supported result shapes are represented: `scalar`, `state_grouped`,
`category_grouped`, and `time_series`.

## Identity and split boundary

- A **family** is the semantic query program after removing date endpoints.
  Changing only the date range does not create a new family.
- A **QuerySpec** is the fully pinned structural query, including its time
  range and workspace snapshot.
- A **sql program** is the approved join path used by the renderer. Several
  different families may legitimately share one join path.

The v2 split policy isolates `family_id`, `query_spec_id`, and canonical SQL
hashes across splits. It permits a `join_program_id` to appear in more than one
split because a shared physical join path is not itself a semantic answer and
does not expose a held-out question. The materializer records this overlap in
`sql_program_split_overlap`.

The old v1 fixture and its strict split policy remain unchanged for historical
evidence.

## What remains before Pilot v1 SFT

These seeds still require, for every family:

1. A reviewed Chinese natural-language variant (and an optional second
   paraphrase), produced through the production runtime prompt builder.
2. Gold SQL checks through SqlPolicy, the PostgreSQL reader role,
   ResultContract, ResultValidator, and business-meaning review.
3. Prompt/token auditing with a 1536-token maximum; prompt labels are `-100`,
   SQL plus EOS are trainable targets, and over-length rows go to an exclusion
   manifest.
4. A final split audit covering near-duplicates, family/query-spec/SQL
   isolation, and train/validation/test coverage.

Until those checks pass, this file must not be described as a finished SFT
train/validation/test dataset.
