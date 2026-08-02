---
goal: Deliver an embedded, trustworthy data-analysis copilot on the Vanna Web Component
version: 1.0
date_created: 2026-08-02
last_updated: 2026-08-02
owner: GengNSCUTer
status: In progress
tags: [architecture, vanna, data-analysis, backend, security]
---

# Introduction

![Status: In progress](https://img.shields.io/badge/status-In_progress-yellow)

This plan replaces the independent Next.js/TailAdmin direction. The product UI remains
the framework-agnostic `<vanna-chat>` component embedded in an existing page. Project
work concentrates on the Python backend, data contracts, secure SQL, rich evidence,
auditability, and evaluation.

## 1. Requirements & Constraints

- **REQ-001**: Preserve `frontends/webcomponent` as the Vanna baseline; integrate through element attributes, CSS custom properties, and browser events before editing upstream component code.
- **REQ-002**: Keep `examples/siliconflow_sqlite_web_demo.py` as the runnable smoke-test baseline until a production-oriented app replaces it.
- **REQ-003**: Deliver the embedded interaction as a floating, minimizable analysis assistant in a host page; do not create a separate React, Next.js, or TailAdmin application.
- **REQ-004**: Return status, tables, charts, business explanation, metric evidence, and SQL visibility according to the resolved user role.
- **REQ-005**: Use Olist as the primary showcase only after its license, source version, and data quality are recorded in `data/manifest/datasets.yaml`.
- **SEC-001**: Never commit `.env`, raw data, generated SQLite files, query CSV output, browser artifacts, or credentials.
- **SEC-002**: All model-generated SQL must pass a project-owned AST policy before a PostgreSQL read-only role executes it.
- **SEC-003**: Tool access groups are supplementary controls; SQL policy and database permissions remain mandatory independent defenses.
- **CON-001**: Development uses SiliconFlow `deepseek-ai/DeepSeek-V4-Flash`; local OpenAI-compatible vLLM/Ollama endpoints remain configuration-only alternatives.
- **CON-002**: Redis, queues, multi-agent orchestration, write operations, and a standalone frontend are out of scope until a concrete requirement is measured.
- **GUD-001**: Record each completed phase in `PROJECT.md`, `docs/DEVELOPMENT_PLAN.md`, the Feishu project document, Git, and `origin/main`.
- **PAT-001**: Add project-specific behavior in a new package under `src/data_analysis_agent/`; avoid modifying `src/vanna/` except for upstream merges or isolated bug fixes.

## 2. Implementation Steps

### Implementation Phase 1: Complete the embedded baseline audit

- GOAL-001: Convert framework promises into repeatable component and API verification.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Add `tests/e2e/test_native_component.py` using Playwright. Verify initial host-page render, minimize, restore, maximize, SSE table result, mobile layout, and absence of application console errors. The prototype uses a fixed demo identity and does not claim real login. | ✅ | 2026-08-02 |
| TASK-002 | Add `docs/VANNA_CAPABILITY_AUDIT.md` and keep each row categorized as verified, source-confirmed, or not-enabled. | ✅ | 2026-08-02 |
| TASK-003 | Add a host-page example at `examples/embedded_analyst_host.html` that loads `<vanna-chat>`, starts minimized, provides Chinese title/subtitle/placeholder, and records `window-state-changed` events without a framework dependency. | ✅ | 2026-08-02 |
| TASK-004 | Configure result-file output outside the repository by injecting a project-owned `FileSystem` into `RunSqlTool`; add a regression test proving `query_results_*.csv` never appears at repository root. Define retention and cleanup when chart/file workflows are introduced. | ✅ | 2026-08-02 |

### Implementation Phase 2: Freeze the data contract and reproducible analytical store

- GOAL-002: Replace the synthetic SQLite-only fixture with a reproducible Olist analytical model.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-005 | Review and record the current Olist source license, download date, file checksums, and source columns in `data/manifest/datasets.yaml`; do not commit raw CSV files. |  |  |
| TASK-006 | Create `data/ddl/analytics.sql` and `data/transforms/` loaders for the tables in `docs/architecture/data-model.md`; preserve source-to-analysis column mappings. |  |  |
| TASK-007 | Create `data/fixtures/olist_minimal/` with synthetic relational rows and `tests/data/test_metrics.py` for GMV, order count, delivery, and review golden values. |  |  |
| TASK-008 | Update `docs/data-dictionary.md` and `docs/metric-catalog.md` from draft to v1 only after TASK-005 through TASK-007 have passed. |  |  |

### Implementation Phase 3: Build the project-owned trusted query boundary

- GOAL-003: Make Vanna tools use a safe, role-aware PostgreSQL execution path.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-009 | Create `src/data_analysis_agent/config.py` and `.env.example` for provider, database, policy, output directory, and model configuration. |  |  |
| TASK-010 | Create `src/data_analysis_agent/sql_policy.py` using `sqlglot`; enforce one statement, SELECT/CTE-SELECT only, allowed relations/columns, mandatory LIMIT, and query timeout metadata. |  |  |
| TASK-011 | Create `src/data_analysis_agent/sql_runner.py` with a PostgreSQL read-only connection and a `SqlRunner` implementation that invokes TASK-010 before executing SQL. |  |  |
| TASK-012 | Create `src/data_analysis_agent/users.py` to resolve local demo identities from a signed session or explicit development header into `analyst` and `admin` groups. |  |  |
| TASK-013 | Create `src/data_analysis_agent/agent_factory.py` to register the safe SQL tool, metric/schema context tools, and `VisualizeDataTool`; never register Vanna's unrestricted runner in the production app. |  |  |
| TASK-014 | Add `tests/security/test_sql_policy.py` covering DDL, DML, multiple statements, comments, unauthorized tables, no LIMIT, and valid aggregations. |  |  |

### Implementation Phase 4: Add evidence, charts, and embedded host behavior

- GOAL-004: Make each accepted answer usable in a business page and defensible in an interview.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-015 | Create `src/data_analysis_agent/evidence.py` to emit metric ID/version, source tables, normalized SQL, row count, policy decision, and result summary as Vanna rich components. |  |  |
| TASK-016 | Register `VisualizeDataTool` with the controlled output filesystem and add prompt/tool policy requiring a chart for time-series and ranked comparison questions. |  |  |
| TASK-017 | Add a host-layer stylesheet in `examples/embedded_analyst_host.html` that renders the normal component as a right-side panel and retains Vanna minimize/maximize behavior. |  |  |
| TASK-018 | Add role-aware SQL visibility: analyst receives evidence summary, admin can receive normalized SQL and policy details. |  |  |
| TASK-019 | Add Playwright tests for chart rendering, panel state transitions, analyst/admin SQL visibility, and mobile viewport layout. |  |  |

### Implementation Phase 5: Persist audit records and prove quality

- GOAL-005: Turn a working demo into an auditable and evaluable portfolio project.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-020 | Create PostgreSQL migrations for query audit records, metric versions, dataset versions, policy decisions, and conversation metadata. |  |  |
| TASK-021 | Implement `src/data_analysis_agent/audit.py` as a Vanna `AuditLogger`; persist request ID, user, model, prompt version, candidate/final SQL, result metadata, and failure reason. |  |  |
| TASK-022 | Convert `evals/cases/draft.yaml` into runnable cases with expected SQL characteristics and golden result checks; add an evaluation runner under `evals/runner.py`. |  |  |
| TASK-023 | Publish a versioned report with execution success rate, manually checked metric correctness, security interception rate, and latency percentiles; use only measured values in resume text. |  |  |

## 3. Alternatives

- **ALT-001**: Build a standalone Next.js/TailAdmin dashboard. Rejected because Vanna's Web Component already solves the interactive analysis surface and a second frontend would add a separate build, auth, deployment, and maintenance boundary without strengthening the core data-analysis system.
- **ALT-002**: Modify `frontends/webcomponent/src/components/vanna-chat.ts` directly for every product change. Rejected because it increases upstream merge cost; host-page composition and backend rich components provide the required first-stage customization.
- **ALT-003**: Use Vanna's stock `SqliteRunner` or `PostgresRunner` in the portfolio app. Rejected because their generic execution behavior does not provide the project-owned AST allowlist, query budget, or application audit contract required by SEC-002.

## 4. Dependencies

- **DEP-001**: Vanna 2.0.2 source merged at repository commit `365d0617c1a4567ffee1b19b40c27feb4206bfcf` and tracked through the `upstream` remote.
- **DEP-002**: Conda environment `/disk2/gengnan/conda_envs/data-analysis-agent` with Python 3.12, Vanna editable install, FastAPI, OpenAI integration, python-dotenv, and Playwright.
- **DEP-003**: SiliconFlow credentials in ignored `.env` for development; a reachable local PostgreSQL instance is required only from TASK-011 onward.
- **DEP-004**: Olist raw source access and a documented license review are required before TASK-005 is complete.

## 5. Files

- **FILE-001**: `examples/siliconflow_sqlite_web_demo.py` remains the Phase 1 smoke-test launcher.
- **FILE-002**: `examples/embedded_analyst_host.html` becomes the framework-free embedded integration reference.
- **FILE-003**: `src/data_analysis_agent/` contains all project-owned backend behavior.
- **FILE-004**: `data/manifest/datasets.yaml`, `docs/data-dictionary.md`, `docs/metric-catalog.md`, and `docs/architecture/data-model.md` define the data contract.
- **FILE-005**: `tests/security/`, `tests/data/`, and `tests/e2e/` contain project verification separate from upstream tests.
- **FILE-006**: `docs/VANNA_CAPABILITY_AUDIT.md` and `docs/PORTFOLIO_POSITIONING.md` record the capability baseline and truthful resume scope.

## 6. Testing

- **TEST-001**: Run `python -m py_compile examples/siliconflow_sqlite_web_demo.py` and build the FastAPI app without exposing secrets.
- **TEST-002**: Run curl SSE integration against the synthetic fixture and assert the table result plus Chinese synthetic-data disclaimer.
- **TEST-003**: Run the Playwright native-component flow from TASK-001 in desktop and mobile viewports.
- **TEST-004**: Run fixture and metric golden tests from TASK-007 before and after database transformations.
- **TEST-005**: Run SQL policy security tests from TASK-014 for every policy change.
- **TEST-006**: Run the evaluation runner from TASK-022 and publish only reproducible results.

## 7. Risks & Assumptions

- **RISK-001**: The Vanna README describes framework capabilities that may require explicit tool registration or backend configuration. Mitigation: maintain the status distinctions in `docs/VANNA_CAPABILITY_AUDIT.md` and verify each claimed feature end to end.
- **RISK-002**: Direct Web Component changes may make upstream updates costly. Mitigation: follow REQ-001 and prefer host-level adaptation.
- **RISK-003**: The configured model can generate unsafe SQL. Mitigation: implement SEC-002 before production-like PostgreSQL access.
- **RISK-004**: Olist source terms or data quality could invalidate the showcase path. Mitigation: complete TASK-005 before raw-data use and retain Chinook/synthetic fixtures for regression.
- **ASSUMPTION-001**: The final portfolio demo may host the component in a small static business page; a full operational dashboard is not required to demonstrate embedded integration.
- **ASSUMPTION-002**: The user accepts PostgreSQL as the future analytical store and SiliconFlow as the normal development provider.

## 8. Related Specifications / Further Reading

[Vanna README](../README.md)
[Vanna capability audit](../docs/VANNA_CAPABILITY_AUDIT.md)
[Portfolio positioning](../docs/PORTFOLIO_POSITIONING.md)
[Data contract plan](../docs/DEVELOPMENT_PLAN.md)
[Project facts](../PROJECT.md)
