"""Bounded, policy-first SQL repair contracts.

The module does not call an LLM itself.  A caller supplies one repair
candidate, then this module treats it as untrusted and runs it through the
same AST policy before it can be executed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Literal
import re

from .sql_policy import PolicyDecision, PolicyViolation, SqlPolicy


SqlErrorCategory = Literal[
    "syntax",
    "unknown_column",
    "unknown_table",
    "ambiguous_reference",
    "timeout",
    "permission",
    "connection",
    "unknown",
]


@dataclass(frozen=True)
class SanitizedSqlError:
    category: SqlErrorCategory
    public_reason: str
    retryable: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "public_reason": self.public_reason,
            "retryable": self.retryable,
        }


class SafeSqlExecutionError(RuntimeError):
    """Database failures exposed to the Agent without driver/credential text."""

    def __init__(self, error: SanitizedSqlError):
        self.error = error
        super().__init__(error.public_reason)


@dataclass(frozen=True)
class SqlRepairOutcome:
    attempted: bool
    accepted: bool
    original_sql: str
    repaired_sql: str | None
    reason: str
    error: SanitizedSqlError | None = None
    policy_decision: PolicyDecision | None = None

    def as_dict(self) -> dict[str, object]:
        """Return auditable metadata without raw database errors or rows."""
        return {
            "attempted": self.attempted,
            "accepted": self.accepted,
            "original_sql": self.original_sql,
            "repaired_sql": self.repaired_sql,
            "reason": self.reason,
            "error": self.error.as_dict() if self.error else None,
            "policy_status": self.policy_decision.status
            if self.policy_decision
            else None,
            "policy_reason": self.policy_decision.reason
            if self.policy_decision
            else None,
        }


_CLASSIFIERS: tuple[tuple[SqlErrorCategory, tuple[str, ...], str, bool], ...] = (
    (
        "timeout",
        ("statement timeout", "canceling statement due to user request", "timeout"),
        "查询超过执行时限，请缩小时间范围或问题粒度。",
        False,
    ),
    (
        "permission",
        ("permission denied", "not authorized", "insufficient privilege"),
        "当前角色没有访问该数据对象的权限。",
        False,
    ),
    (
        "connection",
        ("could not connect", "connection refused", "server closed the connection"),
        "分析数据库当前不可用，请稍后重试。",
        False,
    ),
    (
        "unknown_column",
        ("column", "unknown column", "missing from-clause entry", "undefined column"),
        "生成的查询引用了不存在或不可用的列。",
        True,
    ),
    (
        "unknown_table",
        ("relation", "table"),
        "生成的查询引用了不存在或不可用的表。",
        True,
    ),
    (
        "ambiguous_reference",
        ("ambiguous", "could refer to"),
        "生成的查询存在未限定的字段引用。",
        True,
    ),
    (
        "syntax",
        ("syntax error", "parse failed", "unterminated", "grouping error"),
        "生成的查询语法或聚合结构无效。",
        True,
    ),
)


def sanitize_sql_error(
    error: BaseException | SanitizedSqlError | str,
) -> SanitizedSqlError:
    """Map driver/parser text to a small stable category; never expose it."""
    if isinstance(error, SanitizedSqlError):
        return error
    text = str(error).casefold()
    for category, needles, reason, retryable in _CLASSIFIERS:
        if any(needle in text for needle in needles):
            # The generic ``table``/``column`` markers only classify when the
            # driver also says the object does not exist; avoid mislabeling
            # arbitrary permission or application messages.
            if (
                category in {"unknown_column", "unknown_table"}
                and "does not exist" not in text
                and "missing from-clause" not in text
                and "undefined column" not in text
            ):
                continue
            return SanitizedSqlError(category, reason, retryable)
    return SanitizedSqlError(
        "unknown", "查询执行失败，系统未输出未经验证的数字结论。", False
    )


def build_repair_prompt(
    original_sql: str,
    error: SanitizedSqlError,
    catalog_context: str = "",
    *,
    max_chars: int = 8_000,
) -> str:
    """Build a bounded repair request with only a sanitized error category."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    sql = original_sql.strip()[: max_chars // 2]
    context = catalog_context.strip()[: max_chars // 2]
    prompt = (
        "你正在修复一条只读 PostgreSQL 查询。数据库错误已被归类，不能猜测或扩大权限。\n"
        f"错误类别：{error.category}\n"
        f"安全提示：{error.public_reason}\n"
        "请只返回一条修复后的 SQL，不要解释、不要写库、不要访问 Catalog 未列出的对象。\n"
        f"<original_sql>\n{sql}\n</original_sql>\n"
    )
    if context:
        prompt += f"<allowed_catalog>\n{context}\n</allowed_catalog>\n"
    return prompt[:max_chars]


class OneShotSqlRepair:
    """Coordinate at most one repair candidate for one request."""

    def __init__(self, policy: SqlPolicy, *, role: str = "analyst"):
        self.policy = policy
        self.role = role
        self.attempted = False

    def repair(
        self,
        original_sql: str,
        error: BaseException | SanitizedSqlError | str,
        candidate_factory: Callable[[str], str | None],
        catalog_context: str = "",
    ) -> SqlRepairOutcome:
        sanitized = sanitize_sql_error(error)
        if self.attempted:
            return self._budget_exhausted(original_sql, sanitized)
        self.attempted = True
        if not sanitized.retryable:
            return self._not_repairable(original_sql, sanitized)
        prompt = build_repair_prompt(original_sql, sanitized, catalog_context)
        repaired_sql = candidate_factory(prompt)
        return self._accept_candidate(original_sql, sanitized, repaired_sql)

    async def repair_async(
        self,
        original_sql: str,
        error: BaseException | SanitizedSqlError | str,
        candidate_factory: Callable[[str], Awaitable[str | None]],
        catalog_context: str = "",
    ) -> SqlRepairOutcome:
        """Run the same bounded contract with an asynchronous LLM provider."""

        sanitized = sanitize_sql_error(error)
        if self.attempted:
            return self._budget_exhausted(original_sql, sanitized)
        self.attempted = True
        if not sanitized.retryable:
            return self._not_repairable(original_sql, sanitized)
        prompt = build_repair_prompt(original_sql, sanitized, catalog_context)
        try:
            repaired_sql = await candidate_factory(prompt)
        except Exception:
            # The repair path must fail closed if the secondary model is
            # unavailable or returns an unexpected provider error.
            repaired_sql = None
        return self._accept_candidate(original_sql, sanitized, repaired_sql)

    def _accept_candidate(
        self,
        original_sql: str,
        sanitized: SanitizedSqlError,
        repaired_sql: str | None,
    ) -> SqlRepairOutcome:
        if not isinstance(repaired_sql, str) or not repaired_sql.strip():
            return SqlRepairOutcome(
                attempted=True,
                accepted=False,
                original_sql=original_sql,
                repaired_sql=None,
                reason="repair_candidate_missing",
                error=sanitized,
            )
        try:
            decision = self.policy.evaluate(repaired_sql, role=self.role)
        except PolicyViolation:
            return SqlRepairOutcome(
                attempted=True,
                accepted=False,
                original_sql=original_sql,
                repaired_sql=repaired_sql,
                reason="repaired_sql_rejected_by_policy",
                error=sanitized,
            )
        return SqlRepairOutcome(
            attempted=True,
            accepted=True,
            original_sql=original_sql,
            repaired_sql=repaired_sql,
            reason="repaired_sql_passed_policy",
            error=sanitized,
            policy_decision=decision,
        )

    @staticmethod
    def _budget_exhausted(
        original_sql: str, error: SanitizedSqlError
    ) -> SqlRepairOutcome:
        return SqlRepairOutcome(
            attempted=False,
            accepted=False,
            original_sql=original_sql,
            repaired_sql=None,
            reason="repair_budget_exhausted",
            error=error,
        )

    @staticmethod
    def _not_repairable(
        original_sql: str, error: SanitizedSqlError
    ) -> SqlRepairOutcome:
        return SqlRepairOutcome(
            attempted=False,
            accepted=False,
            original_sql=original_sql,
            repaired_sql=None,
            reason="error_not_repairable",
            error=error,
        )
