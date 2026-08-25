"""Project-owned trusted data analysis extensions around Vanna.

The package also hosts offline post-training utilities with a deliberately
minimal dependency set. Keep production SQL modules lazy so a QLoRA worker can
import an isolated serialization or artifact guard without importing sqlglot,
SQLAlchemy, or the Vanna runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .sql_policy import PolicyDecision, PolicyViolation, SqlPolicy
    from .workspace import WorkspaceProfile


__all__ = ["PolicyDecision", "PolicyViolation", "SqlPolicy", "WorkspaceProfile"]


def __getattr__(name: str) -> Any:
    if name in {"PolicyDecision", "PolicyViolation", "SqlPolicy"}:
        from .sql_policy import PolicyDecision, PolicyViolation, SqlPolicy

        return {
            "PolicyDecision": PolicyDecision,
            "PolicyViolation": PolicyViolation,
            "SqlPolicy": SqlPolicy,
        }[name]
    if name == "WorkspaceProfile":
        from .workspace import WorkspaceProfile

        return WorkspaceProfile
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
