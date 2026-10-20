"""Browser acceptance with a real provider, injected summary failure and restart."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from time import monotonic

import psycopg2
import pytest

if os.getenv("RUN_VANNA_E2E") != "1":
    pytest.skip("Set RUN_VANNA_E2E=1 for live acceptance", allow_module_level=True)

from test_trusted_embedded_window import _bootstrap_password, page as page
from data_analysis_agent.conversation_store import PostgresConversationStore
from data_analysis_agent.postgres_runner import PostgresConnectionSettings
from vanna.core.storage import Message
from vanna.core.user import User

pytestmark = pytest.mark.integration


def test_summary_failure_restart_preserves_raw_history_and_result(page):
    browser, errors = page
    browser.set_default_timeout(240_000)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    root = Path(os.getenv("ANALYSIS_ACCEPTANCE_OUTPUT",
        "/disk2/gengnan/data-analysis-agent-data/evals/background-context-compaction"))
    root.mkdir(parents=True, exist_ok=True)
    records, process, logfile = [], None, None

    def stop():
        nonlocal process, logfile
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process = None
        if logfile is not None:
            logfile.close()
            logfile = None

    def start(fail):
        nonlocal process, logfile
        script = "import uvicorn\nimport data_analysis_agent.server as demo\n"
        if fail:
            script += (
                "async def fail_summary(turns, source):\n"
                "    raise RuntimeError('injected summary failure')\n"
                "demo.build_llm_summary_provider = lambda *args, **kwargs: fail_summary\n"
            )
        script += f"uvicorn.run(demo.create_app(), host='127.0.0.1', port={port})\n"
        logfile = (root / ("restart-failed-provider.log" if fail else "restart-restored-provider.log")).open("w")
        process = subprocess.Popen([sys.executable, "-c", script], stdout=logfile,
                                   stderr=subprocess.STDOUT, cwd=Path(__file__).resolve().parents[2])
        deadline = monotonic() + 45
        while monotonic() < deadline:
            assert process.poll() is None, "isolated test service exited during startup"
            try:
                if browser.request.get(f"{url}/embedded-copilot", timeout=1000).ok:
                    return
            except Exception:
                pass
            browser.wait_for_timeout(250)
        raise AssertionError("isolated test service startup timed out")

    def db_state(cid):
        settings = PostgresConnectionSettings.from_environment()
        connection = psycopg2.connect(host=settings.host, port=settings.port,
                                     database=settings.database, user=settings.writer_user)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT working_memory,message_count FROM app.conversations WHERE conversation_id=%s", (cid,))
                return cursor.fetchone()
        finally:
            connection.close()

    def wait_attempt(cid, status):
        deadline = monotonic() + 100
        while monotonic() < deadline:
            memory, count = db_state(cid)
            if (memory.get("__summary_attempt") or {}).get("status") == status:
                return memory, count
            browser.wait_for_timeout(500)
        raise AssertionError(f"compaction did not reach {status}")

    def send(question):
        chat = browser.locator("vanna-chat")
        started = monotonic()
        chat.locator("textarea.message-input").fill(question)
        with browser.expect_response("**/api/vanna/v2/chat_sse", timeout=240_000) as response:
            chat.locator("button.send-button").click()
        assert response.value.ok
        response.value.text()
        browser.wait_for_function("() => !document.querySelector('vanna-chat')?.shadowRoot?.querySelector('textarea.message-input')?.disabled")
        records.append({"elapsed_ms": round((monotonic() - started) * 1000),
                        "stream": browser.evaluate("() => window.__sseTimings.at(-1)")})

    try:
        start(True)
        assert browser.request.post(f"{url}/api/auth/login", data={
            "username": "analyst", "password": _bootstrap_password()}).ok
        browser.goto(f"{url}/embedded-copilot", wait_until="domcontentloaded")
        browser.locator("#login-gate").wait_for(state="hidden")
        browser.locator("#new-analysis").click()
        send("统计2017年各客户州的商品成交额，按成交额降序排列")
        cid = browser.locator("vanna-chat").evaluate("el => el.conversationId")
        original = browser.request.get(f"{url}/api/project/conversations/{cid}").json()
        artifact_id = original["trusted_result_artifact"]["artifact_id"]
        task_id = original["analysis_state"]["task_id"]
        before = browser.request.get(f"{url}/api/project/conversations/{cid}/execution-memory").json()["executions"]

        async def arrange():
            store = PostgresConversationStore(PostgresConnectionSettings.from_environment())
            conversation = await store.get_conversation(cid, User(id="local:analyst", group_memberships=["analyst"]))
            for _ in range(24):
                await store.append_turn_messages(conversation, [
                    Message(role="user", content="分析目标是理解客户地区分布，先给结论，差异原因留到下一步，不更改查询条件。" * 19),
                    Message(role="assistant", content="按客户地区理解，保留当前受控状态，差异原因尚待解释，不添加新的数据事实。" * 19),
                ])
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(asyncio.run, arrange()).result()
        send("解释刚才已验证的结果，不改条件，不重新查询")
        failed, count = wait_attempt(cid, "failed")
        assert not failed.get("__history_summary")
        assert failed["__summary_attempt"]["error_type"] == "RuntimeError"
        browser.reload(wait_until="domcontentloaded")
        browser.wait_for_function("id => document.querySelector('vanna-chat')?.conversationId === id", arg=cid)
        send("继续解释已有结果，先说结论，保持时间和条件，不重新查询")
        assert not db_state(cid)[0].get("__history_summary")
        stop()
        start(False)
        # Local demo signing keys rotate on restart. Reauthenticate without
        # changing the conversation; persistence and login are separate tests.
        assert browser.request.post(f"{url}/api/auth/login", data={
            "username": "analyst", "password": _bootstrap_password()}).ok
        browser.reload(wait_until="domcontentloaded")
        browser.wait_for_function("id => document.querySelector('vanna-chat')?.conversationId === id", arg=cid)
        send("继续解释刚才已经验证的结果，保持条件，不重新查询")
        resumed, resumed_count = wait_attempt(cid, "generated")
        assert resumed["__history_summary"]["source_message_end"] >= 0
        assert resumed_count >= count
        detail = browser.request.get(f"{url}/api/project/conversations/{cid}").json()
        assert detail["analysis_state"]["task_id"] == task_id
        assert detail["analysis_state"]["time_range"] == original["analysis_state"]["time_range"]
        assert detail["trusted_result_artifact"]["artifact_id"] == artifact_id
        after = browser.request.get(f"{url}/api/project/conversations/{cid}/execution-memory").json()["executions"]
        assert len(after) == len(before)
        browser.screenshot(path=str(root / "failure-and-process-restart.png"), full_page=True)
        (root / "restart-checks.json").write_text(json.dumps({"conversation_id": cid,
            "failure_injected": True, "raw_history_retained": True,
            "process_restart_resumed": True, "no_new_sql": True,
            "attempt": resumed["__summary_attempt"], "turns": records}, ensure_ascii=False, indent=2))
        assert not errors
    finally:
        stop()
