"""Exercise actual ASGI sends without buffering the complete download in a client."""

import asyncio
from hashlib import sha256
import io
import threading

import pytest
from starlette.requests import ClientDisconnect

from data_analysis_agent.result_download import ArtifactCsvResponse, open_csv_download


SCOPE = {'type': 'http', 'asgi': {'spec_version': '2.4'}}


class BoundedReader(io.BytesIO):
    def read(self, size=-1):
        assert 0 < size <= 64 * 1024
        return super().read(size)


@pytest.mark.asyncio
async def test_csv_stream_sends_bounded_chunks_and_closes():
    payload = ('品类,金额\r\n家电,12.5\r\n' * 12000).encode()
    handle = BoundedReader(payload)
    response = ArtifactCsvResponse(handle, artifact_id='rta_test', byte_count=len(payload), row_count=12000)
    digest = sha256()
    chunks = 0

    async def send(message):
        nonlocal chunks
        if message['type'] == 'http.response.start':
            headers = dict(message['headers'])
            assert headers[b'content-length'] == str(len(payload)).encode()
            assert headers[b'cache-control'] == b'private, no-store'
        else:
            block = message['body']
            assert len(block) <= 64 * 1024
            chunks += bool(block)
            digest.update(block)

    await response(SCOPE, None, send)
    assert chunks > 1 and digest.digest() == sha256(payload).digest()
    assert handle.closed


@pytest.mark.asyncio
async def test_cancel_during_verification_waits_for_worker_and_closes_file():
    started = threading.Event()
    release = threading.Event()
    handle = BoundedReader(b'csv')
    loop_thread = threading.get_ident()

    class SlowStore:
        def open_verified_csv(self):
            assert threading.get_ident() != loop_thread
            started.set()
            assert release.wait(5)
            return {}, handle

    task = asyncio.create_task(open_csv_download(SlowStore()))
    try:
        await asyncio.wait_for(asyncio.to_thread(started.wait, 3), 4)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert handle.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['headers', 'body', 'cancel'])
async def test_csv_descriptor_closes_on_send_failure_or_cancellation(stage):
    handle = BoundedReader(b'x' * 100000)
    response = ArtifactCsvResponse(handle, artifact_id='rta_test', byte_count=100000, row_count=1)

    async def send(message):
        if stage == 'headers' or message['type'] == 'http.response.body':
            if stage == 'cancel':
                raise asyncio.CancelledError
            raise OSError('client disconnected')

    error = asyncio.CancelledError if stage == 'cancel' else ClientDisconnect
    with pytest.raises(error):
        await response(SCOPE, None, send)
    assert handle.closed


@pytest.mark.asyncio
async def test_asgi_receive_disconnect_closes_file_before_all_rows_are_sent():
    handle = BoundedReader(b'x' * 1024 * 1024)
    response = ArtifactCsvResponse(handle, artifact_id='rta_test', byte_count=1024 * 1024, row_count=1)
    first_chunk = asyncio.Event()
    sent = 0

    async def send(message):
        nonlocal sent
        if message['type'] == 'http.response.body':
            sent += len(message['body'])
            first_chunk.set()
            await asyncio.sleep(0)

    async def receive():
        await first_chunk.wait()
        return {'type': 'http.disconnect'}

    await response({'type': 'http', 'asgi': {'spec_version': '2.3'}}, receive, send)
    assert 0 < sent < 1024 * 1024
    assert handle.closed


@pytest.mark.asyncio
async def test_blocked_disk_read_does_not_block_event_loop():
    started = threading.Event()
    release = threading.Event()
    loop_thread = threading.get_ident()

    class SlowReader(BoundedReader):
        def read(self, size=-1):
            assert threading.get_ident() != loop_thread
            started.set()
            assert release.wait(5)
            return super().read(size)

    handle = SlowReader(b'csv')
    response = ArtifactCsvResponse(handle, artifact_id='rta_test', byte_count=3, row_count=1)

    async def send(message):
        pass

    task = asyncio.create_task(response(SCOPE, None, send))
    try:
        await asyncio.wait_for(asyncio.to_thread(started.wait, 3), 4)
        assert started.is_set()
        # This coroutine can continue while the worker waits on disk.
        assert not task.done()
    finally:
        release.set()
        await task
    assert handle.closed
