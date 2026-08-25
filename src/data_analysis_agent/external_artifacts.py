"""Minimal external-artifact path guards shared by offline research scripts.

This module intentionally has no production SQL, web, database or model
dependencies. QLoRA environments can import it without pulling in the Vanna
runtime dependency graph.
"""

from __future__ import annotations

from pathlib import Path


class ExternalArtifactPathError(ValueError):
    """An experiment attempted to place raw output inside the Git worktree."""


def ensure_path_outside_repository(path: Path, repository_root: Path) -> Path:
    """Resolve ``path`` and reject every location nested under ``repository_root``."""

    resolved_path = path.resolve()
    resolved_repository = repository_root.resolve()
    if resolved_path.is_relative_to(resolved_repository):
        raise ExternalArtifactPathError("artifact output must stay outside the repository")
    return resolved_path
