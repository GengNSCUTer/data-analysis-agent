"""External, immutable storage for validated result artifacts."""

from __future__ import annotations

import csv
from functools import wraps
from hashlib import sha256
import json
from itertools import islice
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Iterable, Mapping


class ResultArtifactIntegrityError(ValueError):
    """An external artifact no longer matches its persisted manifest."""


class ResultArtifactExpired(FileNotFoundError):
    """The saved payload has exceeded the configured retention period."""


def _artifact_access(method):
    @wraps(method)
    def scoped(self, *args, **kwargs):
        scope = self._scope(kwargs['user_id'], kwargs['workspace_id'], kwargs['artifact_id'])
        with self._lock:
            self._busy[scope] = self._busy.get(scope, 0) + 1
        try:
            return method(self, *args, **kwargs)
        finally:
            with self._lock:
                self._busy[scope] -= 1
                if not self._busy[scope]:
                    del self._busy[scope]
    return scoped


class ResultArtifactStore:
    """Store complete validated results outside conversation metadata."""

    def __init__(self, root: str | Path, *, retention_seconds: int | None = None):
        self.root = Path(root)
        self.retention_seconds = retention_seconds
        self._lock = threading.RLock()
        self._busy = {}

    @_artifact_access
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
        result_metadata: Mapping[str, Any] | None = None,
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
            page_offsets = []
            for index, row in enumerate(rows):
                if index % 200 == 0:
                    page_offsets.append(handle.tell())
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
            "page_index_stride": 200,
            "page_offsets": page_offsets,
            "plotly_ref": None,
            "result_metadata": dict(result_metadata or {}),
        }
        self._write_json_atomic(scope / "manifest.json", manifest)
        return manifest

    @_artifact_access
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

    @_artifact_access
    def load_manifest(
        self, *, artifact_id: str, user_id: str, workspace_id: str
    ) -> dict[str, Any]:
        path = self._scope(user_id, workspace_id, artifact_id) / "manifest.json"
        if not path.is_file():
            raise FileNotFoundError("result artifact not found")
        if self.retention_seconds is not None and time.time() - path.stat().st_mtime >= self.retention_seconds:
            raise ResultArtifactExpired("saved result expired; run the analysis again")
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("artifact_id") != artifact_id or value.get("workspace_id") != workspace_id:
            raise PermissionError("result artifact scope mismatch")
        return value

    @_artifact_access
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

    @_artifact_access
    def load_verified_plotly(
        self, *, artifact_id: str, user_id: str, workspace_id: str
    ) -> dict[str, Any]:
        """Return a hash-checked Plotly figure for conversation replay.

        The figure is limited to traces and layout. Conversation memory stores
        only the file pointer; this read is the first time the payload leaves disk.
        """
        manifest = self.verify_manifest(
            artifact_id=artifact_id, user_id=user_id, workspace_id=workspace_id
        )
        expected_bytes = manifest.get("plotly_bytes")
        if manifest.get("plotly_ref") != "chart.plotly.json":
            raise ResultArtifactIntegrityError("artifact has no chart payload")
        if (
            not isinstance(expected_bytes, int)
            or expected_bytes < 0
            or expected_bytes > _MAX_PLOTLY_REPLAY_BYTES
        ):
            raise ResultArtifactIntegrityError("chart payload is too large to replay")
        path = self._scope(user_id, workspace_id, artifact_id) / "chart.plotly.json"
        figure = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(figure, dict):
            raise ValueError("chart payload is not a figure")
        traces = figure.get("data")
        layout = figure.get("layout")
        if not isinstance(traces, list) or not traces or len(traces) > 32:
            raise ValueError("chart payload has no replayable traces")
        if not isinstance(layout, dict):
            raise ValueError("chart payload has no layout")
        if not all(isinstance(trace, dict) for trace in traces):
            raise ValueError("chart payload traces are invalid")
        return {
            "artifact_id": artifact_id,
            "title": _chart_title(layout),
            "data": traces,
            "layout": layout,
        }

    @_artifact_access
    def verified_csv_path(
        self, *, artifact_id: str, user_id: str, workspace_id: str
    ) -> Path:
        """Return the complete validated CSV only after its manifest checks pass."""

        self.verify_manifest(
            artifact_id=artifact_id, user_id=user_id, workspace_id=workspace_id
        )
        return self._scope(user_id, workspace_id, artifact_id) / "result.csv"

    @_artifact_access
    def open_verified_csv(self, **scope):
        """Open before streaming: an ongoing download survives retention cleanup."""
        return self.verified_csv_path(**scope).open('rb')

    def cleanup_expired(self, *, now: float | None = None) -> dict[str, int]:
        """Delete only known runtime payloads, never experiment directories."""
        removed = removed_bytes = 0
        if self.retention_seconds is None:
            return {'artifacts': 0, 'bytes': 0}
        cutoff = (time.time() if now is None else now) - self.retention_seconds
        for manifest in self.root.glob('*/*/*/manifest.json'):
            scope = manifest.parent
            # Never follow links outside the configured runtime root.
            if any(path.is_symlink() for path in (manifest, scope, scope.parent, scope.parent.parent)):
                continue
            with self._lock:
                if scope in self._busy or not manifest.exists() or manifest.stat().st_mtime > cutoff:
                    continue
                for name in ('result.csv', 'chart.plotly.json', 'manifest.json'):
                    path = scope / name
                    if path.is_file() and not path.is_symlink():
                        removed_bytes += path.stat().st_size
                        path.unlink()
                if not any(scope.iterdir()):
                    scope.rmdir()
                removed += 1
        return {'artifacts': removed, 'bytes': removed_bytes}

    @_artifact_access
    def read_csv_page(
        self, *, artifact_id: str, user_id: str, workspace_id: str,
        cursor: int = 0, page_size: int = 200,
    ) -> dict[str, Any]:
        """Read a stable page from an immutable verified result, never rerun SQL.

        The cursor is a row position in this artifact, not OFFSET in a live
        database. A sparse byte index avoids rereading deep preceding pages.
        """
        if cursor < 0 or not 1 <= page_size <= 500:
            raise ValueError("cursor must be nonnegative and page size must be 1..500")
        manifest = self.verify_manifest(
            artifact_id=artifact_id, user_id=user_id, workspace_id=workspace_id,
        )
        total = manifest["row_count"]
        rows = []
        if cursor < total:
            path = self._scope(user_id, workspace_id, artifact_id) / "result.csv"
            with path.open(encoding="utf-8", newline="") as handle:
                offsets = manifest.get("page_offsets")
                block = cursor // 200
                if offsets:
                    handle.seek(offsets[block])
                    reader = csv.DictReader(handle, fieldnames=manifest["columns"])
                    skip = cursor % 200
                else:
                    # Old artifacts have at most the old bounded result size.
                    reader = csv.DictReader(handle)
                    skip = cursor
                rows = list(islice(reader, skip, skip + page_size))
        next_cursor = cursor + len(rows)
        return {
            "query_mode": "detail_page", "result_status": "detail_page",
            "artifact_id": artifact_id, "columns": manifest["columns"], "rows": rows,
            "total_rows": total, "cursor": cursor, "page_rows": len(rows),
            "page_size": page_size, "has_next_page": next_cursor < total,
            "next_cursor": next_cursor if next_cursor < total else None,
            "previous_cursor": max(0, cursor - page_size) if cursor > 0 else None,
            "dataset_version": manifest["dataset_version"],
            "metric_version": manifest["metric_version"],
        }

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


_MAX_PLOTLY_REPLAY_BYTES = 1_500_000


def _chart_title(layout: Mapping[str, Any]) -> str:
    raw = layout.get("title")
    text = raw.get("text") if isinstance(raw, Mapping) else raw
    if not isinstance(text, str):
        return "图表"
    cleaned = "".join(character for character in text if character not in "<>")
    return cleaned.strip()[:120] or "图表"


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
