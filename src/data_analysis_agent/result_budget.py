"""Server-owned execution envelopes, separate from SQL semantic inputs.

Only aggregate execution is enabled in this migration step. The other modes
can be represented and audited, but cannot fall through to ordinary run_sql.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping


RESULT_BUDGET_CONTRACT_ID = "result-budget-v2"
QueryMode = Literal["aggregate", "detail_page", "export"]


class ResultBudgetError(ValueError):
    """A plan/contract execution envelope is inconsistent or unsupported."""


@dataclass(frozen=True)
class ResultBudget:
    """Immutable per-request budget; counts never describe fact scan rows.

    aggregate_result_rows is still the interim execution cap in this step.
    It is not a measured production cardinality threshold. detail_page/export
    require their own execution paths before they can be enabled.
    """

    query_mode: QueryMode = "aggregate"
    contract_id: str = RESULT_BUDGET_CONTRACT_ID
    execution_max_rows: int = 1_000
    statement_timeout_ms: int = 5_000
    aggregate_result_rows: int | None = 1_000
    detail_page_rows: int | None = None
    export_max_bytes: int | None = None
    preview_rows: int = 200
    llm_context_rows: int = 8
    llm_context_chars: int = 1_200

    def __post_init__(self) -> None:
        if self.contract_id != RESULT_BUDGET_CONTRACT_ID:
            raise ResultBudgetError("unsupported result budget contract")
        if self.query_mode not in ("aggregate", "detail_page", "export"):
            raise ResultBudgetError("unsupported query_mode")
        for name in (
            "execution_max_rows", "statement_timeout_ms", "preview_rows",
            "llm_context_rows", "llm_context_chars", "aggregate_result_rows",
            "detail_page_rows", "export_max_bytes",
        ):
            value = getattr(self, name)
            if value is None and name in (
                "aggregate_result_rows", "detail_page_rows", "export_max_bytes"
            ):
                continue
            if type(value) is not int or value <= 0:
                raise ResultBudgetError(f"{name} must be a positive integer")
        if self.preview_rows > 200 or self.llm_context_rows > 8 or self.llm_context_chars > 1_200:
            raise ResultBudgetError("preview/LLM budget exceeds the frozen output budget")
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
        cls, *, execution_max_rows: int = 1_000, statement_timeout_ms: int = 5_000
    ) -> "ResultBudget":
        return cls(
            execution_max_rows=execution_max_rows,
            aggregate_result_rows=execution_max_rows,
            statement_timeout_ms=statement_timeout_ms,
            preview_rows=min(200, execution_max_rows),
        )

    def as_metadata(self) -> dict[str, Any]:
        budget = asdict(self)
        mode = budget.pop("query_mode")
        return {"query_mode": mode, "result_budget": budget}

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
    Budget identity is exact during this metadata-only migration: advertise no
    lower/higher limit that the current execution path does not actually use.
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
    if contract_budget != expected:
        raise ResultBudgetError("result budget differs from server execution settings")
    return contract_budget
