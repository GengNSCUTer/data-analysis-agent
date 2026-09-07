from __future__ import annotations

from decimal import Decimal

import pandas as pd

from scripts.post_training.evaluation.run_olist_medium_gold_denotation_audit import (
    compare_denotations,
)


def test_denotation_compare_accepts_identical_ordered_rows() -> None:
    gold = pd.DataFrame({"customer_state": ["SP", "RJ"], "gmv": [Decimal("10.00"), Decimal("5.00")]})
    candidate = gold.copy()

    assert compare_denotations(gold, candidate) == "ordered_denotation_match"


def test_denotation_compare_accepts_order_independent_bag_with_numeric_tolerance() -> None:
    gold = pd.DataFrame({"customer_state": ["SP", "RJ"], "gmv": [10.0, 5.0]})
    candidate = pd.DataFrame({"customer_state": ["RJ", "SP"], "gmv": [5.0000001, 10.0]})

    assert compare_denotations(gold, candidate) == "bag_denotation_match"


def test_denotation_compare_rejects_column_or_value_drift() -> None:
    gold = pd.DataFrame({"gmv": [10]})

    assert compare_denotations(gold, pd.DataFrame({"value": [10]})) == "column_mismatch"
    assert compare_denotations(gold, pd.DataFrame({"gmv": [11]})) == "denotation_mismatch"
