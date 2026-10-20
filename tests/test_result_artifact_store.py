from __future__ import annotations

import json
import os
import time

import pytest

from data_analysis_agent.result_artifact_store import (
    ResultArtifactIntegrityError,
    ResultArtifactExpired,
    ResultArtifactStore,
)


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
        figure={
            "data": [{"type": "bar", "x": ["SP"], "y": [10]}],
            "layout": {"title": {"text": "2017年各州GMV"}},
        },
    )
    assert updated["plotly_ref"] == "chart.plotly.json"
    assert store.load_manifest(
        artifact_id="rta_test", user_id="user-a", workspace_id="olist_analytics"
    )["plotly_bytes"] > 0
    verified = store.verify_manifest(
        artifact_id="rta_test", user_id="user-a", workspace_id="olist_analytics"
    )
    assert verified["artifact_id"] == "rta_test"
    figure = store.load_verified_plotly(
        artifact_id="rta_test", user_id="user-a", workspace_id="olist_analytics"
    )
    assert figure["data"][0]["type"] == "bar"
    assert figure["title"] == "2017年各州GMV"
    with pytest.raises(FileNotFoundError):
        store.load_verified_plotly(
            artifact_id="rta_test", user_id="user-b", workspace_id="olist_analytics"
        )


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
    csv_path = store.verified_csv_path(
        artifact_id="rta_test", user_id="user-a", workspace_id="olist_analytics"
    )
    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == "gmv"
    with pytest.raises(FileNotFoundError):
        store.verified_csv_path(
            artifact_id="rta_test", user_id="user-b", workspace_id="olist_analytics"
        )
    with pytest.raises(FileNotFoundError):
        store.load_manifest(
            artifact_id="rta_test", user_id="user-b", workspace_id="olist_analytics"
        )
    with pytest.raises(FileNotFoundError):
        store.load_manifest(
            artifact_id="rta_test", user_id="user-a", workspace_id="other"
        )


def test_result_artifact_store_detects_payload_tampering(tmp_path) -> None:
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
    csv_path = store._scope("user-a", "olist_analytics", "rta_test") / "result.csv"
    csv_path.write_text("gmv\n999\n", encoding="utf-8")

    with pytest.raises(ResultArtifactIntegrityError, match="integrity"):
        store.verify_manifest(
            artifact_id="rta_test", user_id="user-a", workspace_id="olist_analytics"
        )


def test_expiry_cleanup_is_scoped_and_open_download_can_finish(tmp_path):
    store = ResultArtifactStore(tmp_path / 'runtime', retention_seconds=60)
    identity = dict(artifact_id='rta_expire', user_id='user', workspace_id='olist')
    store.save_result_csv(**identity, columns=['n'], rows=[{'n': 1}], row_count=1, dataset_version='v1', metric_version='m1')
    handle = store.open_verified_csv(**identity)
    scope = store._scope('user', 'olist', 'rta_expire')
    manifest = scope / 'manifest.json'
    os.utime(manifest, (time.time() - 120, time.time() - 120))
    with pytest.raises(ResultArtifactExpired):
        store.read_csv_page(**identity)
    external = tmp_path / 'experiment'
    external.mkdir()
    (external / 'manifest.json').write_text('unrelated')
    # A symlink under runtime must not authorize cleanup outside it.
    linked = store.root / 'outside' / 'workspace'
    linked.mkdir(parents=True)
    (linked / 'artifact').symlink_to(external, target_is_directory=True)
    result = store.cleanup_expired()
    assert result['artifacts'] == 1 and result['bytes'] > 0
    assert not manifest.exists()
    assert (external / 'manifest.json').read_text() == 'unrelated'
    assert handle.read().decode().splitlines() == ['n', '1']
    handle.close()
    assert store.cleanup_expired()['artifacts'] == 0


def test_retention_survives_restart_and_skips_artifact_in_use(tmp_path):
    store = ResultArtifactStore(tmp_path, retention_seconds=60)
    identity = dict(artifact_id='rta_keep', user_id='user', workspace_id='olist')
    store.save_result_csv(**identity, columns=['n'], rows=[{'n': 1}], row_count=1, dataset_version='v1', metric_version='m1')
    restarted = ResultArtifactStore(tmp_path, retention_seconds=60)
    assert restarted.read_csv_page(**identity)['rows'] == [{'n': '1'}]
    scope = store._scope('user', 'olist', 'rta_keep')
    os.utime(scope / 'manifest.json', (0, 0))
    store._busy[scope] = 1
    assert store.cleanup_expired()['artifacts'] == 0
    del store._busy[scope]
    assert store.cleanup_expired()['artifacts'] == 1
