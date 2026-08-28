#!/usr/bin/env python3
"""Compatibility entry point for the canonical Spider smoke subset builder."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._post_training_compat import export_implementation


implementation = export_implementation(
    globals(), "scripts.post_training.data.build_spider_schema_stratified_smoke"
)
main = implementation.main


if __name__ == "__main__":
    raise SystemExit(main())
