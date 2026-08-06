"""Explicit live-browser checks for the resizable trusted embedded chat window.

Run against the manually started trusted demo with:

    RUN_VANNA_E2E=1 VANNA_E2E_BASE_URL=http://127.0.0.1:32010 \
      pytest -m integration tests/e2e/test_trusted_embedded_window.py
"""

from __future__ import annotations

import os
import json
import uuid

import psycopg2
import pytest

from data_analysis_agent.postgres_runner import PostgresConnectionSettings


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


def _mock_text_sse(conversation_id: str, request_id: str, content: str) -> str:
    chunk = {
        "rich": {
            "id": f"e2e-text-{request_id}",
            "type": "text",
            "lifecycle": "create",
            "data": {"content": content, "markdown": True},
            "children": [],
            "timestamp": 0,
            "visible": True,
            "interactive": False,
        },
        "simple": {"type": "text", "data": {"text": content}},
        "conversation_id": conversation_id,
        "request_id": request_id,
        "timestamp": 0,
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n"


def test_browser_multiturn_clarification_preserves_conversation_contract(page) -> None:
    """Exercise the embedded UI contract with deterministic SSE responses.

    The backend QuestionRouter/WorkingMemory no-SQL and carry-forward behavior
    is covered by deterministic Python tests; this browser test verifies that
    the same conversation remains visible and addressable across all rounds.
    """

    browser_page, console_errors = page
    requests: list[dict] = []

    def mock_chat(route) -> None:
        payload = json.loads(route.request.post_data or "{}")
        requests.append(payload)
        message = payload.get("message", "")
        conversation_id = payload.get("conversation_id", "")
        request_id = payload.get("request_id", "e2e-request")
        if not message:
            body = "data: [DONE]\n\n"
        elif message == "本月销售额是多少？":
            body = _mock_text_sse(
                conversation_id,
                request_id,
                "需要补充时间范围，当前轮次未执行 SQL。",
            )
        elif message == "2017-01-01 至 2017-12-31":
            body = _mock_text_sse(
                conversation_id,
                request_id,
                "已沿用上一轮 GMV 指标，并使用 2017 年时间范围完成查询。",
            )
        else:
            body = _mock_text_sse(
                conversation_id,
                request_id,
                "已沿用上一轮 GMV 与时间范围，按州返回结果。",
            )
        route.fulfill(
            status=200,
            headers={"Content-Type": "text/event-stream"},
            body=body,
        )

    browser_page.route("**/api/vanna/v2/chat_sse", mock_chat)
    chat = _open_normal_window(browser_page)
    input_box = chat.locator("textarea.message-input")

    for message, expected in (
        ("本月销售额是多少？", "需要补充时间范围"),
        ("2017-01-01 至 2017-12-31", "已沿用上一轮 GMV 指标"),
        ("按州统计", "按州返回结果"),
    ):
        input_box.fill(message)
        chat.locator("button.send-button").click()
        browser_page.wait_for_function(
            "([needle]) => document.querySelector('vanna-chat')?.shadowRoot?.textContent.includes(needle)",
            arg=[expected],
        )

    analysis_requests = [item for item in requests if item.get("message")]
    assert [item["message"] for item in analysis_requests] == [
        "本月销售额是多少？",
        "2017-01-01 至 2017-12-31",
        "按州统计",
    ]
    assert len({item["conversation_id"] for item in analysis_requests}) == 1
    assert all("sql" not in item for item in analysis_requests)
    rendered = chat.evaluate("element => element.shadowRoot.textContent")
    assert "当前轮次未执行 SQL" in rendered
    assert "2017 年时间范围" in rendered
    assert "按州返回结果" in rendered
    assert not console_errors


@pytest.fixture()
def seeded_history_conversation():
    if os.getenv("RUN_PROJECT_DB") != "1":
        pytest.skip("Set RUN_PROJECT_DB=1 to seed the conversation history fixture.")

    suffix = uuid.uuid4().hex[:12]
    conversation_id = f"e2e-history-{suffix}"
    settings = PostgresConnectionSettings.from_environment()
    connection = psycopg2.connect(
        host=settings.host,
        port=settings.port,
        database=settings.database,
        user=settings.writer_user,
    )
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO app.conversations (
                        conversation_id, user_id, user_role, title,
                        dataset_version_id, metric_version, message_count
                    ) VALUES (%s, %s, 'analyst', %s, %s, %s, 2)
                    """,
                    (
                        conversation_id,
                        "demo-analyst",
                        "历史会话里的问题",
                        "olist-kaggle-v2-2026-08-03",
                        "0.1-draft",
                    ),
                )
                cursor.executemany(
                    """
                    INSERT INTO app.messages (
                        conversation_id, message_index, user_id, user_role, role, content
                    ) VALUES (%s, %s, %s, 'analyst', %s, %s)
                    """,
                    [
                        (conversation_id, 0, "demo-analyst", "user", "历史会话里的问题"),
                        (
                            conversation_id,
                            1,
                            "demo-analyst",
                            "assistant",
                            "## 历史会话里的结论\n\n"
                            "| 指标 | 数值 |\n| --- | --- |\n"
                            "| **GMV** | 207,630 元 |",
                        ),
                    ],
                )
    finally:
        connection.close()
    try:
        yield conversation_id
    finally:
        connection = psycopg2.connect(
            host=settings.host,
            port=settings.port,
            database=settings.database,
            user=settings.writer_user,
        )
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM app.conversations WHERE conversation_id = %s",
                        (conversation_id,),
                    )
        finally:
            connection.close()


def _inject_chart_fixture(chat) -> None:
    """Render a deterministic chart without depending on an online LLM response."""
    chat.evaluate(
        """element => {
          const layout = element.shadowRoot.querySelector('.chat-layout');
          const wrapper = document.createElement('div');
          wrapper.className = 'rich-component rich-chart';
          const chart = document.createElement('plotly-chart');
          chart.data = [{
            x: ['SP', 'RJ', 'MG', 'RS', 'PR'],
            y: [41127, 12698, 11496, 5417, 4983],
            type: 'bar',
            marker: { color: '#0f9ba6' }
          }];
          chart.layout = {
            title: { text: '有效订单数前五州' },
            xaxis: { title: { text: '州代码' } },
            yaxis: { title: { text: '有效订单数' } },
            margin: { l: 56, r: 20, t: 60, b: 56 }
          };
          chart.config = { displayModeBar: false };
          wrapper.appendChild(chart);
          layout.appendChild(wrapper);
        }"""
    )


def _chart_dimensions(chart) -> dict:
    return chart.evaluate(
        """element => {
          const svg = element.shadowRoot.querySelector('svg.main-svg');
          const host = element.getBoundingClientRect();
          const canvas = svg?.getBoundingClientRect();
          return {
            hostWidth: host.width,
            hostRight: host.right,
            canvasWidth: canvas?.width ?? 0,
            canvasRight: canvas?.right ?? 0,
            labels: element.shadowRoot.querySelectorAll('text').length
          };
        }"""
    )


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
    browser_page.wait_for_function(
        """() => {
          const chat = document.querySelector('vanna-chat');
          const layout = chat?.shadowRoot?.querySelector('.chat-layout');
          const input = chat?.shadowRoot?.querySelector('textarea.message-input');
          const host = chat?.getBoundingClientRect();
          const inner = layout?.getBoundingClientRect();
          return Boolean(host && inner && input && host.height > 400
            && inner.height > 300 && input.getBoundingClientRect().bottom <= host.bottom + 1);
        }"""
    )
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


def test_chart_tracks_embedded_window_width_without_overflow(page) -> None:
    browser_page, console_errors = page
    chat = _open_normal_window(browser_page)
    _inject_chart_fixture(chat)
    chart = chat.locator("plotly-chart")
    chart.wait_for()
    browser_page.wait_for_timeout(500)

    initial = _chart_dimensions(chart)
    chat_width = chat.bounding_box()["width"]
    assert initial["labels"] > 0
    assert initial["hostWidth"] < chat_width
    assert initial["canvasWidth"] == pytest.approx(initial["hostWidth"], abs=2)
    assert initial["canvasRight"] <= initial["hostRight"] + 1

    window = browser_page.locator("#agent-window")
    window.evaluate("element => { element.style.width = '610px'; element.style.height = '680px'; }")
    browser_page.wait_for_timeout(500)
    resized = _chart_dimensions(chart)
    assert resized["hostWidth"] >= initial["hostWidth"] + 160
    assert resized["canvasWidth"] == pytest.approx(resized["hostWidth"], abs=2)
    assert resized["canvasRight"] <= resized["hostRight"] + 1

    chat.locator("button.maximize").click()
    browser_page.wait_for_selector("vanna-chat.maximized")
    browser_page.wait_for_timeout(500)
    maximized = _chart_dimensions(chart)
    assert maximized["hostWidth"] > resized["hostWidth"]
    assert maximized["canvasWidth"] == pytest.approx(maximized["hostWidth"], abs=2)
    assert maximized["canvasRight"] <= maximized["hostRight"] + 1
    assert not console_errors


def test_chart_stays_within_the_mobile_embedded_window(page) -> None:
    browser_page, console_errors = page
    browser_page.set_viewport_size({"width": 390, "height": 844})
    chat = _open_normal_window(browser_page)
    _inject_chart_fixture(chat)
    chart = chat.locator("plotly-chart")
    chart.wait_for()
    browser_page.wait_for_timeout(500)

    dimensions = _chart_dimensions(chart)
    document_widths = browser_page.evaluate(
        """() => ({
          clientWidth: document.documentElement.clientWidth,
          scrollWidth: document.documentElement.scrollWidth
        })"""
    )
    assert dimensions["labels"] > 0
    assert dimensions["canvasWidth"] == pytest.approx(dimensions["hostWidth"], abs=2)
    assert dimensions["canvasRight"] <= dimensions["hostRight"] + 1
    assert document_widths["scrollWidth"] == document_widths["clientWidth"]
    assert not console_errors


def test_demo_role_switch_uses_a_signed_session_not_request_headers(page) -> None:
    browser_page, console_errors = page
    browser_page.goto(f"{BASE_URL}/embedded-demo", wait_until="domcontentloaded")

    unsigned_header_role = browser_page.evaluate(
        """async () => (await fetch('/api/project/session', {
          headers: { 'X-Demo-Role': 'admin', 'X-Demo-User': 'demo-admin' }
        })).json()"""
    )
    assert unsigned_header_role["role"] == "analyst"
    assert unsigned_header_role["auth_mode"] == "demo_signed_session"

    with browser_page.expect_navigation(wait_until="domcontentloaded"):
        browser_page.locator("[data-demo-role='admin']").click()

    signed_session = browser_page.evaluate(
        """async () => (await fetch('/api/project/session')).json()"""
    )
    assert signed_session["role"] == "admin"
    assert signed_session["user_id"] == "demo-admin"
    assert signed_session["is_demo"] is True
    assert browser_page.locator("[data-demo-role='admin']").get_attribute("aria-pressed") == "true"
    assert "不是密码登录或生产认证" in browser_page.locator(".demo-session").inner_text()
    assert not console_errors


def test_conversation_history_restore_refresh_and_new_session(
    page, seeded_history_conversation
) -> None:
    browser_page, console_errors = page
    conversation_id = seeded_history_conversation
    browser_page.goto(f"{BASE_URL}/embedded-demo", wait_until="domcontentloaded")

    history_button = browser_page.locator(
        f"button[data-load-conversation='{conversation_id}']"
    )
    history_button.wait_for()
    history_button.click()
    browser_page.wait_for_function(
        """id => {
          const chat = document.querySelector('vanna-chat');
          const messages = Array.from(
            chat?.shadowRoot?.querySelectorAll('vanna-message') || []
          )
            .map(message => message.shadowRoot?.textContent || '')
          .join('\\n');
          return messages.includes('历史会话里的结论')
            && localStorage.getItem('data-analysis-agent-current-conversation-v1') === id;
        }""",
        arg=conversation_id,
    )
    rendered = browser_page.locator("vanna-chat").evaluate(
        """element => Array.from(
          element.shadowRoot?.querySelectorAll('vanna-message') || []
        )
          .map(message => message.shadowRoot?.textContent || '')
          .join('\\n')"""
    )
    assert "历史会话里的问题" in rendered
    assert "历史会话里的结论" in rendered
    history_chat = browser_page.locator("vanna-chat")
    assert history_chat.locator("table.text-markdown-table").count() == 1
    assert history_chat.locator("strong").count() >= 1

    browser_page.reload(wait_until="domcontentloaded")
    browser_page.wait_for_function(
        """id => {
          const chat = document.querySelector('vanna-chat');
          const messages = Array.from(
            chat?.shadowRoot?.querySelectorAll('vanna-message') || []
          )
            .map(message => message.shadowRoot?.textContent || '')
          .join('\\n');
          return messages.includes('历史会话里的结论')
            && localStorage.getItem('data-analysis-agent-current-conversation-v1') === id;
        }""",
        arg=conversation_id,
    )

    history_chat = browser_page.locator("vanna-chat")
    assert history_chat.locator("table.text-markdown-table").count() == 1
    assert history_chat.locator("h2").count() >= 1

    browser_page.locator("#new-conversation").click()
    browser_page.wait_for_function(
        """id => {
          const chat = document.querySelector('vanna-chat');
          const messages = Array.from(
            chat?.shadowRoot?.querySelectorAll('vanna-message') || []
          )
            .map(message => message.shadowRoot?.textContent || '')
          .join('\\n');
          return localStorage.getItem('data-analysis-agent-current-conversation-v1') !== id
            && !messages.includes('历史会话里的结论');
        }""",
        arg=conversation_id,
    )
    assert not console_errors
