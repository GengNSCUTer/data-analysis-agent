from data_analysis_agent.result_artifact import ResultArtifact, chart_replay_from_trace
from data_analysis_agent.working_memory import WorkingMemory, replayable_chart_refs


def test_trusted_result_artifact_is_bounded_and_replayable_without_sql() -> None:
    artifact = ResultArtifact.from_trusted_usage(
        request_id="request-123",
        summary="可信结果摘要：GMV 为 100。" + "x" * 2_000,
        catalog_trace={
            "result_contract": {
                "metric_ids": ["gmv"],
                "required_result_columns": ["gmv"],
                "dataset_version": "olist-v1",
                "metric_version": "metrics-v1",
            }
        },
        chart_rendered=True,
    )

    memory = WorkingMemory().with_result_artifact(artifact)
    restored = WorkingMemory.from_mapping(memory.as_dict())

    assert restored.previous_result_artifact == artifact
    assert restored.previous_result_summary == artifact.summary
    assert len(artifact.summary) == 1_200
    assert "不会重新执行 SQL" in artifact.replay_text()
    assert "SELECT" not in str(artifact.as_dict())


def test_trusted_result_artifact_preserves_external_integrity_references() -> None:
    artifact = ResultArtifact.from_trusted_usage(
        request_id="request-with-file",
        summary="可信结果摘要",
        catalog_trace={
            "workspace_id": "olist_analytics",
            "result_artifact": {
                "workspace_id": "olist_analytics",
                "csv_ref": "result.csv",
                "csv_sha256": "a" * 64,
                "csv_bytes": 42,
            },
        },
        chart_rendered=False,
    )
    restored = ResultArtifact.from_mapping(artifact.as_dict())
    assert restored is not None
    assert restored.workspace_id == "olist_analytics"
    assert restored.csv_sha256 == "a" * 64
    assert restored.csv_bytes == 42


def test_chart_history_survives_a_later_result_without_a_chart() -> None:
    chart = ResultArtifact.from_trusted_usage(
        request_id="chart-request",
        summary="各州 GMV 柱状图",
        catalog_trace={
            "result_artifact": {
                "workspace_id": "olist-demo",
                "plotly_ref": "chart.plotly.json",
                "plotly_sha256": "b" * 64,
                "plotly_bytes": 128,
            }
        },
        chart_rendered=True,
    )
    later = ResultArtifact.from_trusted_usage(
        request_id="text-request",
        summary="后来的文字结果",
        catalog_trace={},
        chart_rendered=False,
    )
    memory = WorkingMemory().with_result_artifact(chart).with_result_artifact(later)
    restored = WorkingMemory.from_mapping(memory.as_dict())

    assert later.chart_replay_ref() is None
    assert restored.previous_result_artifact == later
    assert len(restored.chart_history) == 1
    assert restored.chart_history[0].artifact_id == chart.artifact_id
    assert replayable_chart_refs(restored, restored.previous_result_artifact) == restored.chart_history
    assert "图表已保存在会话中" in chart.replay_text()
    legacy = WorkingMemory(previous_result_artifact=chart)
    assert legacy.chart_history == ()
    assert replayable_chart_refs(legacy, chart)[0].artifact_id == chart.artifact_id
    parsed = chart_replay_from_trace(
        {
            "result_artifact": {
                "artifact_id": chart.artifact_id,
                "workspace_id": "olist-demo",
                "plotly_ref": "chart.plotly.json",
                "plotly_sha256": "b" * 64,
                "plotly_bytes": 128,
            }
        },
        "统计2017年各州GMV，画柱状图",
    )
    assert parsed is not None
    assert parsed[0].artifact_id == chart.artifact_id
    assert parsed[1] == "统计2017年各州GMV，画柱状图"
    assert chart_replay_from_trace({"result_artifact": {"plotly_ref": None}}) is None
