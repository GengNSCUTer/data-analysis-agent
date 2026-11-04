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


def test_csv_missing_groups_stay_missing_without_losing_literal_nan_names(tmp_path):
    import csv
    import io
    import pandas as pd
    store = ResultArtifactStore(tmp_path)
    values = [None, float("nan"), pd.NA, "nan", "null", 0, [1, 2]]
    store.save_result_csv(artifact_id="nulls", user_id="u", workspace_id="w", columns=["category"],
        rows=[{"category": v} for v in values], row_count=len(values), dataset_version="v", metric_version="m")
    _, handle = store.open_verified_csv(artifact_id="nulls", user_id="u", workspace_id="w", dataset_version="v", metric_version="m")
    with handle:
        actual = list(csv.DictReader(io.StringIO(handle.read().decode())))
    assert [row["category"] for row in actual] == ["", "", "", "nan", "null", "0", "[1, 2]"]


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
    manifest, handle = store.open_verified_csv(
        artifact_id="rta_test", user_id="user-a", workspace_id="olist_analytics",
        dataset_version="v1", metric_version="m1",
    )
    with handle:
        assert handle.read().decode().splitlines()[0] == "gmv"
    assert manifest["row_count"] == 1
    with pytest.raises(FileNotFoundError):
        store.open_verified_csv(
            artifact_id="rta_test", user_id="user-b", workspace_id="olist_analytics",
            dataset_version="v1", metric_version="m1",
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
    _, handle = store.open_verified_csv(**identity, dataset_version='v1', metric_version='m1')
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


def test_download_verifies_once_and_keeps_verified_descriptor(tmp_path, monkeypatch):
    from data_analysis_agent import result_artifact_store as module

    store = ResultArtifactStore(tmp_path)
    identity = dict(artifact_id='download', user_id='user', workspace_id='shop')
    store.save_result_csv(**identity, columns=['n'], rows=[{'n': 42}], row_count=1,
                          dataset_version='v1', metric_version='m1')
    original = module._stream_sha256_size
    calls = []

    def counted(handle):
        calls.append(handle)
        return original(handle)

    monkeypatch.setattr(module, '_stream_sha256_size', counted)
    manifest, handle = store.open_verified_csv(**identity, dataset_version='v1', metric_version='m1')
    try:
        assert calls == [handle] and handle.tell() == 0
        path = store._scope('user', 'shop', 'download') / 'result.csv'
        replacement = path.with_suffix('.replacement')
        replacement.write_bytes(b'n\r\n999\r\n')
        os.replace(replacement, path)
        assert handle.read() == b'n\r\n42\r\n'
        assert manifest['csv_bytes'] == 7
    finally:
        handle.close()
    with pytest.raises(ResultArtifactIntegrityError):
        store.open_verified_csv(**identity, dataset_version='v1', metric_version='m1')


@pytest.mark.parametrize('change', ['dataset_version', 'metric_version', 'csv', 'chart'])
def test_download_rejects_version_drift_and_tampered_payloads(tmp_path, change):
    store = ResultArtifactStore(tmp_path)
    identity = dict(artifact_id='download', user_id='user', workspace_id='shop')
    store.save_result_csv(**identity, columns=['n'], rows=[{'n': 42}], row_count=1,
                          dataset_version='v1', metric_version='m1')
    store.save_plotly_json(**identity, figure={'data': [], 'layout': {}})
    scope = store._scope('user', 'shop', 'download')
    versions = dict(dataset_version='v1', metric_version='m1')
    if change in versions:
        versions[change] = 'other'
    else:
        filename = 'result.csv' if change == 'csv' else 'chart.plotly.json'
        (scope / filename).write_text('changed')
    with pytest.raises(ResultArtifactIntegrityError):
        store.open_verified_csv(**identity, **versions)


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
