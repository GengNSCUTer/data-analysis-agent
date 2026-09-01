#!/usr/bin/env python3
"""Compatibility entry point for Olist candidate prompt-language comparisons."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._post_training_compat import export_implementation


implementation = export_implementation(
    globals(), "scripts.post_training.evaluation.analyze_olist_candidate_sql_locale_comparison"
)
main = implementation.main


if __name__ == "__main__":
    raise SystemExit(main())
