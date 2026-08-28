"""Keep legacy script paths working while implementations live by capability."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
import sys
from types import ModuleType
from typing import Any


def export_implementation(target: dict[str, Any], module_name: str) -> ModuleType:
    """Expose every public implementation symbol through a legacy module."""

    repository_root = Path(__file__).resolve().parents[1]
    source_root = repository_root / "src"
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    implementation = import_module(module_name)
    public_names = [name for name in dir(implementation) if not name.startswith("_")]
    target.update({name: getattr(implementation, name) for name in public_names})
    target["__all__"] = public_names
    return implementation
