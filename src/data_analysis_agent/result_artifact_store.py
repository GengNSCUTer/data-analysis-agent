"""External, immutable storage for validated result artifacts."""

from __future__ import annotations

import csv
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping


class ResultArtifactStore:
    """Store complete validated results outside conversation metadata."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def save_result_csv(
        self,
        *,
        artifact_id: str,
        user_id: str,
        workspace_id: str,
        columns: Iterable[str],
        rows: Iterable[Mapping[str, Any]],
        row_count: int,
        dataset_version: str,
        metric_version: str,
    ) -> dict[str, Any]:
        scope = self._scope(user_id, workspace_id, artifact_id)
        scope.mkdir(parents=True, exist_ok=True)
        path = scope / "result.csv"
        names = [str(item) for item in columns]
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=scope, delete=False
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=names, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({name: _csv_value(row.get(name)) for name in names})
            temp_name = handle.name
        os.replace(temp_name, path)
        digest, size = _sha256_size(path)
        manifest = {
            "version": "result-artifact-manifest-v1",
            "artifact_id": artifact_id,
            "scope_user_sha256": _fingerprint(user_id),
            "workspace_id": workspace_id[:128],
            "dataset_version": dataset_version[:128],
            "metric_version": metric_version[:128],
            "row_count": max(0, int(row_count)),
            "columns": names[:32],
            "csv_sha256": digest,
            "csv_bytes": size,
            "csv_ref": "result.csv",
            "plotly_ref": None,
        }
        self._write_json_atomic(scope / "manifest.json", manifest)
        return manifest

    def save_plotly_json(
        self,
        *,
        artifact_id: str,
        user_id: str,
        workspace_id: str,
        figure: Mapping[str, Any],
    ) -> dict[str, Any]:
        scope = self._scope(user_id, workspace_id, artifact_id)
        manifest_path = scope / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError("result artifact manifest does not exist")
        path = scope / "chart.plotly.json"
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=scope, delete=False
        ) as handle:
            json.dump(figure, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            temp_name = handle.name
        os.replace(temp_name, path)
        digest, size = _sha256_size(path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["plotly_sha256"] = digest
        manifest["plotly_bytes"] = size
        manifest["plotly_ref"] = "chart.plotly.json"
        self._write_json_atomic(manifest_path, manifest)
        return manifest

    def load_manifest(
        self, *, artifact_id: str, user_id: str, workspace_id: str
    ) -> dict[str, Any]:
        path = self._scope(user_id, workspace_id, artifact_id) / "manifest.json"
        if not path.is_file():
            raise FileNotFoundError("result artifact not found")
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("artifact_id") != artifact_id or value.get("workspace_id") != workspace_id:
            raise PermissionError("result artifact scope mismatch")
        return value

    def _scope(self, user_id: str, workspace_id: str, artifact_id: str) -> Path:
        if not all(isinstance(value, str) and value.strip() for value in (user_id, workspace_id, artifact_id)):
            raise ValueError("artifact scope identifiers must be non-empty")
        return self.root / _fingerprint(user_id) / _safe_component(workspace_id) / _safe_component(artifact_id)

    @staticmethod
    def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            temp_name = handle.name
        os.replace(temp_name, path)


def _fingerprint(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:32]


def _safe_component(value: str) -> str:
    text = value.strip()
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    if not text or any(character not in allowed for character in text):
        raise ValueError("artifact identifier contains unsafe characters")
    return text[:128]


def _sha256_size(path: Path) -> tuple[str, int]:
    digest = sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _csv_value(value: Any) -> str:
    return "" if value is None else str(value)

