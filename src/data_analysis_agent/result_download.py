"""Bounded CSV transfer from an already verified, open result file."""

import asyncio
from typing import BinaryIO

import anyio

from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse


async def open_csv_download(store, **identity):
    """Do not strand an open file when cancellation arrives during verification."""
    worker = asyncio.create_task(asyncio.to_thread(store.open_verified_csv, **identity))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            try:
                _, handle = await worker
            except Exception:
                pass  # Preserve request cancellation if verification itself failed.
            else:
                handle.close()
        raise


class ArtifactCsvResponse(StreamingResponse):
    """Own the descriptor through completion, cancellation and send failures."""

    def __init__(self, handle: BinaryIO, *, artifact_id: str, byte_count: int, row_count: int):
        self.handle = handle
        super().__init__(
            self._chunks(), media_type="text/csv",
            headers={
                "Content-Length": str(byte_count),
                "X-Row-Count": str(row_count),
                "Content-Disposition": f'attachment; filename="{artifact_id}.csv"',
                "Cache-Control": "private, no-store",
            },
        )

    async def _chunks(self):
        while chunk := await run_in_threadpool(self.handle.read, 64 * 1024):
            yield chunk

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.handle.close()
