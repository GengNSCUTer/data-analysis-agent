"""External, immutable storage for validated result artifacts."""

from __future__ import annotations

import csv
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping


class ResultArtifactIntegrityError(ValueError):
    """An external artifact no longer matches its persisted manifest."""


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

    def verify_manifest(
        self, *, artifact_id: str, user_id: str, workspace_id: str
    ) -> dict[str, Any]:
        """Recompute external payload integrity before a replay/export action.

        The manifest is not itself evidence that a CSV or Plotly payload still
        exists or was not modified after it was written.  This method verifies
        the fixed file names and their persisted byte/hash values without
        returning the payload content.
        """
        manifest = self.load_manifest(
            artifact_id=artifact_id, user_id=user_id, workspace_id=workspace_id
        )
        scope = self._scope(user_id, workspace_id, artifact_id)
        self._verify_file(
            scope=scope,
            ref=manifest.get("csv_ref"),
            expected_ref="result.csv",
            expected_sha256=manifest.get("csv_sha256"),
            expected_bytes=manifest.get("csv_bytes"),
        )
        plotly_ref = manifest.get("plotly_ref")
        if plotly_ref is not None:
            self._verify_file(
                scope=scope,
                ref=plotly_ref,
                expected_ref="chart.plotly.json",
                expected_sha256=manifest.get("plotly_sha256"),
                expected_bytes=manifest.get("plotly_bytes"),
            )
        return manifest

    @staticmethod
    def _verify_file(
        *,
        scope: Path,
        ref: object,
        expected_ref: str,
        expected_sha256: object,
        expected_bytes: object,
    ) -> None:
        if ref != expected_ref:
            raise ResultArtifactIntegrityError("artifact manifest file reference is invalid")
        if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
            raise ResultArtifactIntegrityError("artifact manifest checksum is invalid")
        if not isinstance(expected_bytes, int) or expected_bytes < 0:
            raise ResultArtifactIntegrityError("artifact manifest byte count is invalid")
        path = scope / expected_ref
        if not path.is_file():
            raise ResultArtifactIntegrityError("artifact payload is missing")
        digest, size = _sha256_size(path)
        if digest != expected_sha256 or size != expected_bytes:
            raise ResultArtifactIntegrityError("artifact payload integrity check failed")

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
