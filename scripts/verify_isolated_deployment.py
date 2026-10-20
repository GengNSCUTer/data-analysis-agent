"""Verify an installed wheel against a new, disposable native PostgreSQL cluster.

Run with the NEW environment's Python, not PYTHONPATH or an editable install.
Requires PostgreSQL >=16 binaries, Playwright Chromium and SILICONFLOW_API_KEY.
All state/logs stay in the generated directory, and existing services are untouched.
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import httpx
import pandas as pd
import psycopg2
from playwright.sync_api import sync_playwright
import data_analysis_agent


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-bin", type=Path, required=True)
    parser.add_argument("--output-parent", type=Path, required=True)
    args = parser.parse_args()
    args.output_parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="isolated-", dir=args.output_parent))
    root.chmod(0o700)
    work = root / "empty-workdir"
    work.mkdir()
    state = root / "state"
    state.mkdir()
    pgdata = root / "postgres"
    socket_dir = Path(tempfile.mkdtemp(prefix="daa-deploy-"))
    pgport, httpport = free_port(), free_port()
    url = f"http://127.0.0.1:{httpport}"
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PGPASSWORD", "PGSERVICE", "PGSERVICEFILE"):
        env.pop(key, None)
    admin_password = secrets.token_hex(24)
    pwfile = root / "admin-password"
    pwfile.write_text(admin_password)
    pwfile.chmod(0o600)
    env.update({
        "DATA_ANALYSIS_ENV_FILE": "/dev/null", "DATA_ANALYSIS_STATE_DIR": str(state),
        "DATA_ANALYSIS_POSTGRES_HOST": "127.0.0.1", "DATA_ANALYSIS_POSTGRES_PORT": str(pgport),
        "DATA_ANALYSIS_POSTGRES_DB": "data_analysis_agent",
        "DATA_ANALYSIS_POSTGRES_READER_USER": "daa_analytics_reader",
        "DATA_ANALYSIS_POSTGRES_WRITER_USER": "daa_app_writer",
        "DATA_ANALYSIS_CATALOG_PATH": str(state / "synthetic-catalog.yaml"),
        "DATA_ANALYSIS_DATASET_VERSION": "synthetic-olist-v1",
        "DATA_ANALYSIS_DEMO_PORT": str(httpport), "DATA_ANALYSIS_DEMO_HOST": "127.0.0.1",
        "DATA_ANALYSIS_BOOTSTRAP_USERNAME": "analyst",
        "DATA_ANALYSIS_BOOTSTRAP_PASSWORD": secrets.token_hex(16),
        "DATA_ANALYSIS_BOOTSTRAP_PASSWORD_PATH": str(state / "bootstrap-password.txt"),
        "DATA_ANALYSIS_RESULT_ARTIFACT_ROOT": str(state / "result-artifacts"),
        "VANNA_QUERY_RESULTS_DIR": str(state / "query-results"),
        "PGPASSFILE": str(state / "pgpass"),
    })
    env.pop("DATA_ANALYSIS_DEMO_SESSION_SECRET", None)  # Verify persisted fallback.
    env.pop("DATA_ANALYSIS_TOKENIZER_PATH", None)
    init_env = {**env, "DATA_ANALYSIS_ADMIN_DSN": f"postgresql://app_admin:{admin_password}@127.0.0.1:{pgport}/data_analysis_agent",
                "DATA_ANALYSIS_READER_PASSWORD": secrets.token_hex(24), "DATA_ANALYSIS_WRITER_PASSWORD": secrets.token_hex(24)}
    for key in ("DATA_ANALYSIS_ADMIN_DSN", "DATA_ANALYSIS_READER_PASSWORD", "DATA_ANALYSIS_WRITER_PASSWORD"):
        env.pop(key, None)
    report = {"installed_module": data_analysis_agent.__file__, "python": sys.executable,
              "pg_version": subprocess.check_output([args.pg_bin / "postgres", "--version"], text=True).strip(),
              "root": str(root), "status": "failed", "steps": []}
    assert "site-packages" in data_analysis_agent.__file__, "Requires a wheel installed outside the repository"
    process = None
    pg_started = False
    log = (root / "app.log").open("a")

    def pg(action):
        command = [args.pg_bin / "pg_ctl", "-D", pgdata, "-l", root / "postgres.log"]
        if action == "start":
            command += ["-o", f"-p {pgport} -h 127.0.0.1 -k {socket_dir}"]
        subprocess.run([*command, "-w", action], check=True, stdout=log, stderr=log, env=env)

    def start_app():
        nonlocal process
        process = subprocess.Popen([sys.executable, "-m", "data_analysis_agent.server"],
                                   cwd=work, env=env, stdout=log, stderr=log)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            assert process.poll() is None, f"App startup failed; see {root / 'app.log'}"
            try:
                if httpx.get(url + "/health", timeout=1).is_success:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise AssertionError("Startup timed out")

    def stop_app():
        nonlocal process
        if process is not None:
            process.terminate()
            process.wait(timeout=30)
            process = None

    try:
        subprocess.run([args.pg_bin / "initdb", "-D", pgdata, "-U", "app_admin", "-A", "scram-sha-256", "--pwfile", pwfile], check=True, stdout=log, stderr=log)
        pg("start")
        pg_started = True
        connection = psycopg2.connect(host="127.0.0.1", port=pgport, user="app_admin", password=admin_password, dbname="postgres")
        connection.autocommit = True
        try:
            with connection.cursor() as cursor:
                cursor.execute("CREATE DATABASE data_analysis_agent")
        finally:
            connection.close()
        for _ in range(2):
            subprocess.run([sys.executable, "-m", "data_analysis_agent.deployment", "--synthetic"], cwd=work, env=init_env, check=True, stdout=log, stderr=log)
        report["steps"].append("fresh_initialization_and_idempotent_reapply")
        print("Initialized independent PostgreSQL twice: 1200 orders", flush=True)
        for role, password, statement in (
            ("daa_analytics_reader", init_env["DATA_ANALYSIS_READER_PASSWORD"], "DELETE FROM analytics.fact_orders"),
            ("daa_app_writer", init_env["DATA_ANALYSIS_WRITER_PASSWORD"], "SELECT * FROM analytics.fact_orders"),
        ):
            connection = psycopg2.connect(host="127.0.0.1", port=pgport, dbname="data_analysis_agent", user=role, password=password)
            try:
                with connection.cursor() as cursor:
                    try:
                        cursor.execute(statement)
                    except (psycopg2.errors.InsufficientPrivilege, psycopg2.errors.ReadOnlySqlTransaction):
                        pass
                    else:
                        raise AssertionError(f"Role isolation failed: {role}")
            finally:
                connection.rollback()
                connection.close()
        report["steps"].append("reader_readonly_writer_no_analytics_access")
        start_app()
        assert httpx.get(url + "/api/project/conversations").status_code == 401
        with sync_playwright() as engine:
            browser = engine.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 960})
            page.set_default_timeout(240_000)
            page.goto(url + "/embedded-copilot", wait_until="networkidle")
            page.locator("#login-username").fill("analyst")
            page.locator("#login-password").fill(env["DATA_ANALYSIS_BOOTSTRAP_PASSWORD"])
            page.locator("#login-submit").click()
            page.locator("#login-gate").wait_for(state="hidden")
            report["steps"].append("browser_login")
            print("Installed app and real browser login ready", flush=True)
            assert page.request.get(url + "/api/project/runtime-contract").json()["dataset_version"] == "synthetic-olist-v1"
            chat = page.locator("vanna-chat")

            def send(question):
                print("Sending live analysis request", flush=True)
                chat.locator("textarea.message-input").fill(question)
                with page.expect_response(lambda response: response.url.endswith("/chat_sse") and response.request.post_data_json.get("message") == question, timeout=240_000) as pending:
                    chat.locator("button.send-button").click()
                payload = pending.value.text()
                assert pending.value.ok
                page.wait_for_function("() => !document.querySelector('vanna-chat').shadowRoot.querySelector('textarea.message-input').disabled")
                cid = chat.evaluate("el => el.conversationId")
                detail = page.request.get(f"{url}/api/project/conversations/{cid}").json()
                assert detail["trusted_result_artifact"], payload[-1500:]
                (root / f"response-{len(report['steps'])}.txt").write_text(payload)
                return cid, detail

            cid, detail = send("按客户州统计2017年商品成交额，并画柱状图")
            chat.locator(".js-plotly-plot").last.wait_for(state="visible")
            report["steps"].append("live_model_sql_chart")
            cid, detail = send("改为按客户城市统计，返回完整城市列表，不用画图")
            artifact_id = detail["trusted_result_artifact"]["artifact_id"]
            csv_url = f"{url}/api/project/conversations/{cid}/result.csv"
            original_csv = page.request.get(csv_url)
            assert original_csv.ok
            observed = pd.read_csv(io.StringIO(original_csv.text()))
            assert len(observed) > 200
            with psycopg2.connect(init_env["DATA_ANALYSIS_ADMIN_DSN"]) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT c.customer_city, SUM(i.price) AS gmv FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id JOIN analytics.dim_customers c ON c.customer_id=o.customer_id WHERE o.order_status NOT IN ('canceled','unavailable') AND o.order_purchase_timestamp>='2017-01-01' AND o.order_purchase_timestamp<'2018-01-01' GROUP BY c.customer_city")
                    expected = pd.DataFrame(cursor.fetchall(), columns=[col.name for col in cursor.description])
            pd.testing.assert_frame_equal(observed.sort_values("customer_city").reset_index(drop=True), expected.astype({"gmv": float}).sort_values("customer_city").reset_index(drop=True), check_dtype=False)
            page.locator(".more-menu summary").click()
            page.locator("#browse-result").click()
            page.wait_for_function("() => document.querySelector('#result-page-table tbody')?.children.length===200")
            page.locator("#result-page-next").click()
            page.wait_for_function("() => document.querySelector('#result-page-status').textContent.includes('201–')")
            assert page.locator("#result-page-table tbody tr").count() == len(observed) - 200
            page.locator("#result-page-close").click()
            with page.expect_download() as pending_download:
                page.locator("#download-result").click()
            pending_download.value.save_as(root / "browser-download.csv")
            assert (root / "browser-download.csv").read_bytes() == original_csv.body()
            report["steps"].append("independent_values_pagination_and_full_csv")
            report["city_rows"] = len(observed)
            print("Query, chart, pagination, download and independent SQL values passed", flush=True)
            secret_before = (state / "session-secret").read_text()
            stop_app()
            pg("stop")
            pg_started = False
            pg("start")
            pg_started = True
            start_app()
            page.reload(wait_until="networkidle")
            page.locator("#login-gate").wait_for(state="hidden")
            recovered = page.request.get(f"{url}/api/project/conversations/{cid}")
            assert recovered.ok
            assert recovered.json()["trusted_result_artifact"]["artifact_id"] == artifact_id
            assert page.request.get(csv_url).body() == original_csv.body()
            assert (state / "session-secret").read_text() == secret_before
            report["steps"].append("app_and_database_restart_cookie_conversation_artifact_restore")
            send("保持这些城市和时间不变，改看有效订单数")
            report["steps"].append("live_followup_after_restart")
            page.screenshot(path=str(root / "restored.png"), full_page=True)
            browser.close()
        report["status"] = "passed"
    finally:
        stop_app()
        if pg_started:
            pg("stop")
        log.close()
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
