"""Explicit live-browser checks for the resizable trusted embedded chat window.

Run against the manually started trusted demo with:

    RUN_VANNA_E2E=1 VANNA_E2E_BASE_URL=http://127.0.0.1:32010 \
      pytest -m integration tests/e2e/test_trusted_embedded_window.py
"""

from __future__ import annotations

import os

import pytest


pytestmark = pytest.mark.integration

if os.getenv("RUN_VANNA_E2E") != "1":
    pytest.skip(
        "Set RUN_VANNA_E2E=1 to run the live trusted embedded-window test.",
        allow_module_level=True,
    )

playwright = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright.sync_playwright

BASE_URL = os.getenv("VANNA_E2E_BASE_URL", "http://127.0.0.1:32010")


@pytest.fixture()
def page():
    with sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        browser_page = browser.new_page(viewport={"width": 1440, "height": 960})
        errors: list[str] = []
        browser_page.on(
            "console",
            lambda message: errors.append(message.text)
            if message.type == "error"
            else None,
        )
        browser_page.on("pageerror", lambda error: errors.append(str(error)))
        yield browser_page, errors
        browser.close()


def _open_normal_window(browser_page):
    # The component starts an SSE request for its optional starter UI, so a
    # network-idle wait would incorrectly wait for the streaming connection.
    browser_page.goto(f"{BASE_URL}/embedded-demo", wait_until="domcontentloaded")
    chat = browser_page.locator("vanna-chat")
    browser_page.wait_for_selector("vanna-chat.minimized")
    chat.locator(".minimized-icon").click()
    browser_page.wait_for_selector("vanna-chat.normal")
    return chat


def test_desktop_window_drag_resize_and_content_sizing(page) -> None:
    browser_page, console_errors = page
    chat = _open_normal_window(browser_page)
    starter_text = chat.evaluate("element => element.shadowRoot.textContent")
    assert "经营分析副驾" in starter_text
    assert "州前五" in starter_text
    assert "品类前十" in starter_text
    assert chat.locator("textarea.message-input").get_attribute("placeholder") == "输入经营分析问题"
    assert chat.locator("vanna-status-bar").evaluate("element => element.message") == "已就绪"
    assert chat.locator("vanna-status-bar").evaluate("element => element.detail") == "选择示例问题或直接输入"
    window = browser_page.locator("#agent-window")
    before = window.bounding_box()
    assert before is not None

    browser_page.mouse.move(before["x"] + 20, before["y"] + 16)
    browser_page.mouse.down()
    browser_page.mouse.move(before["x"] - 100, before["y"] - 72, steps=8)
    browser_page.mouse.up()
    after_drag = window.bounding_box()
    assert after_drag is not None
    assert after_drag["x"] < before["x"] - 50
    assert after_drag["y"] < before["y"] - 40

    browser_page.mouse.move(
        after_drag["x"] + after_drag["width"] - 5,
        after_drag["y"] + after_drag["height"] - 5,
    )
    browser_page.mouse.down()
    browser_page.mouse.move(
        after_drag["x"] + after_drag["width"] + 100,
        after_drag["y"] + after_drag["height"] + 70,
        steps=8,
    )
    browser_page.mouse.up()
    after_resize = window.bounding_box()
    assert after_resize is not None
    assert after_resize["width"] >= after_drag["width"] + 90
    assert after_resize["height"] >= after_drag["height"] + 60

    dimensions = chat.evaluate(
        """element => {
          const layout = element.shadowRoot.querySelector('.chat-layout').getBoundingClientRect();
          const outer = document.querySelector('#agent-window').getBoundingClientRect();
          const host = element.getBoundingClientRect();
          return { outerHeight: outer.height, hostHeight: host.height, layoutHeight: layout.height };
        }"""
    )
    assert dimensions["hostHeight"] == pytest.approx(dimensions["outerHeight"] - 32, abs=2)
    assert dimensions["layoutHeight"] == pytest.approx(dimensions["hostHeight"], abs=2)

    chat.locator("button.minimize").click()
    browser_page.wait_for_selector("vanna-chat.minimized")
    chat.locator(".minimized-icon").click()
    browser_page.wait_for_selector("vanna-chat.normal")
    assert browser_page.locator("#agent-window").bounding_box()["width"] == pytest.approx(after_resize["width"], abs=2)
    assert not console_errors


def test_mobile_window_is_adaptive_without_horizontal_overflow(page) -> None:
    browser_page, console_errors = page
    browser_page.set_viewport_size({"width": 390, "height": 844})
    chat = _open_normal_window(browser_page)
    dimensions = chat.evaluate(
        """element => {
          const outer = document.querySelector('#agent-window').getBoundingClientRect();
          const layout = element.shadowRoot.querySelector('.chat-layout').getBoundingClientRect();
          return {
            clientWidth: document.documentElement.clientWidth,
            scrollWidth: document.documentElement.scrollWidth,
            outerWidth: outer.width,
            hostWidth: element.getBoundingClientRect().width,
            layoutWidth: layout.width
          };
        }"""
    )
    assert dimensions["scrollWidth"] == dimensions["clientWidth"]
    assert dimensions["outerWidth"] == pytest.approx(358, abs=1)
    assert dimensions["hostWidth"] == pytest.approx(dimensions["outerWidth"], abs=2)
    assert dimensions["layoutWidth"] == pytest.approx(dimensions["hostWidth"], abs=2)
    assert not console_errors
