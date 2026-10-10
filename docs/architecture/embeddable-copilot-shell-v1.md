---
title: EmbeddableCopilotShell v1
status: frozen
date: 2026-09-21
---

# EmbeddableCopilotShell v1

## Scope

`/embedded-copilot` is the only product page. It shows the workspace, conversation
list, chat, evidence, new analysis, and clear-context actions. Demo role
switching and the audit list stay behind the `管理` toggle. `/embedded-demo`
redirects here.

The shell contains:

- the current server-derived workspace label;
- the conversation list, history restore, and delete;
- the Vanna chat surface for questions, clarifications, tables and charts;
- `新分析`, which starts a new conversation and therefore clears active analysis
  state without deleting historical conversations;
- `清空分析上下文`, which keeps the same conversation and its chat history, but
  drops the active metric, time, dimension and result reference;
- a small `查看依据` panel showing server-provided dataset/metric versions and
  the read-only execution boundary;
- a login gate until a local session or an accepted embed token exists;
- `下载完整结果`, which fetches the validated CSV for the current conversation;
- a `管理` toggle for the demo role switch, diagnostics, embed-token issue, and recent audit list;
- an edit control on the latest user question. Submitting it deletes that
  question and every later message from the conversation store, then asks the
  edited question again.

The user view does not show demo role switching or the audit list until `管理`
is opened. Those controls are a signed demo session, not production login.

## Identity and workspace boundary

The browser does not submit `user_id`, role, SQL, DOM content or arbitrary
workspace identifiers. The page sends the normal same-origin chat requests;
the server resolves identity from its session and the workspace label comes
from `/api/project/runtime-contract`. This endpoint is informational only and
does not authorize SQL.

The current implementation is same-origin Olist Demo infrastructure. It is an
embedded product-shell contract, not a claim that the demo cookie is production
authentication. A real host still needs to provide authenticated session and
workspace bootstrap through the M1 contract.

## Deliberate non-goals

- no TheLook connection to the online default page;
- cross-origin embedding uses a 5-minute token bound to an allowlisted origin, sent by `postMessage`; the iframe cannot submit `user_id`;
- full CSV download reads the owner-scoped validated artifact after an integrity check and writes `app.export_audits`; the generic preview export button stays off;
- no frontend rewrite of the Vanna chat component.

## Acceptance

The minimum browser contract is:

1. `/embedded-copilot` renders the chat shell and workspace label;
2. Demo role/audit controls stay hidden until `管理` is opened;
3. `查看依据` loads only version and read-only boundary metadata;
4. `新分析` creates a new conversation without deleting old history;
5. desktop and 390px mobile layouts have no horizontal overflow;
6. `/embedded-demo` redirects to `/embedded-copilot`.
