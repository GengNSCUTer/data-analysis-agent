#!/usr/bin/env python3
"""Download and freeze a public Hugging Face base model outside the repo."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", default="Qwen/Qwen2.5-Coder-1.5B")
    parser.add_argument("--endpoint", default="https://hf-mirror.com")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--revision", default=None)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    args = parse_args()
    api = HfApi(endpoint=args.endpoint)
    info = api.model_info(args.model_id, revision=args.revision)
    revision = info.sha
    args.output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = snapshot_download(
        repo_id=args.model_id,
        revision=revision,
        endpoint=args.endpoint,
        local_dir=str(args.output_dir),
    )
    root = Path(downloaded)
    files = []
    for path in sorted(root.rglob("*")):
        if (
            path.is_file()
            and path.name != "download_manifest.json"
            and ".cache" not in path.relative_to(root).parts
        ):
            files.append(
                {
                    "path": str(path.relative_to(root)),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    manifest = {
        "model_id": args.model_id,
        "revision": revision,
        "endpoint": args.endpoint,
        "resolved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "pipeline_tag": info.pipeline_tag,
        "license": "Apache-2.0 (model card metadata; verify before redistribution)",
        "storage": str(root),
        "files": files,
    }
    (root / "download_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
