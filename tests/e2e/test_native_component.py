"""Live-browser verification for the embedded Vanna prototype.

This intentionally targets a manually started demo server because it exercises a
real SiliconFlow model call. Run it only after starting the example with:

    RUN_VANNA_E2E=1 pytest -m integration tests/e2e/test_native_component.py
"""

from __future__ import annotations

import os
from pathlib import Path
import time

import pytest


pytestmark = pytest.mark.integration

if os.getenv("RUN_VANNA_E2E") != "1":
    pytest.skip(
        "Set RUN_VANNA_E2E=1 to run the live SiliconFlow browser test.",
        allow_module_level=True,
    )

playwright = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright.sync_playwright

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BASE_URL = os.getenv("VANNA_E2E_BASE_URL", "http://127.0.0.1:32009")
QUERY_RESULTS_DIRECTORY = Path(
    os.getenv("VANNA_QUERY_RESULTS_DIR", "/tmp/data-analysis-agent-vanna-query-results")
)


@pytest.fixture()
def page():
    with sync_playwright() as engine:
        browser = engine.chromium.launch(
            headless=True,
            executable_path="/usr/bin/google-chrome",
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        console_errors: list[str] = []
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: console_errors.append(str(error)))
        yield page, console_errors
        browser.close()


def _window_state(page) -> str:
    return page.locator("vanna-chat").evaluate("element => element.windowState")


def test_embedded_component_window_states_and_sql_result(page) -> None:
    browser_page, console_errors = page
    browser_page.goto(f"{BASE_URL}/embedded-demo", wait_until="networkidle")
    chat = browser_page.locator("vanna-chat")

    browser_page.wait_for_selector("vanna-chat.minimized")
    assert browser_page.locator("body").get_attribute("data-vanna-window-state") == "minimized"
    assert _window_state(browser_page) == "minimized"

    chat.locator(".minimized-icon").click()
    browser_page.wait_for_selector("vanna-chat.normal")
    assert _window_state(browser_page) == "normal"

    chat.locator("button.maximize").click()
    browser_page.wait_for_selector("vanna-chat.maximized")
    assert _window_state(browser_page) == "maximized"

    chat.locator("button.restore").click()
    browser_page.wait_for_selector("vanna-chat.normal")

    chat.locator("button.minimize").click()
    browser_page.wait_for_selector("vanna-chat.minimized")
    chat.locator(".minimized-icon").click()
    browser_page.wait_for_selector("vanna-chat.normal")

    query_started_at = time.time()
    input_box = chat.locator("textarea.message-input")
    input_box.fill("查询 sales_daily 表的前两条记录，使用 SELECT 语句")
    chat.locator("button.send-button").click()

    browser_page.wait_for_function(
        """() => document.querySelector('vanna-chat').shadowRoot.textContent.includes('24680')""",
        timeout=120_000,
    )
    rendered_text = chat.evaluate("element => element.shadowRoot.textContent")
    assert "Query Results" in rendered_text
    assert "business_date" in rendered_text
    assert "24680" in rendered_text
    assert "31800" in rendered_text
    assert not console_errors

    assert any(
        result.stat().st_mtime >= query_started_at
        for result in QUERY_RESULTS_DIRECTORY.glob("*/query_results_*.csv")
    )
    assert not list(REPOSITORY_ROOT.glob("query_results_*.csv"))
    assert not list(REPOSITORY_ROOT.glob("*/query_results_*.csv"))


def test_embedded_host_has_no_mobile_horizontal_overflow(page) -> None:
    browser_page, console_errors = page
    browser_page.set_viewport_size({"width": 390, "height": 844})
    browser_page.goto(f"{BASE_URL}/embedded-demo", wait_until="domcontentloaded")

    browser_page.wait_for_selector("vanna-chat.minimized")
    dimensions = browser_page.evaluate(
        """() => ({
            clientWidth: document.documentElement.clientWidth,
            scrollWidth: document.documentElement.scrollWidth,
            chatWidth: document.querySelector('vanna-chat').getBoundingClientRect().width,
        })"""
    )
    assert dimensions["scrollWidth"] == dimensions["clientWidth"]
    assert dimensions["chatWidth"] == 64
    assert not console_errors


def test_markdown_table_is_rendered_as_html_table(page) -> None:
    browser_page, console_errors = page
    browser_page.goto(f"{BASE_URL}/embedded-demo", wait_until="networkidle")
    browser_page.wait_for_selector("vanna-chat.minimized")
    chat = browser_page.locator("vanna-chat")

    chat.evaluate(
        """element => element.componentManager.processUpdate({
            operation: 'create',
            target_id: 'markdown-table-regression',
            timestamp: new Date().toISOString(),
            component: {
                id: 'markdown-table-regression',
                type: 'text',
                lifecycle: 'create',
                data: {
                    markdown: true,
                    content: '## 经营概览\\n\\n| 指标 | 数值 |\\n| --- | --- |\\n| **总成交额** | 207,630 元 |\\n| 总支付订单数 | 833 单 |\\n\\n> 以上为合成演示数据。'
                },
                children: [],
                visible: true,
                interactive: false,
                timestamp: new Date().toISOString()
            }
        })"""
    )

    table = chat.locator("table.text-markdown-table")
    assert table.count() == 1
    assert table.locator("th").all_inner_texts() == ["指标", "数值"]
    assert table.locator("td").all_inner_texts() == ["总成交额", "207,630 元", "总支付订单数", "833 单"]
    rendered_text = chat.evaluate("element => element.shadowRoot.textContent")
    assert "| 指标 | 数值 |" not in rendered_text
    assert not console_errors


def test_root_page_uses_the_local_component_bundle(page) -> None:
    browser_page, console_errors = page
    browser_page.goto(BASE_URL, wait_until="domcontentloaded")

    assert browser_page.locator('script[type="module"][src="/static/vanna-components.js"]').count() == 1
    assert not console_errors
