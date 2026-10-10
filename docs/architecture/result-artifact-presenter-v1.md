---
title: ResultArtifact Presenter Contract v1
status: frozen
date: 2026-09-21
---

# ResultArtifact Presenter Contract v1

## Purpose

This contract describes the boundary between a validated server result and a
table shown in the embedded copilot. It is a presentation contract only. It
does not change SQL generation, PostgreSQL reader enforcement, ResultValidator,
or the 1,000-row execution cap.

## Required fields

`ResultPresentation` contains:

- `total_row_count`: rows in the validated result returned by execution;
- `preview_row_count`: rows actually sent to the table component;
- `display_truncated`: exactly `preview_row_count < total_row_count`;
- `columns`: server-owned result column identifiers in display order;
- `column_labels`: bounded UI labels for those columns;
- `column_units`: optional bounded units, such as `BRL` or `天`;
- `workspace_id`, `dataset_version`, `metric_version`: the scope and version
  references used for the result;
- `audit_ref`: an opaque request/audit reference, never SQL text;
- `artifact_id`: an optional opaque external artifact reference.

The component receives only preview rows. Hidden rows, raw SQL, and full CSV
payloads are not part of this DTO or sent to the model.

## Export policy

Both `preview_export_allowed` and `full_export_allowed` are `false` in v1.
The generic table export control is disabled because it has no authenticated,
workspace-scoped, audited download route. The existing external
`ResultArtifactStore` remains an internal immutable artifact store; exposing a
full download requires a separate permission and audit contract.

## Display examples

For a validated 250-row result with a 200-row preview:

```json
{
  "total_row_count": 250,
  "preview_row_count": 200,
  "display_truncated": true,
  "preview_export_allowed": false,
  "full_export_allowed": false
}
```

The UI description must communicate `预览 200 / 共 250 行`. For a 20-row
result, preview and total are both 20 and `display_truncated` is false.

## Acceptance boundary

This contract is accepted when focused tests prove:

1. 20, 200, and 250-row results preserve total/preview semantics;
2. labels and units remain display-only and do not mutate trusted values;
3. workspace/version/audit references are present;
4. generic export is disabled;
5. no hidden row or raw SQL is present in presenter metadata.
