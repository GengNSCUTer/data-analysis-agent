"""Project-owned SQL tool wrapper with a bounded repair lifecycle.

Vanna's ``RunSqlTool`` remains responsible for rendering a successful
DataFrame.  This wrapper only coordinates a single, policy-first repair after
the injected runner reports a sanitized execution failure.
"""

from __future__ import annotations

import re
from hashlib import sha256
from typing import Protocol

from vanna.capabilities.sql_runner import RunSqlToolArgs
from vanna.core.llm import LlmMessage, LlmRequest, LlmService
from vanna.core.tool import ToolContext, ToolResult
from vanna.tools import RunSqlTool
from vanna.components import DataFrameComponent, SimpleTextComponent, UiComponent

from .budget import CURRENT_BUDGET
from .sql_policy import SqlPolicy
from .sql_repair import (
    OneShotSqlRepair,
    SanitizedSqlError,
    SqlRepairOutcome,
)
from .result_presentation import display_records
from .result_artifact_store import ResultArtifactStore


class RepairCandidateProvider(Protocol):
    async def __call__(self, prompt: str, context: ToolContext) -> str | None:
        """Return one untrusted SQL candidate, or ``None``."""


def extract_sql_candidate(content: str | None, *, max_chars: int = 8_000) -> str | None:
    """Extract only a bounded SQL-shaped response from a repair model."""

    if not isinstance(content, str) or not content.strip():
        return None
    text = content.strip()
    fenced = re.search(r"```(?:sql|postgres|postgresql)?\s*(.*?)```", text, re.I | re.S)
    if fenced:
        text = fenced.group(1).strip()
    text = re.sub(r"^sql\s*:\s*", "", text, flags=re.I).strip()
    match = re.search(r"\b(?:select|with)\b", text, re.I)
    if match:
        text = text[match.start() :]
    if not re.match(r"^(?:select|with)\b", text, re.I):
        return None
    return text[:max_chars].strip() or None


class LlmRepairCandidateProvider:
    """Adapt Vanna's LlmService to the bounded repair callback."""

    def __init__(self, llm_service: LlmService, *, max_tokens: int = 320):
        self.llm_service = llm_service
        self.max_tokens = max_tokens

    async def __call__(self, prompt: str, context: ToolContext) -> str | None:
        request = LlmRequest(
            messages=[LlmMessage(role="user", content=prompt)],
            user=context.user,
            stream=False,
            temperature=0.0,
            max_tokens=self.max_tokens,
            metadata={
                "purpose": "sql_repair",
                "conversation_id": context.conversation_id,
                "request_id": context.request_id,
            },
        )
        response = await self.llm_service.send_request(request)
        usage = CURRENT_BUDGET.get()
        if usage is not None:
            usage.record_usage(response.usage)
        return extract_sql_candidate(response.content)


class TrustedRunSqlTool(RunSqlTool):
    """Run Vanna SQL rendering with one request-scoped repair attempt."""

    def __init__(
        self,
        sql_runner,
        *,
        repair_provider: RepairCandidateProvider | None = None,
        repair_policy: SqlPolicy | None = None,
        file_system=None,
        custom_tool_name: str | None = None,
        custom_tool_description: str | None = None,
        result_artifact_store: ResultArtifactStore | None = None,
    ):
        super().__init__(
            sql_runner=sql_runner,
            file_system=file_system,
            custom_tool_name=custom_tool_name,
            custom_tool_description=custom_tool_description,
        )
        self.repair_provider = repair_provider
        self.repair_policy = repair_policy or getattr(sql_runner, "policy", None)
        self.result_artifact_store = result_artifact_store

    async def execute(self, context: ToolContext, args: RunSqlToolArgs) -> ToolResult:
        result = await super().execute(context, args)
        if result.success:
            self._replace_with_server_presentation(context, result)
            self._remember_result_artifact(context, result)
            return result

        safe_error = context.metadata.get("safe_sql_error")
        if not isinstance(safe_error, SanitizedSqlError):
            # Result validation failures are terminal for this tool call.  A
            # second SQL candidate cannot prove a missing semantic column.
            validation = context.metadata.get("result_validation")
            if validation and validation.get("state") != "valid":
                return self._terminal_result(
                    result,
                    context,
                    evidence={
                        "repair_attempted": False,
                        "repair_execution_status": "not_attempted",
                        "result_validation": validation,
                        "terminal_reason": "result_validation_failed",
                    },
                    detail="查询已执行，但结果没有通过指标结果合同，系统未输出未经验证的数字。",
                )
            return result
        if self.repair_provider is None or self.repair_policy is None:
            return self._terminal_result(
                result,
                context,
                evidence={
                    "repair_attempted": False,
                    "repair_error_category": safe_error.category,
                    "repair_execution_status": "not_attempted",
                    "terminal_reason": "repair_provider_unavailable",
                },
                detail=safe_error.public_reason,
            )
        if context.metadata.get("sql_repair_attempted"):
            return self._terminal_result(
                result,
                context,
                evidence={
                    "repair_attempted": False,
                    "repair_error_category": safe_error.category,
                    "repair_execution_status": "not_attempted",
                    "terminal_reason": "repair_budget_exhausted",
                },
                detail="SQL 修复次数已达到上限，系统未输出未经验证的数字。",
            )

        context.metadata["sql_repair_attempted"] = True
        role = "admin" if "admin" in context.user.group_memberships else "analyst"
        coordinator = OneShotSqlRepair(self.repair_policy, role=role)
        catalog_context = str(context.metadata.get("catalog_context", ""))
        required_columns = context.metadata.get("required_result_columns", ())
        if isinstance(required_columns, (list, tuple)) and required_columns:
            catalog_context = (
                f"{catalog_context}\n"
                "<required_result_columns>\n"
                + ", ".join(str(column) for column in required_columns[:32])
                + "\n</required_result_columns>"
            )
        outcome = await coordinator.repair_async(
            args.sql,
            safe_error,
            lambda prompt: self.repair_provider(prompt, context),
            catalog_context=catalog_context,
        )
        evidence = self._evidence(outcome)
        context.metadata["repair_evidence"] = evidence
        usage = CURRENT_BUDGET.get()
        if usage is not None:
            usage.record_repair(evidence)

        if not outcome.accepted or not outcome.repaired_sql:
            evidence["terminal_reason"] = outcome.reason
            self._audit_repair_rejection(context, args.sql, outcome, role)
            return self._terminal_result(
                result,
                context,
                evidence=evidence,
                detail="原始 SQL 执行失败，修复候选没有通过安全策略，系统未输出未经验证的数字。",
            )

        # The retry is a new database attempt, but remains one logical Vanna
        # tool call and inherits the same server-owned result contract.
        context.metadata.pop("safe_sql_error", None)
        context.metadata.pop("sql_error", None)
        retry_result = await super().execute(
            context, RunSqlToolArgs(sql=outcome.repaired_sql)
        )
        if retry_result.success:
            evidence["repair_execution_status"] = "succeeded"
            evidence["result_validation"] = context.metadata.get("result_validation")
            retry_result.metadata["sql_repair"] = evidence
            self._remember_result_artifact(context, retry_result)
            return retry_result

        evidence["repair_execution_status"] = "failed"
        evidence["result_validation"] = context.metadata.get("result_validation")
        evidence["terminal_reason"] = "repaired_sql_execution_failed"
        return self._terminal_result(
            retry_result,
            context,
            evidence=evidence,
            detail="修复后的 SQL 仍未通过可信执行链，系统未输出未经验证的数字。",
        )

    def _remember_result_artifact(self, context: ToolContext, result: ToolResult) -> None:
        """Bind a chart to the one successful result emitted in this request."""

        output_file = result.metadata.get("output_file")
        if isinstance(output_file, str) and output_file:
            context.metadata["current_result_filename"] = output_file
        if self.result_artifact_store is None:
            return
        validation = context.metadata.get("result_validation")
        # A successful UI tool result alone is not evidence that the result
        # satisfies the server-owned ResultContract.  Artifact persistence is
        # deliberately fail-closed so unvalidated rows can never be replayed.
        if not isinstance(validation, dict) or validation.get("state") != "valid":
            return
        columns = result.metadata.get("columns")
        rows = result.metadata.get("results")
        if not isinstance(columns, list) or not isinstance(rows, list):
            return
        workspace_id = str(context.metadata.get("workspace_id") or "unknown")
        artifact_id = "rta_" + sha256(context.request_id.encode("utf-8")).hexdigest()[:20]
        contract = context.metadata.get("result_contract")
        contract = contract if isinstance(contract, dict) else {}
        manifest = self.result_artifact_store.save_result_csv(
            artifact_id=artifact_id,
            user_id=context.user.id,
            workspace_id=workspace_id,
            columns=columns,
            rows=rows,
            row_count=int(result.metadata.get("row_count") or len(rows)),
            dataset_version=str(contract.get("dataset_version") or "unknown"),
            metric_version=str(contract.get("metric_version") or "unknown"),
        )
        context.metadata["result_artifact"] = manifest
        usage = CURRENT_BUDGET.get()
        if usage is not None:
            usage.record_catalog({"result_artifact": _safe_artifact_manifest(manifest)})

    @staticmethod
    def _evidence(outcome: SqlRepairOutcome) -> dict[str, object]:
        data = outcome.as_dict()
        return {
            "repair_attempted": bool(data["attempted"]),
            "original_sql": str(data["original_sql"])[:8_000],
            "repair_error_category": (data["error"] or {}).get("category")
            if isinstance(data.get("error"), dict)
            else None,
            "repair_policy_status": data.get("policy_status"),
            "repaired_sql": (
                str(data["repaired_sql"])[:8_000]
                if data.get("repaired_sql") is not None
                else None
            ),
            "repair_reason": data.get("reason"),
            "repair_execution_status": "not_attempted",
            "result_validation": None,
            "terminal_reason": None,
        }

    def _audit_repair_rejection(
        self,
        context: ToolContext,
        original_sql: str,
        outcome: SqlRepairOutcome,
        role: str,
    ) -> None:
        audit = getattr(self.sql_runner, "audit", None)
        if audit is None or outcome.repaired_sql is None:
            return
        try:
            audit.record(
                context,
                role,
                original_sql,
                status="rejected",
                final_sql=outcome.repaired_sql,
                reason=f"repair:{outcome.reason}",
                error_message="修复候选未通过 SQL Policy",
                repair_evidence=context.metadata.get("repair_evidence"),
            )
        except Exception:
            # An audit transport outage must not turn a safe refusal into an
            # application error or expose driver details to the model.
            return

    @staticmethod
    def _terminal_result(
        result: ToolResult,
        context: ToolContext,
        *,
        evidence: dict[str, object],
        detail: str,
    ) -> ToolResult:
        context.metadata["repair_evidence"] = evidence
        usage = CURRENT_BUDGET.get()
        if usage is not None:
            usage.record_repair(evidence)
            error_type = (
                "result_validation_failed"
                if evidence.get("terminal_reason") == "result_validation_failed"
                else "sql_repair_failed"
            )
            usage.terminate("execution_error", error_type)
        result.success = False
        result.result_for_llm = detail
        result.error = detail
        result.metadata = {
            **result.metadata,
            "error_type": "sql_error",
            "sql_repair": evidence,
            "terminal_reason": evidence.get("terminal_reason"),
        }
        return result

    @staticmethod
    def _replace_with_server_presentation(context: ToolContext, result: ToolResult) -> None:
        """Format only the UI table after validation, never the result artifact."""
        records = result.metadata.get("display_results")
        columns = result.metadata.get("columns")
        if not isinstance(records, list) or not isinstance(columns, list):
            return
        labels = context.metadata.get("result_column_labels")
        if not isinstance(labels, dict):
            labels = {}
        display_rows, _ = display_records(records, columns=columns, labels=labels)
        row_count = int(result.metadata.get("row_count") or len(display_rows))
        display_truncated = bool(result.metadata.get("display_truncated"))
        budget = int(result.metadata.get("display_row_budget") or len(display_rows))
        result.ui_component = UiComponent(
            rich_component=DataFrameComponent.from_records(
                records=display_rows,
                title="查询结果",
                max_rows_displayed=budget,
                description=(
                    f"已通过结果合同：共 {row_count} 行、{len(columns)} 列"
                    + (f"；界面仅预览前 {budget} 行" if display_truncated else "")
                ),
            ),
            # Keep the model-facing summary and result filename unchanged. It
            # remains a tool artifact, not a user-facing formatting source.
            simple_component=SimpleTextComponent(text=result.result_for_llm),
        )


def _safe_artifact_manifest(manifest: dict[str, object]) -> dict[str, object]:
    """Keep only bounded artifact identity in request audit evidence."""
    allowed = {
        "version", "artifact_id", "workspace_id", "dataset_version",
        "metric_version", "row_count", "columns", "csv_sha256", "csv_bytes",
        "csv_ref", "plotly_sha256", "plotly_bytes", "plotly_ref",
    }
    return {key: manifest[key] for key in allowed if key in manifest}
