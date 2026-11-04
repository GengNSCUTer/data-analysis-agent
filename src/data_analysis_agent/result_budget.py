"""Server-owned execution envelopes, separate from SQL semantic inputs.

SQL execution returns complete aggregates. Pages and CSV downloads read an
existing validated artifact and cannot fall through to ordinary run_sql.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


RESULT_BUDGET_CONTRACT_ID = "result-budget-v2"
QueryMode = Literal["aggregate", "detail_page", "export"]
DEFAULT_EXECUTION_MAX_ROWS = 5_000
MAX_PREVIEW_ROWS = 5_000


class ResultSettings(BaseModel):
    """User resource preferences, scoped by the authenticated workspace."""

    model_config = ConfigDict(extra="forbid", strict=True)
    execution_max_rows: int = Field(default=DEFAULT_EXECUTION_MAX_ROWS, gt=0)
    preview_rows: int = Field(default=200, gt=0, le=MAX_PREVIEW_ROWS)

    @model_validator(mode="after")
    def check_preview(self) -> "ResultSettings":
        if self.preview_rows > self.execution_max_rows:
            raise ValueError("preview_rows must fit execution_max_rows")
        return self

    def to_budget(self, ceiling: "ResultBudget") -> "ResultBudget":
        # An operator may lower the process ceiling after preferences were saved.
        rows = min(self.execution_max_rows, ceiling.execution_max_rows)
        return replace(ceiling, execution_max_rows=rows, aggregate_result_rows=rows,
                       preview_rows=min(self.preview_rows, rows))


class ResultBudgetError(ValueError):
    """A plan/contract execution envelope is inconsistent or unsupported."""


@dataclass(frozen=True)
class ResultBudget:
    """Immutable per-request budget; counts never describe fact scan rows.

    aggregate_result_rows is the plan's output cardinality bound, within the
    user's execution ceiling. detail_page/export are artifact delivery modes.
    """

    query_mode: QueryMode = "aggregate"
    contract_id: str = RESULT_BUDGET_CONTRACT_ID
    execution_max_rows: int = DEFAULT_EXECUTION_MAX_ROWS
    statement_timeout_ms: int = 5_000
    aggregate_result_rows: int | None = DEFAULT_EXECUTION_MAX_ROWS
    detail_page_rows: int | None = None
    export_max_bytes: int | None = None
    preview_rows: int = 200
    llm_context_rows: int = 8
    llm_context_chars: int = 1_200
    result_max_bytes: int = 32 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.contract_id != RESULT_BUDGET_CONTRACT_ID:
            raise ResultBudgetError("unsupported result budget contract")
        if self.query_mode not in ("aggregate", "detail_page", "export"):
            raise ResultBudgetError("unsupported query_mode")
        for name in (
            "execution_max_rows", "statement_timeout_ms", "preview_rows",
            "llm_context_rows", "llm_context_chars", "aggregate_result_rows",
            "detail_page_rows", "export_max_bytes",
            "result_max_bytes",
        ):
            value = getattr(self, name)
            if value is None and name in (
                "aggregate_result_rows", "detail_page_rows", "export_max_bytes"
            ):
                continue
            if type(value) is not int or value <= 0:
                raise ResultBudgetError(f"{name} must be a positive integer")
        if self.preview_rows > min(MAX_PREVIEW_ROWS, self.execution_max_rows):
            raise ResultBudgetError("preview_rows exceeds the display/execution budget")
        if self.llm_context_rows > 8 or self.llm_context_chars > 1_200:
            raise ResultBudgetError("LLM budget exceeds the historical offline sample budget")
        if self.query_mode == "aggregate":
            if self.aggregate_result_rows is None or self.aggregate_result_rows > self.execution_max_rows:
                raise ResultBudgetError("aggregate_result_rows must fit the execution budget")
            if self.detail_page_rows is not None or self.export_max_bytes is not None:
                raise ResultBudgetError("aggregate cannot carry page/export budgets")
        elif self.query_mode == "detail_page":
            if self.aggregate_result_rows is not None or self.export_max_bytes is not None:
                raise ResultBudgetError("detail_page cannot carry aggregate/export budgets")
            if self.detail_page_rows is None or self.detail_page_rows > min(500, self.execution_max_rows):
                raise ResultBudgetError("detail_page_rows must fit the page/execution budget")
        elif self.aggregate_result_rows is not None or self.detail_page_rows is not None:
            raise ResultBudgetError("export cannot carry aggregate/page budgets")

    @classmethod
    def aggregate(
        cls, *, execution_max_rows: int = DEFAULT_EXECUTION_MAX_ROWS, statement_timeout_ms: int = 5_000,
        preview_rows: int = 200,
        result_max_bytes: int = 32 * 1024 * 1024,
    ) -> "ResultBudget":
        return cls(
            execution_max_rows=execution_max_rows,
            aggregate_result_rows=execution_max_rows,
            statement_timeout_ms=statement_timeout_ms,
            preview_rows=min(preview_rows, execution_max_rows),
            result_max_bytes=result_max_bytes,
        )

    def for_result_rows(self, rows: int) -> "ResultBudget":
        """Derive output cardinality without changing the request resource ceiling."""
        return replace(self, aggregate_result_rows=max(1, rows))

    def as_metadata(self) -> dict[str, Any]:
        budget = asdict(self)
        mode = budget.pop("query_mode")
        return {"query_mode": mode, "result_budget": budget}

    def as_evidence(self) -> dict[str, Any]:
        """Name each limit by the layer it protects.

        ``as_metadata`` is the strict plan/contract payload and keeps its
        frozen field names. This view is only for logs and UI diagnostics, so
        readers do not mistake the request ceiling, aggregate cardinality,
        frontend preview and historical offline sample for one row limit.
        Online result evidence is separately bounded by RequestBudget tokens.
        """
        return {
            "contract_id": self.contract_id,
            "query_mode": self.query_mode,
            "request_execution_row_ceiling": self.execution_max_rows,
            "query_result_row_ceiling": self.aggregate_result_rows,
            "detail_page_row_limit": self.detail_page_rows,
            "frontend_preview_row_limit": self.preview_rows,
            "offline_sample_row_limit": self.llm_context_rows,
            "offline_sample_char_limit": self.llm_context_chars,
            "result_byte_ceiling": self.result_max_bytes,
            "statement_timeout_ms": self.statement_timeout_ms,
            "export_byte_limit": self.export_max_bytes,
        }

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any]) -> "ResultBudget":
        budget = metadata.get("result_budget")
        if not isinstance(budget, Mapping):
            raise ResultBudgetError("result_budget must be an object")
        expected_keys = set(cls().as_metadata()["result_budget"])
        if set(budget) != expected_keys:
            raise ResultBudgetError("result_budget fields are missing or unknown")
        return cls(query_mode=metadata.get("query_mode"), **dict(budget))


def validate_matching_result_budgets(
    plan_budget: ResultBudget | None, contract_budget: ResultBudget | None
) -> None:
    """Legacy pairs are allowed; a new envelope must be present on both sides."""
    if plan_budget != contract_budget:
        raise ResultBudgetError("QueryPlan/ResultContract result budget mismatch")
    if plan_budget is not None and plan_budget.query_mode != "aggregate":
        raise ResultBudgetError(f"query_mode {plan_budget.query_mode} is not enabled")


def validate_runtime_result_budget(
    metadata: Mapping[str, Any], *, expected: ResultBudget
) -> ResultBudget | None:
    """Check server plan and tool contract before Policy or a reader connection.

    Old offline callers omit both envelopes. A half-present or mutated envelope
    must fail rather than silently falling back to the old execution contract.
    Per-request budgets may be smaller than the process resource ceilings.
    """
    plan = metadata.get("query_plan")
    plan = plan if isinstance(plan, Mapping) else {}
    keys = ("query_mode", "result_budget")
    plan_has_budget = any(key in plan for key in keys)
    contract_has_budget = any(key in metadata for key in keys)
    if not plan_has_budget and not contract_has_budget:
        return None
    if not plan_has_budget or not contract_has_budget:
        raise ResultBudgetError("QueryPlan/ResultContract result budget missing pair")
    plan_budget = ResultBudget.from_metadata(plan)
    contract_budget = ResultBudget.from_metadata(metadata)
    validate_matching_result_budgets(plan_budget, contract_budget)
    if (
        contract_budget.execution_max_rows > expected.execution_max_rows
        or contract_budget.statement_timeout_ms > expected.statement_timeout_ms
        or contract_budget.result_max_bytes > expected.result_max_bytes
    ):
        raise ResultBudgetError("result budget differs from server execution settings")
    return contract_budget
