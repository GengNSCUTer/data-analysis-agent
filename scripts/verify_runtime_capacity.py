"""Exercise the product with a controlled provider and real PostgreSQL.

No success SSE is mocked. Intent parsing, QueryPlan, Agent/tool, SQL policy,
reader role, ResultValidator, CSV and Plotly all run through create_app().
The fixed provider tests service capacity, not natural-language SQL quality.
Evidence stays in --output; an isolated process/state directory is used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import uuid

import httpx
import psycopg2
import psycopg2.extras

SQL = """SELECT c.customer_state, COUNT(DISTINCT o.order_id) AS paid_order_count
FROM analytics.fact_orders o JOIN analytics.dim_customers c ON o.customer_id=c.customer_id
WHERE o.order_status NOT IN ('canceled','unavailable')
GROUP BY c.customer_state ORDER BY paid_order_count DESC"""
QUESTION = "按客户州统计有效订单数，并画柱状图"
ACCOUNT_PREFIX = os.getenv("CAPACITY_ACCOUNT_PREFIX", "capacity")


def serve(args):
    import uvicorn
    from data_analysis_agent.local_auth import AccountStore
    from data_analysis_agent.runtime_control import CURRENT_REQUEST
    from data_analysis_agent.server import create_app
    from vanna.core.llm import LlmResponse, LlmStreamChunk
    from vanna.core.tool import ToolCall

    class ControlledProvider:
        model = "controlled-capacity-provider"

        async def delay(self, request):
            await asyncio.sleep(
                20
                if any(
                    "CAPACITY_SLOW" in message.content for message in request.messages
                )
                else 0.1
            )

        async def send_request(self, request):
            await self.delay(request)
            if request.metadata.get("purpose") == "result_explanation":
                from data_analysis_agent.result_evidence import RESULT_EVIDENCE_PREFIX
                text = request.messages[-1].content.removeprefix(RESULT_EVIDENCE_PREFIX)
                data = json.loads(text)
                return LlmResponse(content=json.dumps({"facts": [
                    {"kind": "row", "row_number": n} for n in data["row_numbers"]
                ]}))
            assert request.metadata.get("purpose") == "semantic_intent"
            return LlmResponse(
                content=json.dumps(
                    {
                        "request_kind": "data_query",
                        "action": "new_task",
                        "metric_ids": ["paid_order_count"],
                        "dimensions": ["customer_state"],
                        "chart_request": "bar",
                        "confidence": 0.99,
                    }
                )
            )

        async def stream_request(self, request):
            await self.delay(request)
            yield LlmStreamChunk(
                tool_calls=[
                    ToolCall(id="fixed-sql", name="run_sql", arguments={"sql": SQL})
                ],
                finish_reason="tool_calls",
            )

        async def validate_tools(self, tools):
            return []

        async def aclose(self):
            pass

    store = AccountStore()
    for suffix in ("1", "5", "10", "browser", "admin"):
        name = ACCOUNT_PREFIX + suffix
        store.create_account(
            name,
            os.environ["CAPACITY_TEST_PASSWORD"],
            "admin" if name.endswith("admin") else "analyst",
        )
    app = create_app(llm_provider=ControlledProvider())
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        log_level="info",
        timeout_graceful_shutdown=10,
        proxy_headers=False,
    )


def percentile(values, quantile):
    values = sorted(values)
    return (
        round(values[min(len(values) - 1, int((len(values) - 1) * quantile))], 2)
        if values
        else None
    )


async def collect_level(url, password, concurrency):
    semaphore = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(base_url=url, timeout=60) as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": f"{ACCOUNT_PREFIX}{concurrency}", "password": password},
        )
        login.raise_for_status()

        async def question(index):
            async with semaphore:
                request_id = f"capacity-{concurrency}-{uuid.uuid4().hex}"
                record = {
                    "request_id": request_id,
                    "first_effective_ms": None,
                    "chart": False,
                    "table": False,
                }
                started = time.perf_counter()
                async with client.stream(
                    "POST",
                    "/api/vanna/v2/chat_sse",
                    json={
                        "message": QUESTION,
                        "conversation_id": f"capacity-{uuid.uuid4().hex}",
                        "request_id": request_id,
                    },
                ) as response:
                    record["http_status"] = response.status_code
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data: ") or line == "data: [DONE]":
                            continue
                        event = json.loads(line[6:])
                        kind = (event.get("rich") or {}).get("type")
                        record["chart"] |= kind in ("chart", "plotly")
                        record["table"] |= kind == "dataframe"
                        if record["first_effective_ms"] is None and kind in (
                            "text",
                            "dataframe",
                            "chart",
                            "plotly",
                        ):
                            record["first_effective_ms"] = round(
                                (time.perf_counter() - started) * 1000, 2
                            )
                record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
                return record

        return await asyncio.gather(*(question(index) for index in range(20)))


def process_stats(pid):
    """Linux test-host RSS and CPU, without a product monitoring dependency."""
    status = Path(f"/proc/{pid}/status").read_text()
    rss = (
        next(
            int(line.split()[1])
            for line in status.splitlines()
            if line.startswith("VmRSS:")
        )
        * 1024
    )
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    cpu = (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    return rss, cpu


async def measured_level(url, password, concurrency, pid):
    from data_analysis_agent.postgres_runner import PostgresConnectionSettings

    settings = PostgresConnectionSettings.from_environment()
    _, cpu_before = process_stats(pid)
    peak_rss = 0
    peak_backends = 0
    finished = asyncio.Event()
    connection = psycopg2.connect(
        host=settings.host,
        port=settings.port,
        dbname=settings.database,
        user=settings.writer_user,
    )
    connection.autocommit = True

    def database_backends():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT numbackends FROM pg_stat_database WHERE datname=current_database()"
            )
            return cursor.fetchone()[0]

    async def monitor():
        nonlocal peak_rss, peak_backends
        while not finished.is_set():
            peak_rss = max(peak_rss, process_stats(pid)[0])
            peak_backends = max(
                peak_backends, await asyncio.to_thread(database_backends)
            )
            try:
                await asyncio.wait_for(finished.wait(), 0.1)
            except TimeoutError:
                pass

    task = asyncio.create_task(monitor())
    try:
        records = await collect_level(url, password, concurrency)
    finally:
        finished.set()
        await task
        connection.close()
    _, cpu_after = process_stats(pid)
    return records, {
        "rss_peak_mb": round(peak_rss / 1024**2, 2),
        "database_backends_peak_including_monitor": peak_backends,
        "cpu_seconds": round(cpu_after - cpu_before, 2),
    }


def trace_records(records):
    from data_analysis_agent.postgres_runner import PostgresConnectionSettings

    settings = PostgresConnectionSettings.from_environment()
    connection = psycopg2.connect(
        host=settings.host,
        port=settings.port,
        dbname=settings.database,
        user=settings.writer_user,
    )
    try:
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(
                "SELECT request_id,termination_reason,sql_calls_used,catalog_trace FROM app.agent_runs WHERE request_id=ANY(%s)",
                ([record["request_id"] for record in records],),
            )
            rows = {row["request_id"]: dict(row) for row in cursor.fetchall()}
        for record in records:
            record["run"] = rows.get(record["request_id"])
            trace = (record["run"] or {}).get("catalog_trace") or {}
            record["passed"] = bool(
                record["run"]
                and record["run"]["termination_reason"] == "completed"
                and trace.get("result_contract_satisfied")
                and record["chart"]
                and record["table"]
            )
    finally:
        connection.close()


def browser_checks(url, password, output):
    from playwright.sync_api import sync_playwright
    from data_analysis_agent.local_auth import AccountStore

    with sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        calls = []
        page.on(
            "request",
            lambda request: (
                calls.append(request.url) if "/api/vanna/v2/" in request.url else None
            ),
        )
        assert page.request.post(
            url + "/api/auth/login",
            data={"username": ACCOUNT_PREFIX + "browser", "password": password},
        ).ok
        page.goto(url + "/embedded-copilot", wait_until="networkidle")
        page.locator("#login-gate").wait_for(state="hidden")
        chat = page.locator("vanna-chat")
        page.wait_for_function(
            "() => !!document.querySelector('vanna-chat').shadowRoot.querySelector('textarea.message-input')"
        )
        chat.locator("textarea").fill(QUESTION + " CAPACITY_SLOW")
        chat.get_by_role(
            "button", name="发送消息", exact=False
        ).click() if chat.get_by_role(
            "button", name="发送消息"
        ).count() else chat.locator("button.send-button").click()
        chat.get_by_role("button", name="停止生成").wait_for()
        chat.get_by_role("button", name="停止生成").click()
        page.wait_for_function(
            "() => !document.querySelector('vanna-chat').shadowRoot.querySelector('textarea').disabled"
        )
        page.screenshot(path=str(output / "browser-cancel.png"), full_page=True)
        assert not any("chat_poll" in call for call in calls)
        assert any("/cancel" in call for call in calls)
        # Disconnect a request in the model phase and poll private diagnostics.
        page.evaluate("""async () => {
          const controller = new AbortController();
          const response = await fetch('/api/vanna/v2/chat_sse', {method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({message:'按州统计有效订单数 CAPACITY_SLOW', conversation_id:'disconnect-'+Date.now(), request_id:'disconnect-'+Date.now()}),signal:controller.signal});
          const reader=response.body.getReader(); await reader.read(); controller.abort();
        }""")
        admin = httpx.Client(base_url=url)
        admin.post(
            "/api/auth/login",
            json={"username": ACCOUNT_PREFIX + "admin", "password": password},
        ).raise_for_status()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            capacity = admin.get("/api/project/diagnostics").json()["capacity"]
            if capacity["inflight"] == 0 and capacity["models"]["active"] == 0:
                break
            time.sleep(0.1)
        assert (
            capacity["inflight"]
            == capacity["requests"]["active"]
            == capacity["models"]["active"]
            == 0
        )
        assert capacity["outcomes"].get("user_cancelled") == 1
        assert capacity["outcomes"].get("client_disconnected", 0) >= 1
        # A fresh successful request and reload recover the same artifact.
        page.reload(wait_until="networkidle")
        page.locator("#login-gate").wait_for(state="hidden")
        chat.locator("textarea").fill(QUESTION)
        with page.expect_response(
            lambda response: (
                response.url.endswith("/chat_sse")
                and response.request.post_data_json.get("message") == QUESTION
            )
        ) as pending:
            chat.locator("button.send-button").click()
        assert "[DONE]" in pending.value.text()
        page.wait_for_function(
            "() => !document.querySelector('vanna-chat').shadowRoot.querySelector('textarea').disabled"
        )
        cid = chat.evaluate("el=>el.conversationId")
        before = page.request.get(url + f"/api/project/conversations/{cid}").json()
        artifact = before["trusted_result_artifact"]
        assert artifact and before["charts"]
        csv_url = url + f"/api/project/conversations/{cid}/result.csv"
        original_csv = page.request.get(csv_url)
        assert original_csv.ok
        # Cross-user export is denied even when the artifact ID is known.
        assert admin.get(csv_url).status_code == 404
        page.reload(wait_until="networkidle")
        after = page.request.get(url + f"/api/project/conversations/{cid}").json()
        assert (
            after["trusted_result_artifact"]["artifact_id"] == artifact["artifact_id"]
        )
        assert page.request.get(csv_url).body() == original_csv.body()
        chat.locator(".js-plotly-plot").last.wait_for(state="visible")
        page.screenshot(path=str(output / "browser-recovery.png"), full_page=True)
        # A broken SSE must not silently execute the same question via poll.
        before_calls = len(calls)
        page.route(
            "**/api/vanna/v2/chat_sse", lambda route: route.abort("connectionfailed")
        )
        chat.locator("textarea").fill(QUESTION)
        chat.locator("button.send-button").click()
        page.wait_for_function(
            "() => !document.querySelector('vanna-chat').shadowRoot.querySelector('textarea').disabled"
        )
        failed_calls = calls[before_calls:]
        assert sum("chat_sse" in call for call in failed_calls) == 1
        assert not any("chat_poll" in call for call in failed_calls)
        page.screenshot(
            path=str(output / "browser-network-failure.png"), full_page=True
        )
        admin.close()
        browser.close()
        return {
            "cancel_disconnect_capacity": capacity,
            "reload_artifact": artifact["artifact_id"],
            "cross_user_download": 404,
            "automatic_poll_requests": sum("chat_poll" in x for x in calls),
            "broken_sse_requests": len(failed_calls),
        }


def main():
    global ACCOUNT_PREFIX
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--browser-only",
        action="store_true",
        help="Run only real browser cancellation/recovery/fault checks",
    )
    args = parser.parse_args()
    if args.serve:
        serve(args)
        return
    ACCOUNT_PREFIX = "cap" + uuid.uuid4().hex[:8]
    args.output.mkdir(parents=True, exist_ok=False)
    args.output.chmod(0o700)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    password = secrets.token_hex(16)
    env = {
        **os.environ,
        "DATA_ANALYSIS_STATE_DIR": str(args.output / "state"),
        "DATA_ANALYSIS_RESULT_ARTIFACT_ROOT": str(
            args.output / "state" / "result-artifacts"
        ),
        "VANNA_QUERY_RESULTS_DIR": str(args.output / "state" / "query-results"),
        "DATA_ANALYSIS_BOOTSTRAP_USERNAME": ACCOUNT_PREFIX + "browser",
        "DATA_ANALYSIS_BOOTSTRAP_PASSWORD": password,
        "DATA_ANALYSIS_BOOTSTRAP_PASSWORD_PATH": str(
            args.output / "state" / "password"
        ),
        "CAPACITY_TEST_PASSWORD": password,
        "CAPACITY_ACCOUNT_PREFIX": ACCOUNT_PREFIX,
        "SILICONFLOW_MODEL": "controlled-capacity-provider",
    }
    # Unique account passwords must remain consistent on reruns, outside Git.
    credentials = args.output / "credentials.json"
    credentials.write_text(json.dumps({"password": password}))
    credentials.chmod(0o600)
    report = {
        "provider": "controlled, 100ms per call",
        "levels": [],
        "status": "failed",
    }
    log = (args.output / "server.log").open("w")
    process = subprocess.Popen(
        [
            sys.executable,
            __file__,
            "--serve",
            "--port",
            str(port),
            "--output",
            str(args.output),
        ],
        env=env,
        stdout=log,
        stderr=log,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):
            if process.poll() is not None:
                raise RuntimeError("test service exited; inspect server.log")
            try:
                if httpx.get(url + "/health", timeout=1).is_success:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        report["baseline_rss_mb"] = round(process_stats(process.pid)[0] / 1024**2, 2)
        for concurrency in () if args.browser_only else (1, 5, 10):
            records, resources = asyncio.run(
                measured_level(url, password, concurrency, process.pid)
            )
            trace_records(records)
            level = {
                "concurrency": concurrency,
                "requests": len(records),
                "passed": sum(r["passed"] for r in records),
                **resources,
                "rss_mb_after": round(process_stats(process.pid)[0] / 1024**2, 2),
                "first_effective_p50_ms": percentile(
                    [
                        r["first_effective_ms"]
                        for r in records
                        if r["first_effective_ms"] is not None
                    ],
                    0.5,
                ),
                "elapsed_p50_ms": percentile([r["elapsed_ms"] for r in records], 0.5),
                "elapsed_p95_ms": percentile([r["elapsed_ms"] for r in records], 0.95),
                "records": records,
            }
            report["levels"].append(level)
            (args.output / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2)
            )
            print({k: v for k, v in level.items() if k != "records"}, flush=True)
            assert level["passed"] == 20, "inspect records before continuing"
        report["browser"] = browser_checks(url, password, args.output)
        with httpx.Client(base_url=url) as client:
            client.post(
                "/api/auth/login",
                json={"username": ACCOUNT_PREFIX + "admin", "password": password},
            ).raise_for_status()
            report["capacity_after"] = client.get("/api/project/diagnostics").json()[
                "capacity"
            ]
        report["status"] = "passed"
    finally:
        process.terminate()
        process.wait(timeout=20)
        log.close()
        report["server_exit_code"] = process.returncode
        (args.output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )


if __name__ == "__main__":
    main()
