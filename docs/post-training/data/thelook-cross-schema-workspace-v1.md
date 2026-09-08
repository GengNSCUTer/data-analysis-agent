# TheLook Cross-Schema Workspace v1

## Task Card

| Item | Decision |
| --- | --- |
| Goal | Prepare a second, relational e-commerce PostgreSQL workspace for a future **zero-shot cross-schema** Base versus Olist-LoRA comparison. |
| Non-goal | Do not add TheLook data, schema, prompts, questions, Gold SQL, or examples to Olist training, validation, prompt tuning, or Adapter selection. Do not run a model evaluation in this task. |
| Database | Dedicated local PostgreSQL database `thelook_analytics` on `127.0.0.1:35434`; it is separate from Olist database `data_analysis_agent`. |
| Source snapshot | `thelook-kaggle-mirror-v1-20260908`, external directory `/disk2/gengnan/data-analysis-agent-data/datasets/thelook/kaggle-mirror-v1-20260908/`. |
| Acceptance | Archive/file hashes, raw row counts and schema dump are recorded externally; isolated reader role can query only explicit safe views; source integrity audit passes. |

## Provenance Boundary

The intended upstream dataset is Google BigQuery Public Dataset
`bigquery-public-data.thelook_ecommerce`. This server had no Google Cloud credentials or
`bq` client, so it could not make an authenticated BigQuery export. The frozen asset is
therefore explicitly a **Kaggle mirror**, not a direct BigQuery export:

```text
Kaggle ref: mustafakeser4/looker-ecommerce-bigquery-dataset
Kaggle title: Looker Ecommerce BigQuery Dataset
Kaggle subtitle: CSV version of BigQuery Looker Ecommerce Dataset
Kaggle version: 1, published 2024-01-18
Published license field: Unknown
```

The archive SHA-256 is
`bbaa65297f205192ef6623afd6258aaaab64ae1973c5d30f70de2284c5c65888`.
The mirror is acceptable for the local engineering and zero-shot evaluation prototype,
but it must not be represented as an official BigQuery export or redistributed as a
licensed dataset. A later direct export may replace it only through a new snapshot ID,
full reload and matching audit.

## Isolated Data Boundary

```text
external raw archive / CSVs
    -> thelook_analytics.thelook_raw     (administrator only; import fidelity)
    -> thelook_analytics.analytics       (seven vetted reader views)
    -> daa_thelook_reader                (read-only, 5-second timeout)
```

The raw relation names are `distribution_centers`, `events`, `inventory_items`,
`order_items`, `orders`, `products`, and `users`. They are source-faithful tables.
The reader-visible `analytics` views have the same relation names but exclude direct
identifiers: first/last name, email, street address, postal code, IP address and precise
user coordinates. No privilege is granted on `thelook_raw`.

The snapshot has the following raw row counts:

| Table | Rows |
| --- | ---: |
| `distribution_centers` | 10 |
| `users` | 100,000 |
| `products` | 29,120 |
| `orders` | 125,226 |
| `order_items` | 181,759 |
| `inventory_items` | 490,705 |
| `events` | 2,431,963 |

The full file checksums, schema dump and aggregate audit live outside Git under
`.../kaggle-mirror-v1-20260908/audit/`. Raw CSV rows, schema dump and data artifacts
never enter the repository.

## Import and Audit Entrypoints

```bash
# Initial import; refuses any target other than thelook_analytics.
./infra/postgres/load_thelook_local.sh

# Generate snapshot manifest/schema dump after import.
./infra/postgres/audit_thelook_snapshot.sh

# Validate source cardinality, join integrity, timestamp coverage and reader boundary.
./infra/postgres/audit_thelook_schema.sh
```

The loader imports all seven raw CSVs in one transaction. It fails rather than filling
missing source values. The only source-format adaptation is `events.user_id`: the mirror
serializes non-null IDs as integral decimal values such as `35092.0`. Raw storage uses a
checked `NUMERIC(20,1)` and the reader view casts to `BIGINT`; the audit verifies both
zero non-integral values and zero non-null user-reference orphans.

## Completed Schema Audit

The frozen snapshot passed these gates:

- 7 raw tables and 7 reader-visible views exist.
- `daa_thelook_reader` has zero raw-table grants and exactly the seven safe-view grants.
- Raw direct access is denied; writes are denied by both permissions and its default
  read-only transaction.
- The events decimal-ID conversion has zero non-integral values and zero user orphans.
- `order_items` has zero missing `orders` references; every `orders.num_of_item` equals
  the loaded item count.
- Source time ranges span late 2018/early 2019 through January 2024 depending on the
  relation. Exact aggregate evidence is in the external `schema_audit.md`.

## Next Boundary

The next separate task is to define a TheLook-only Semantic Catalog and metric contracts
from these reader-visible relations. First establish a small set of comparable but
schema-specific metrics such as GMV, completed-order count, return rate, fulfillment
duration and average order value. Do not create QuerySpec families, natural-language
questions, Gold SQL, Prompt assets, or invoke Base/Adapter generation until that Catalog
contract is reviewed.
