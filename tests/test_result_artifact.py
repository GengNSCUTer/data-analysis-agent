from data_analysis_agent.result_artifact import ResultArtifact
from data_analysis_agent.working_memory import WorkingMemory


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
