from vanna.tools.run_sql import result_preview_for_llm


def test_validated_summary_replaces_the_csv_prefix() -> None:
    csv_content = "state,gmv\nSP,10\nRJ,9\n"
    preview = result_preview_for_llm(csv_content, "指标：gmv\nSP 10\nRJ 9")
    assert preview == "指标：gmv\nSP 10\nRJ 9"
    assert "state,gmv" not in preview


def test_csv_fallback_stops_on_a_row_boundary() -> None:
    row = "SP," + ("1" * 80)
    csv_content = "state,gmv\n" + ((row + "\n") * 30)
    preview = result_preview_for_llm(csv_content, "  ")
    body = preview.split("\n(Results truncated", 1)[0]
    assert "truncated to complete rows" in preview
    assert len(body) < 1000
    assert body.splitlines()[-1] == row
