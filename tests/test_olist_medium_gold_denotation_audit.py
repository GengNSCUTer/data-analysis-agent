from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from scripts.post_training.evaluation.run_olist_medium_gold_denotation_audit import (
    GoldDenotationAuditError,
    _safe_records,
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


def test_safe_report_cardinality_comes_from_the_frozen_test_contract() -> None:
    report = {
        "records": [
            {"source_id": "case-1", "result_contract_satisfied": True},
            {"source_id": "case-2", "result_contract_satisfied": False},
        ]
    }

    assert set(_safe_records(report, "base", expected_count=2)) == {"case-1", "case-2"}
    with pytest.raises(GoldDenotationAuditError, match="must contain 3 records"):
        _safe_records(report, "base", expected_count=3)
