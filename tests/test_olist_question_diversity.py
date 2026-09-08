from scripts.post_training.data.audit_olist_question_diversity import audit_rows, normalize_question


def _row(question: str, family: str = "f1", metric: str = "gmv") -> dict[str, object]:
    return {
        "case_id": f"case-{family}-{metric}-{len(question)}",
        "family_id": family,
        "question": question,
        "query_plan": {
            "metric_ids": [metric],
            "result_shape": "scalar",
            "dimension": None,
            "time_range": {"mode": "absolute_range", "start": "2020-01-01", "end": "2021-01-01"},
            "join_program_id": "orders",
        },
    }


def test_normalization_collapses_literal_dates_metrics_and_dimensions() -> None:
    assert normalize_question("请统计2020-01-01至2021-01-01各客户州的成交金额。") == normalize_question(
        "请统计2022-01-01至2023-01-01各客户州的销售额。"
    )


def test_audit_reports_duplicates_aliases_and_family_variants() -> None:
    rows = [
        _row("请统计2020-01-01至2021-01-01的成交金额。", "f1"),
        _row("看一下2020年到2021年的销售额。", "f1"),
        _row("请统计2020-01-01至2021-01-01各客户州的订单量。", "f2", "paid_order_count"),
    ]
    report = audit_rows(rows)
    assert report["row_count"] == 3
    assert report["unique_question_count"] == 3
    assert report["family_count"] == 2
    assert report["family_variant_count_distribution"] == {1: 1, 2: 1}
    assert report["opening_phrase_distribution"]["请统计"] == 2
    assert report["time_expression_distribution"]["absolute_range"] == 3
    assert report["split_distribution"] == {"unknown": 3}
    assert report["result_shape_distribution"] == {"scalar": 3}
    assert report["metric_count_distribution"] == {"1": 3}
    assert report["metric_alias_hit_distribution"]["gmv"] == 2
    assert report["metric_alias_hit_distribution"]["paid_order_count"] == 1
