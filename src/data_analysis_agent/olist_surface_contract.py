"""Stable surface-form contract for the Olist v3.1 Chinese SFT release.

This module deliberately contains only language-layer identities and pure-Chinese
guards.  It does not own metric aliases, QuerySpec parsing, SQL generation, or
runtime routing.  Metric labels are always read from the frozen Catalog by the
surface builder.
"""

from __future__ import annotations

import re


OLIST_V3_1_SURFACE_VERSION = "olist-v3-controlled-surface-overlay-v2"
OLIST_V3_1_VARIANT_SCHEMA_VERSION = "4"
OLIST_V3_1_VARIANT_POLICY = (
    "eight_controlled_pure_zh_catalog_grounded_forms_one_query_instance"
)

# The IDs, not the display names, are the immutable contract used by loaders,
# primary-form allocation, and release audits.  "analysis_cut_front" means
# dimension-first for grouped rows, grain-first for time series, and explicit
# overall-scope-first for scalar rows; a scalar QuerySpec must not invent a
# grouping dimension just to fit a language template.
OLIST_V3_1_VARIANTS: tuple[tuple[str, str], ...] = (
    ("v1", "formal_request"),
    ("v2", "colloquial_request"),
    ("v3", "manager_request"),
    ("v4", "concise_request"),
    ("v5", "result_oriented_request"),
    ("v6", "time_front_request"),
    ("v7", "analysis_cut_front_request"),
    ("v8", "catalog_alias_request"),
)
OLIST_V3_1_VARIANT_IDS = tuple(item[0] for item in OLIST_V3_1_VARIANTS)
OLIST_V3_1_VARIANT_KIND_BY_ID = dict(OLIST_V3_1_VARIANTS)
OLIST_V3_1_VARIANTS_PER_SEED = len(OLIST_V3_1_VARIANTS)

# A pure-Chinese primary release deliberately does not accept Latin words or
# abbreviations.  A separate multilingual overlay may define a different
# contract later; it must not silently contaminate this release.
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z]+")


def contains_latin_token(value: str) -> bool:
    """Return whether a question/alias has an ASCII Latin token."""
    return bool(_LATIN_TOKEN_RE.search(value))


def is_pure_zh_catalog_label(value: str) -> bool:
    """Accept a non-empty Catalog name/alias that has no Latin token."""
    return bool(value.strip()) and not contains_latin_token(value)
