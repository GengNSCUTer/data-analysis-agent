from __future__ import annotations

import json

import pytest

from data_analysis_agent.result_artifact_store import ResultArtifactStore


def test_result_artifact_store_writes_csv_manifest_and_plotly(tmp_path) -> None:
    store = ResultArtifactStore(tmp_path)
    manifest = store.save_result_csv(
        artifact_id="rta_test",
        user_id="user-a",
        workspace_id="olist_analytics",
        columns=["customer_state", "gmv"],
        rows=[{"customer_state": "SP", "gmv": 10}],
        row_count=1,
        dataset_version="olist-v1",
        metric_version="metrics-v1",
    )
    assert manifest["csv_ref"] == "result.csv"
    assert len(manifest["csv_sha256"]) == 64
    updated = store.save_plotly_json(
        artifact_id="rta_test",
        user_id="user-a",
        workspace_id="olist_analytics",
        figure={"data": [], "layout": {}},
    )
    assert updated["plotly_ref"] == "chart.plotly.json"
    assert store.load_manifest(
        artifact_id="rta_test", user_id="user-a", workspace_id="olist_analytics"
    )["plotly_bytes"] > 0


def test_result_artifact_store_scopes_users_and_workspaces(tmp_path) -> None:
    store = ResultArtifactStore(tmp_path)
    store.save_result_csv(
        artifact_id="rta_test",
        user_id="user-a",
        workspace_id="olist_analytics",
        columns=["gmv"],
        rows=[{"gmv": 10}],
        row_count=1,
        dataset_version="v1",
        metric_version="m1",
    )
    with pytest.raises(FileNotFoundError):
        store.load_manifest(
            artifact_id="rta_test", user_id="user-b", workspace_id="olist_analytics"
        )
    with pytest.raises(FileNotFoundError):
        store.load_manifest(
            artifact_id="rta_test", user_id="user-a", workspace_id="other"
        )

