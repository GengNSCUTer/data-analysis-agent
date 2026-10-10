---
goal: Freeze and verify a fail-closed host integration contract for the embedded Copilot
version: 1.0
date_created: 2026-09-21
last_updated: 2026-09-21
owner: Data Analysis Agent
status: Complete
tags: [feature, runtime, embedded-copilot, security, m1]
---

# M1：Copilot Host Contract

## Goal

Establish the smallest production-shaped boundary between an embedding host and
the existing trusted Olist demo. This task adds a bootstrap contract only; it
does not migrate conversations, change the SQL pipeline, add a frontend shell,
or claim that the signed demo session is production SSO.

## Contract

The browser may submit only three bounded presentation hints:

| Field | Meaning | Limit |
| --- | --- | --- |
| `surface_id` | Which trusted host surface mounted the Copilot | ASCII allowlist, 1--64 chars |
| `requested_workspace_id` | A request to use one registered workspace, not authority to enter it | optional opaque ID, 1--128 chars |
| `selected_entity_refs` | Opaque host-selected entity handles | unique, max 16, 1--128 chars each |

The browser cannot submit `user_id`, role, organization membership, raw DOM,
URL parameters, SQL, metric definitions or dataset/version pins. `subject_id`,
role, workspace membership and the dataset/metric/catalog/policy versions are
derived by the server from the resolved user and server-side
`WorkspaceAccessRegistry`.

`POST /api/copilot/bootstrap` returns a `CopilotBootstrap` with a bounded
`AnalysisSurfaceContext` and `ConversationScope`. It is same-origin only in
this release. The current registry grants the two signed demo identities access
to Olist only; it intentionally does not expose TheLook in the online product.

## Implementation

| Task | Result | Status |
| --- | --- | --- |
| TASK-001 | Add Pydantic contracts and a server-side workspace access registry in `src/data_analysis_agent/copilot_contract.py`. | Complete |
| TASK-002 | Add the minimal same-origin `/api/copilot/bootstrap` route to `examples/trusted_olist_web_demo.py`; preserve the existing demo session endpoint. | Complete |
| TASK-003 | Cover valid bootstrap, client authority injection, malformed host fields, unknown workspace and unknown subject with focused tests. | Complete |

## Acceptance evidence

`tests/test_copilot_contract.py`, `tests/test_demo_session.py` and
`tests/test_workspace_profile.py` pass together in the runtime Conda
environment: `19 passed` on 2026-09-21. The tests prove that request-body
identity/role/SQL/DOM fields are rejected, and unauthorized workspace selection
fails closed. They do not prove cross-origin SSO, database workspace isolation,
or browser-shell behavior; those belong to M2 and M4.

## Explicit non-goals

- No browser-to-server authentication design beyond the existing signed demo
  cookie.
- No `workspace_id` or `state_version` persistence migration (M2).
- No ResultArtifact presenter/UI change (M3/M4).
- No modification to Router, Catalog, QueryPlan, SQL Policy, reader role,
  ResultValidator, model provider, training or evaluation data.
