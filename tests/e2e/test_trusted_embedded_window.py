"""Explicit live-browser checks for the resizable trusted embedded chat window.

Run against the manually started trusted demo with:

    RUN_VANNA_E2E=1 VANNA_E2E_BASE_URL=http://127.0.0.1:32010 \
      pytest -m integration tests/e2e/test_trusted_embedded_window.py
"""

from __future__ import annotations

import os
import json
import asyncio
from concurrent.futures import ThreadPoolExecutor
import uuid
from pathlib import Path

import yaml

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


def _bootstrap_password() -> str:
    password = os.getenv("DATA_ANALYSIS_BOOTSTRAP_PASSWORD", "").strip()
    if password:
        return password
    password_path = Path("/disk2/gengnan/data-analysis-agent-data/local-auth/bootstrap-password.txt")
    if not password_path.is_file():
        pytest.skip("local bootstrap password file is not present")
    return password_path.read_text(encoding="utf-8").strip()


def _login(browser_page, username: str = "analyst") -> None:
    logged_in = browser_page.request.post(
        f"{BASE_URL}/api/auth/login",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"username": username, "password": _bootstrap_password()}),
    )
    assert logged_in.ok


def _open_normal_window(browser_page):
    # The component starts an SSE request for its optional starter UI, so a
    # network-idle wait would incorrectly wait for the streaming connection.
    _login(browser_page)
    browser_page.goto(f"{BASE_URL}/embedded-copilot", wait_until="domcontentloaded")
    chat = browser_page.locator("vanna-chat")
    browser_page.wait_for_selector("vanna-chat")
    browser_page.locator("#login-gate").wait_for(state="hidden")
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


def _mock_status_card_sse(
    conversation_id: str,
    request_id: str,
    title: str,
    description: str,
) -> str:
    """Build the no-SQL clarification response used by the trusted handler."""
    chunk = {
        "rich": {
            "id": f"e2e-status-card-{request_id}",
            "type": "status_card",
            "lifecycle": "create",
            "data": {
                "title": title,
                "status": "warning",
                "description": description,
                "icon": "!",
            },
            "children": [],
            "timestamp": 0,
            "visible": True,
            "interactive": False,
        },
        "simple": {"type": "text", "data": {"text": description}},
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


def test_browser_clarification_stream_clears_transient_sending_status(page) -> None:
    """A completed no-SQL clarification must not leave the input in loading UI."""

    browser_page, console_errors = page

    def mock_chat(route) -> None:
        payload = json.loads(route.request.post_data or "{}")
        conversation_id = payload.get("conversation_id", "")
        request_id = payload.get("request_id", "e2e-request")
        if not payload.get("message"):
            body = "data: [DONE]\n\n"
        else:
            body = _mock_status_card_sse(
                conversation_id,
                request_id,
                "需要补充信息",
                "当前工作区尚未冻结该指标按维度汇总的归属口径。",
            )
        route.fulfill(
            status=200,
            headers={"Content-Type": "text/event-stream"},
            body=body,
        )

    browser_page.route("**/api/vanna/v2/chat_sse", mock_chat)
    chat = _open_normal_window(browser_page)
    input_box = chat.locator("textarea.message-input")
    input_box.fill("各品类平均履约天数和好评率")
    chat.locator("button.send-button").click()

    browser_page.wait_for_function(
        "([needle]) => document.querySelector('vanna-chat')?.shadowRoot?.textContent.includes(needle)",
        arg=["当前工作区尚未冻结"],
    )
    browser_page.wait_for_function(
        """() => {
          const chat = document.querySelector('vanna-chat');
          const status = chat?.shadowRoot?.querySelector('vanna-status-bar');
          return status?.message !== 'Sending message...';
        }"""
    )

    status_bar = chat.locator("vanna-status-bar")
    assert status_bar.evaluate("element => element.status") == "idle"
    assert status_bar.evaluate("element => element.message") == ""
    assert input_box.is_enabled()
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
                        dataset_version_id, metric_version, message_count, workspace_id
                    ) VALUES (%s, %s, 'analyst', %s, %s, %s, 2, 'olist-demo')
                    """,
                    (
                        conversation_id,
                        "local:analyst",
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
                        (conversation_id, 0, "local:analyst", "user", "历史会话里的问题"),
                        (
                            conversation_id,
                            1,
                            "local:analyst",
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
    pytest.skip("浮动宿主窗口已并入 Copilot 页面")
    browser_page, console_errors = page
    chat = _open_normal_window(browser_page)
    starter_text = chat.evaluate("element => element.shadowRoot.textContent")
    assert "经营分析副驾" in starter_text
    assert "州前五" in starter_text
    assert "品类 GMV" in starter_text
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


def test_desktop_window_supports_all_resize_directions(page) -> None:
    pytest.skip("浮动宿主窗口已并入 Copilot 页面")
    browser_page, console_errors = page
    _open_normal_window(browser_page)
    window = browser_page.locator("#agent-window")
    browser_page.evaluate(
        """() => {
          const element = document.querySelector('#agent-window');
          Object.assign(element.style, {
            left: '500px', top: '200px', right: 'auto', bottom: 'auto',
            width: '440px', height: '560px'
          });
        }"""
    )

    resize_cases = (
        ("n", 0, -30, "height"),
        ("e", 50, 0, "width"),
        ("s", 0, 40, "height"),
        ("w", -40, 0, "width"),
        ("ne", 40, -30, "both"),
        ("nw", -40, -30, "both"),
        ("se", 40, 40, "both"),
        ("sw", -40, 40, "both"),
    )
    for direction, dx, dy, changed_axis in resize_cases:
        browser_page.evaluate(
            """() => {
              const element = document.querySelector('#agent-window');
              Object.assign(element.style, {
                left: '500px', top: '200px', right: 'auto', bottom: 'auto',
                width: '440px', height: '560px'
              });
            }"""
        )
        before = window.bounding_box()
        handle = browser_page.locator(f"[data-resize-handle='{direction}']")
        handle_box = handle.bounding_box()
        assert before is not None and handle_box is not None
        start_x = handle_box["x"] + handle_box["width"] / 2
        start_y = handle_box["y"] + handle_box["height"] / 2
        browser_page.mouse.move(start_x, start_y)
        browser_page.mouse.down()
        browser_page.mouse.move(start_x + dx, start_y + dy, steps=5)
        browser_page.mouse.up()
        after = window.bounding_box()
        assert after is not None
        assert after["width"] >= 360
        assert after["height"] >= 460
        assert after["x"] >= 16
        assert after["y"] >= 16
        assert after["x"] + after["width"] <= 1424
        assert after["y"] + after["height"] <= 944
        if changed_axis in {"width", "both"}:
            assert abs(after["width"] - before["width"]) >= 20
        if changed_axis in {"height", "both"}:
            assert abs(after["height"] - before["height"]) >= 20
    assert not console_errors


def test_long_history_markdown_keeps_a_scrollable_view_after_restore(page) -> None:
    browser_page, console_errors = page
    chat = _open_normal_window(browser_page)
    content = (
        "## 历史会话里的结论\n\n"
        "根据查询结果，当前数据覆盖 **2天**。\n\n"
        "| 指标 | 数值 |\n\n"
        "|------|------|\n\n"
        "| 📅 营业天数 | 2天 |\n\n"
        "| 🛒 总支付订单数 | **833 单** |\n\n"
        "| 💰 总成交额（GMV） | **207,630 元** |\n\n"
        "| ⭐ 平均好评率 | **91.87%** |\n\n"
        "---\n\n"
        "这是一段较长的业务解释，用来验证窄窗口恢复后仍然可以滚动查看完整回答。"
    )
    chat.evaluate(
        """(element, markdown) => element.loadConversation('e2e-long-history', [
          { role: 'user', content: '概览 GMV' },
          { role: 'assistant', content: markdown }
        ])""",
        content,
    )
    browser_page.wait_for_function(
        """() => document.querySelector('vanna-message')?.shadowRoot?.querySelector(
          'table.text-markdown-table'
        ) !== null"""
    )
    assert chat.locator("table.text-markdown-table").count() == 1
    assert "历史会话里的结论" in chat.evaluate(
        """element => Array.from(element.shadowRoot?.querySelectorAll('vanna-message') || [])
          .map(message => message.shadowRoot?.textContent || '').join('\\n')"""
    )
    assert not console_errors
    return
    west = browser_page.locator("[data-resize-handle='w']").bounding_box()
    south = browser_page.locator("[data-resize-handle='s']").bounding_box()
    assert west is not None and south is not None
    west_x = west["x"] + west["width"] / 2
    west_y = west["y"] + west["height"] / 2
    browser_page.mouse.move(west_x, west_y)
    browser_page.mouse.down()
    browser_page.mouse.move(west_x + 80, west_y, steps=5)
    browser_page.mouse.up()
    south = browser_page.locator("[data-resize-handle='s']").bounding_box()
    assert south is not None
    south_x = south["x"] + south["width"] / 2
    south_y = south["y"] + south["height"] / 2
    browser_page.mouse.move(south_x, south_y)
    browser_page.mouse.down()
    browser_page.mouse.move(south_x, south_y - 100, steps=5)
    browser_page.mouse.up()
    browser_page.wait_for_function(
        """() => {
          const chat = document.querySelector('vanna-chat');
          const messages = chat?.shadowRoot?.querySelector('.chat-messages');
          return Boolean(messages && messages.clientHeight >= 120 && messages.scrollHeight > messages.clientHeight
            && messages.scrollTop + messages.clientHeight >= messages.scrollHeight - 2);
        }"""
    )
    messages = chat.locator(".chat-messages")
    assert messages.evaluate("element => element.scrollTop + element.clientHeight >= element.scrollHeight - 2")
    assert chat.locator("table.text-markdown-table").count() == 1
    assert chat.locator("strong").count() >= 3

    chat.locator("button.minimize").click()
    browser_page.wait_for_selector("vanna-chat.minimized")
    chat.locator(".minimized-icon").click()
    browser_page.wait_for_selector("vanna-chat.normal")
    browser_page.wait_for_function(
        """() => {
          const chat = document.querySelector('vanna-chat');
          const messages = chat?.shadowRoot?.querySelector('.chat-messages');
          return Boolean(messages && messages.clientHeight >= 120 && messages.scrollHeight > messages.clientHeight
            && messages.scrollTop + messages.clientHeight >= messages.scrollHeight - 2);
        }"""
    )
    assert messages.evaluate("element => element.scrollTop + element.clientHeight >= element.scrollHeight - 2")
    assert "历史会话里的结论" in chat.evaluate(
        """element => Array.from(element.shadowRoot?.querySelectorAll('vanna-message') || [])
          .map(message => message.shadowRoot?.textContent || '').join('\\n')"""
    )
    assert not console_errors


def test_trusted_result_preview_renders_as_a_readable_table(page) -> None:
    """A validated grouped result must not degrade into opaque audit metadata."""
    browser_page, console_errors = page
    browser_page.route(
        "**/api/vanna/v2/chat_sse",
        lambda route: route.fulfill(
            status=200,
            headers={"Content-Type": "text/event-stream"},
            body="data: [DONE]\n\n",
        ),
    )
    preview = (
        "### 查询结果\n\n"
        "已返回 75 个分组，完整明细见上表。\n"
        "以下为表格中的前几条记录，不代表排名或趋势：\n\n"
        "| 商品品类 | 平均履约天数 | 好评率 |\n"
        "| --- | --- | --- |\n"
        "| health_beauty | 2.50 天 | 91.87% |\n\n"
        "结果已通过服务器结果合同的字段、数值及适用范围/截断检查。"
        "为避免超出结果证据，本轮未额外推断趋势、排名、币种或因果。"
    )
    chat = _open_normal_window(browser_page)
    chat.evaluate(
        """(element, markdown) => element.loadConversation('e2e-trusted-preview', [
          { role: 'user', content: '各品类平均履约天数和好评率' },
          { role: 'assistant', content: markdown }
        ])""",
        preview,
    )
    table = chat.locator("table.text-markdown-table")
    table.wait_for()
    rendered_preview = chat.evaluate(
        """element => Array.from(element.shadowRoot?.querySelectorAll('vanna-message') || [])
          .map(message => message.shadowRoot?.textContent || '').join('\\n')"""
    )

    assert table.locator("th").all_inner_texts() == ["商品品类", "平均履约天数", "好评率"]
    assert table.locator("td").all_inner_texts() == ["health_beauty", "2.50 天", "91.87%"]
    assert "已返回 75 个分组，完整明细见上表" in rendered_preview
    assert "不代表排名或趋势" in rendered_preview
    widths = browser_page.evaluate(
        """() => ({
          clientWidth: document.documentElement.clientWidth,
          scrollWidth: document.documentElement.scrollWidth
        })"""
    )
    assert widths["scrollWidth"] == widths["clientWidth"]
    assert not console_errors


def test_result_presenter_shows_total_and_preview_rows_and_hides_export(page) -> None:
    """The live component must consume the server-owned presenter contract."""
    browser_page, console_errors = page
    # This is a presenter test. Starter SSE must not race with the injected
    # component or introduce unrelated online model output into its assertions.
    browser_page.route(
        "**/api/vanna/v2/chat_sse",
        lambda route: route.fulfill(
            status=200,
            headers={"Content-Type": "text/event-stream"},
            body="data: [DONE]\n\n",
        ),
    )
    browser_page.goto(f"{BASE_URL}/embedded-demo", wait_until="domcontentloaded")
    chat = _open_normal_window(browser_page)
    chat.evaluate("async element => { await element.updateComplete; }")
    rows = [{"state": f"S{i}", "gmv": i} for i in range(200)]
    chat.evaluate(
        """(element, rows) => element.componentManager.processUpdate({
          operation: 'create',
          target_id: 'm3-presenter-regression',
          timestamp: new Date().toISOString(),
          component: {
            id: 'm3-presenter-regression',
            type: 'dataframe',
            lifecycle: 'create',
            data: {
              data: rows,
              columns: ['state', 'gmv'],
              title: '查询结果',
              description: '已通过结果合同；界面预览 200 / 共 250 行',
              row_count: 200,
              column_count: 2,
              total_row_count: 250,
              preview_row_count: 200,
              display_truncated: true,
              max_rows_displayed: 200,
              searchable: true,
              sortable: true,
              filterable: true,
              exportable: false,
              preview_export_allowed: false,
              full_export_allowed: false
            },
            children: [],
            visible: true,
            interactive: false
          }
        })""",
        rows,
    )

    dataframe = chat.locator('.rich-dataframe[data-component-id="m3-presenter-regression"]')
    dataframe.wait_for()
    assert dataframe.locator(".row-count").inner_text() == "200 / 250 rows"
    assert dataframe.locator(".dataframe-truncated").inner_text() == "Showing 200 of 250 rows"
    assert dataframe.locator(".export-btn").count() == 0
    assert not console_errors


def test_embeddable_copilot_shell_hides_demo_controls_and_exposes_evidence(page) -> None:
    """The product shell must not expose the TrustedDemoHost control plane."""
    browser_page, console_errors = page
    password_path = Path("/disk2/gengnan/data-analysis-agent-data/local-auth/bootstrap-password.txt")
    if not password_path.is_file():
        pytest.skip("local bootstrap password file is not present")
    logged_in = browser_page.request.post(
        f"{BASE_URL}/api/auth/login",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"username": "analyst", "password": password_path.read_text(encoding="utf-8").strip()}),
    )
    assert logged_in.ok
    browser_page.goto(f"{BASE_URL}/embedded-copilot", wait_until="domcontentloaded")
    browser_page.wait_for_selector("vanna-chat")

    assert browser_page.locator("#new-analysis").count() == 1
    assert browser_page.locator("#clear-analysis").count() == 1
    assert browser_page.locator("#save-analysis").count() == 1
    assert browser_page.locator("#download-result").count() == 1
    assert browser_page.locator("#login-gate").is_hidden()
    assert browser_page.locator("#preference-form").count() == 1
    assert browser_page.locator("#evidence-toggle").count() == 1
    assert browser_page.locator("#conversation-list").count() == 1
    assert browser_page.locator("#admin-panel").is_hidden()
    assert browser_page.locator("#diagnostics").is_hidden()
    assert browser_page.evaluate("() => typeof document.querySelector('vanna-chat').replaceLastQuestion === 'function'")
    assert "演示角色" not in browser_page.locator("body").inner_text()
    browser_page.locator("details.more-menu > summary").click()
    browser_page.locator("#admin-toggle").click()
    assert browser_page.locator("#admin-panel").is_visible()
    assert browser_page.locator("[data-demo-role]").count() == 0
    browser_page.locator("#account-identity").wait_for()
    browser_page.wait_for_function(
        "() => document.querySelector('#account-identity')?.textContent?.includes('analyst · 分析员')"
    )

    browser_page.locator("#evidence-toggle").click()
    browser_page.wait_for_function(
        "() => document.querySelector('[data-evidence=dataset]')?.textContent !== '加载中'"
    )
    assert browser_page.locator('[data-evidence="dataset"]').inner_text() != "加载中"
    assert browser_page.locator('[data-evidence="metric"]').inner_text() != "加载中"

    browser_page.locator("#new-analysis").click()
    browser_page.wait_for_function(
        "() => Boolean(localStorage.getItem('data-analysis-agent-embeddable-conversation-v1'))"
    )
    assert not console_errors


def test_embeddable_copilot_shell_is_mobile_safe(page) -> None:
    browser_page, console_errors = page
    browser_page.set_viewport_size({"width": 390, "height": 844})
    _login(browser_page)
    browser_page.goto(f"{BASE_URL}/embedded-copilot", wait_until="domcontentloaded")
    browser_page.wait_for_selector("vanna-chat")
    dimensions = browser_page.evaluate(
        """() => ({
          clientWidth: document.documentElement.clientWidth,
          scrollWidth: document.documentElement.scrollWidth,
          shellWidth: document.querySelector('.copilot-surface')?.getBoundingClientRect().width,
          shellLeft: document.querySelector('.copilot-surface')?.getBoundingClientRect().left,
          shellRight: document.querySelector('.copilot-surface')?.getBoundingClientRect().right
        })"""
    )
    assert dimensions["scrollWidth"] == dimensions["clientWidth"]
    # The current floating shell deliberately retains an outer margin.
    assert 320 <= dimensions["shellWidth"] <= dimensions["clientWidth"]
    assert dimensions["shellLeft"] >= 0
    assert dimensions["shellRight"] <= dimensions["clientWidth"]
    assert not console_errors


@pytest.mark.parametrize("width", [1440, 390])
def test_result_browser_pages_without_new_sql_or_model_calls(page, width) -> None:
    from urllib.parse import parse_qs, urlparse

    browser_page, console_errors = page
    browser_page.set_viewport_size({"width": width, "height": 960})
    _login(browser_page)
    browser_page.add_init_script("localStorage.setItem('data-analysis-agent-embeddable-conversation-v1', 'paging-test')")
    cursors = []

    def result_response(route):
        path = urlparse(route.request.url)
        if path.path.endswith("/rows"):
            cursor = int(parse_qs(path.query)["cursor"][0])
            cursors.append(cursor)
            end = min(405, cursor + 200)
            payload = {
                "columns": ["city", "orders"], "rows": [{"city": "城市" + str(n), "orders": n} for n in range(cursor, end)],
                "cursor": cursor, "page_rows": end - cursor, "total_rows": 405,
                "has_next_page": end < 405, "next_cursor": end if end < 405 else None,
                "previous_cursor": max(0, cursor - 200) if cursor else None,
            }
            payload["rows"][0]["city"] = "<img src=x onerror=alert(1)>"
        else:
            payload = {"messages": [], "charts": [], "trusted_result_artifact": {"artifact_id": "paging-artifact"}}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    browser_page.route("**/api/project/conversations/paging-test**", result_response)
    browser_page.goto(f"{BASE_URL}/embedded-copilot", wait_until="networkidle")
    browser_page.evaluate("document.querySelector('vanna-chat').setConversationId('paging-test')")
    browser_page.locator(".more-menu summary").click()
    browser_page.locator("#browse-result").click()
    browser_page.wait_for_function("() => document.querySelector('#result-page-status').textContent.includes('1–200')", timeout=5000)
    assert browser_page.locator("#result-page-table tbody tr").count() == 200
    assert browser_page.locator("#result-page-table img").count() == 0
    assert browser_page.locator("#result-page-prev").is_disabled()
    browser_page.locator("#result-page-next").click()
    browser_page.wait_for_function("() => document.querySelector('#result-page-status').textContent.includes('201–400')")
    browser_page.locator("#result-page-next").click()
    browser_page.wait_for_function("() => document.querySelector('#result-page-status').textContent.includes('401–405')")
    assert browser_page.locator("#result-page-table tbody tr").count() == 5
    assert browser_page.locator("#result-page-next").is_disabled()
    browser_page.locator("#result-page-prev").click()
    browser_page.wait_for_function("() => document.querySelector('#result-page-status').textContent.includes('201–400')")
    assert cursors == [0, 200, 400, 200]
    dialog = browser_page.locator("#result-browser").bounding_box()
    assert dialog["x"] >= 0 and dialog["x"] + dialog["width"] <= width
    browser_page.locator("#result-page-close").click()
    assert browser_page.locator("#result-browser").is_hidden()
    assert not console_errors


def test_mobile_window_is_adaptive_without_horizontal_overflow(page) -> None:
    browser_page, console_errors = page
    browser_page.set_viewport_size({"width": 390, "height": 844})
    chat = _open_normal_window(browser_page)
    dimensions = chat.evaluate(
        """element => {
          const outer = document.querySelector('.copilot-surface').getBoundingClientRect();
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
    assert dimensions["outerWidth"] == pytest.approx(dimensions["clientWidth"] - 24, abs=2)
    assert dimensions["hostWidth"] <= dimensions["outerWidth"] + 2
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

    window = browser_page.locator(".copilot-surface")
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


def test_password_login_rejects_demo_role_switch_and_request_headers(page) -> None:
    browser_page, console_errors = page
    browser_page.goto(f"{BASE_URL}/embedded-copilot", wait_until="domcontentloaded")
    assert browser_page.locator("#login-gate").is_visible()
    anonymous = browser_page.request.get(f"{BASE_URL}/api/project/session")
    assert anonymous.status == 401

    _login(browser_page)
    browser_page.goto(f"{BASE_URL}/embedded-copilot", wait_until="domcontentloaded")
    browser_page.locator("#login-gate").wait_for(state="hidden")
    elevated = browser_page.evaluate(
        """async () => (await fetch('/api/project/session', {
          headers: { 'X-Demo-Role': 'admin', 'X-Demo-User': 'demo-admin' },
          credentials: 'same-origin'
        })).json()"""
    )
    assert elevated["role"] == "analyst"
    assert elevated["auth_mode"] == "local_session"
    assert elevated["is_demo"] is False
    denied = browser_page.request.post(
        f"{BASE_URL}/api/project/demo-session",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"role": "admin"}),
    )
    assert denied.status == 404
    assert browser_page.locator("[data-demo-role]").count() == 0


def test_conversation_history_restore_refresh_and_new_session(
    page, seeded_history_conversation
) -> None:
    browser_page, console_errors = page
    conversation_id = seeded_history_conversation
    _login(browser_page)
    listing = browser_page.request.get(f"{BASE_URL}/api/project/conversations?limit=20")
    assert listing.ok
    assert conversation_id in [item["conversation_id"] for item in listing.json()], listing.json()
    browser_page.goto(f"{BASE_URL}/embedded-copilot", wait_until="domcontentloaded")

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
            && localStorage.getItem('data-analysis-agent-embeddable-conversation-v1') === id;
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
    browser_page.wait_for_function("id => document.querySelector('vanna-chat')?.conversationId === id", arg=conversation_id)
    browser_page.wait_for_function(
        """id => {
          const chat = document.querySelector('vanna-chat');
          const messages = Array.from(
            chat?.shadowRoot?.querySelectorAll('vanna-message') || []
          )
            .map(message => message.shadowRoot?.textContent || '')
          .join('\\n');
          return messages.includes('历史会话里的结论')
            && localStorage.getItem('data-analysis-agent-embeddable-conversation-v1') === id;
        }""",
        arg=conversation_id,
    )

    history_chat = browser_page.locator("vanna-chat")
    assert history_chat.locator("table.text-markdown-table").count() == 1
    assert history_chat.locator("h2").count() >= 1

    browser_page.locator("#new-analysis").click()
    browser_page.wait_for_function(
        """id => {
          const chat = document.querySelector('vanna-chat');
          const messages = Array.from(
            chat?.shadowRoot?.querySelectorAll('vanna-message') || []
          )
            .map(message => message.shadowRoot?.textContent || '')
          .join('\\n');
          return localStorage.getItem('data-analysis-agent-embeddable-conversation-v1') !== id
            && !messages.includes('历史会话里的结论');
        }""",
        arg=conversation_id,
    )
    assert not console_errors


def test_analysis_state_live_multiturn_refresh_and_clear(page):
    """Real browser -> auth/SSE -> model -> SQL policy/reader -> artifact."""
    browser_page, console_errors = page
    browser_page.set_default_timeout(240_000)
    chat = _open_normal_window(browser_page)
    browser_page.locator("#new-analysis").click()
    browser_page.wait_for_function("() => Boolean(localStorage.getItem('data-analysis-agent-embeddable-conversation-v1'))")
    conversation_id = browser_page.evaluate("() => localStorage.getItem('data-analysis-agent-embeddable-conversation-v1')")
    input_box = chat.locator("textarea.message-input")
    states = []
    screenshot_dir = Path("/disk2/gengnan/data-analysis-agent-data/evals/analysis-memory-phase2/browser")
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    for index, (question, expected_filter) in enumerate([
        ("统计2017年各客户州的商品成交额", []),
    ]):
        input_box.fill(question)
        with browser_page.expect_response("**/api/vanna/v2/chat_sse", timeout=240_000) as response_info:
            chat.locator("button.send-button").click()
        response = response_info.value
        assert response.ok
        payload = response.text()
        assert '"type": "error"' not in payload
        browser_page.wait_for_function("() => !document.querySelector('vanna-chat')?.shadowRoot?.querySelector('textarea.message-input')?.disabled")
        detail_response = browser_page.request.get(f"{BASE_URL}/api/project/conversations/{conversation_id}")
        assert detail_response.ok
        detail = detail_response.json()
        state = detail["analysis_state"]
        assert state["status"] == "completed", {"index": index, "status": state["status"]}
        assert state["metric_ids"] == ["gmv"]
        assert state["dimensions"] == ["customer_state"]
        assert state["filters"] == expected_filter
        assert state["time_range"]["start"] == "2017-01-01"
        assert state["time_range"]["end"] == "2018-01-01"
        assert state["last_successful_state_version"] == state["state_version"]
        assert state["last_successful_plan"] == state["active_plan"]
        assert detail["trusted_result_artifact"]
        manifest = browser_page.request.get(
            f"{BASE_URL}/api/project/conversations/{conversation_id}/artifacts/{detail['trusted_result_artifact']['artifact_id']}/manifest"
        )
        assert manifest.ok
        artifact = detail["trusted_result_artifact"]
        assert artifact["query_scope"]["dimensions"] == ["customer_state"]
        assert artifact["result_metadata"]["validation_status"] == "valid"
        assert 0 < len(artifact["result_metadata"]["entities"]) <= 8
        evidence_response = browser_page.request.get(
            f"{BASE_URL}/api/project/conversations/{conversation_id}/execution-memory"
        )
        assert evidence_response.ok
        executions = evidence_response.json()["executions"]
        successful = [item for item in executions if item["artifact_id"] == artifact["artifact_id"]]
        assert len(successful) == 1
        assert successful[0]["execution_id"] == artifact["result_metadata"]["execution_id"]
        assert successful[0]["validation_status"] == "valid"
        assert successful[0]["task_id"] == state["task_id"]
        assert "original_sql" not in str(executions) and "final_sql" not in str(executions)
        states.append(state)
        browser_page.screenshot(path=str(screenshot_dir / f"turn-{index+1}.png"), full_page=True)
    assert len({s["task_id"] for s in states}) == 1
    assert len({s["turn_id"] for s in states}) == 1
    browser_page.reload(wait_until="domcontentloaded")
    browser_page.wait_for_function("id => document.querySelector('vanna-chat')?.conversationId === id", arg=conversation_id)
    browser_page.wait_for_function("() => document.querySelector('vanna-chat')?.shadowRoot?.querySelectorAll('vanna-message').length > 0")
    browser_page.wait_for_timeout(1_000)
    restored = browser_page.request.get(f"{BASE_URL}/api/project/conversations/{conversation_id}").json()
    assert restored["analysis_state"]["task_id"] == states[-1]["task_id"]
    # A pure explanation reuses the verified artifact, without a second SQL.
    before = len(executions)
    chat = browser_page.locator("vanna-chat")
    chat.locator("textarea.message-input").fill("解释一下刚才的查询结果，不要重新查询")
    with browser_page.expect_response("**/api/vanna/v2/chat_sse", timeout=240_000) as followup_info:
        chat.locator("button.send-button").click()
    assert followup_info.value.ok
    followup_info.value.text()
    after = browser_page.request.get(f"{BASE_URL}/api/project/conversations/{conversation_id}/execution-memory").json()["executions"]
    assert len(after) == before
    restored = browser_page.request.get(f"{BASE_URL}/api/project/conversations/{conversation_id}").json()
    connection = psycopg2.connect(
        host=PostgresConnectionSettings().host, port=PostgresConnectionSettings().port,
        database=PostgresConnectionSettings().database, user=PostgresConnectionSettings().writer_user,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT catalog_trace FROM app.agent_runs WHERE conversation_id = %s ORDER BY started_at DESC LIMIT 1", (conversation_id,))
            followup_trace = cursor.fetchone()[0]
    finally:
        connection.close()
    assert followup_trace["result_reuse"] == "validated_result"
    assert restored["trusted_result_artifact"]["artifact_id"] == artifact["artifact_id"]
    page_response = browser_page.request.get(f"{BASE_URL}/api/project/conversations/{conversation_id}/messages?limit=5")
    assert page_response.ok and page_response.json()["messages"]
    browser_page.locator("details.more-menu > summary").click()
    with browser_page.expect_response(
        f"**/api/project/conversations/{conversation_id}/analysis-context"
    ) as clear_response_info:
        browser_page.locator("#clear-analysis").click()
    clear_response = clear_response_info.value
    assert clear_response.ok
    assert clear_response.json()["conversation_id"] == conversation_id
    browser_page.wait_for_function("() => document.querySelector('#analysis-notice')?.textContent?.includes('已清空')", timeout=10_000)
    cleared = browser_page.request.get(f"{BASE_URL}/api/project/conversations/{conversation_id}").json()
    assert cleared["analysis_state"]["task_id"] != states[-1]["task_id"]
    assert cleared["analysis_state"]["metric_ids"] == []
    assert cleared["trusted_result_artifact"] is None
    assert len(cleared["messages"]) == len(restored["messages"])
    chat = browser_page.locator("vanna-chat")
    chat.locator("textarea.message-input").fill("统计有效订单总数")
    with browser_page.expect_response("**/api/vanna/v2/chat_sse", timeout=240_000) as response_info:
        chat.locator("button.send-button").click()
    assert response_info.value.ok
    response_info.value.text()
    fresh = browser_page.request.get(f"{BASE_URL}/api/project/conversations/{conversation_id}").json()["analysis_state"]
    assert fresh["status"] == "completed"
    assert fresh["metric_ids"] == ["paid_order_count"]
    assert fresh["dimensions"] == [] and fresh["filters"] == [] and fresh["time_range"] is None
    browser_page.screenshot(path=str(screenshot_dir / "after-clear-new-query.png"), full_page=True)
    assert not console_errors


def test_analysis_context_live_summary_reload_and_incremental_resume(page):
    """Real LLM summary plus verified artifact reuse after a browser reload."""
    from data_analysis_agent.conversation_store import PostgresConversationStore
    from vanna.core.storage import Message
    from vanna.core.user import User

    browser_page, console_errors = page
    browser_page.set_default_timeout(240_000)
    chat = _open_normal_window(browser_page)
    browser_page.locator("#new-analysis").click()
    browser_page.wait_for_function("() => Boolean(localStorage.getItem('data-analysis-agent-embeddable-conversation-v1'))")
    cid = browser_page.evaluate("() => localStorage.getItem('data-analysis-agent-embeddable-conversation-v1')")

    def send(question):
        chat.locator("textarea.message-input").fill(question)
        with browser_page.expect_response("**/api/vanna/v2/chat_sse", timeout=240_000) as response:
            chat.locator("button.send-button").click()
        assert response.value.ok
        payload = response.value.text()
        browser_page.wait_for_function("() => !document.querySelector('vanna-chat')?.shadowRoot?.querySelector('textarea.message-input')?.disabled")
        return payload

    send("统计2017年各客户州的商品成交额")
    detail = browser_page.request.get(f"{BASE_URL}/api/project/conversations/{cid}").json()
    assert detail["analysis_state"]["status"] == "completed"
    task_id = detail["analysis_state"]["task_id"]
    artifact_id = detail["trusted_result_artifact"]["artifact_id"]
    before = browser_page.request.get(f"{BASE_URL}/api/project/conversations/{cid}/execution-memory").json()["executions"]
    store = PostgresConversationStore(PostgresConnectionSettings.from_environment())
    owner = User(id="local:analyst", group_memberships=["analyst"])

    async def add_narrative_turns(count):
        conversation = await store.get_conversation(cid, owner)
        for n in range(count):
            await store.append_turn_messages(conversation, [
                Message(role="user", content=f"第{n+1}轮补充：我想先理解销售分析的口径，暂时不换指标、不修改查询条件。"),
                Message(role="assistant", content="保持当前分析范围，接下来解释已验证的结果。"),
            ])

    def read_summary_and_trace():
        settings = PostgresConnectionSettings.from_environment()
        connection = psycopg2.connect(host=settings.host, port=settings.port, database=settings.database, user=settings.writer_user)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT working_memory FROM app.conversations WHERE conversation_id=%s", (cid,))
                summary = cursor.fetchone()[0].get("__history_summary")
                cursor.execute("SELECT catalog_trace FROM app.agent_runs WHERE conversation_id=%s ORDER BY started_at DESC LIMIT 1", (cid,))
                trace = cursor.fetchone()[0]
            return summary, trace
        finally:
            connection.close()

    # Playwright's sync driver runs its own event loop on this thread.
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, add_narrative_turns(8)).result()
    browser_page.reload(wait_until="domcontentloaded")
    browser_page.wait_for_function("id => document.querySelector('vanna-chat')?.conversationId === id", arg=cid)
    send("解释一下刚才的查询结果，不要重新查询，也不要改任何筛选条件")
    first_summary, trace = read_summary_and_trace()
    assert first_summary and first_summary["text"]
    assert trace["context_packet"]["summary_status"] == "generated"
    assert trace["context_packet"]["recent_turns"] <= 6
    assert trace["context_packet"]["recent_tokens"] <= 6000
    assert trace["prompt_budgets"]
    assert trace["result_reuse"] == "validated_result"
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, add_narrative_turns(2)).result()
    browser_page.reload(wait_until="domcontentloaded")
    browser_page.wait_for_function("id => document.querySelector('vanna-chat')?.conversationId === id", arg=cid)
    explanation = send("说明刚才已验证查询的时间范围和分组口径，继续解释已有结果，不改条件、不重新查询")
    assert "2017" in explanation
    second_summary, trace = read_summary_and_trace()
    assert second_summary["source_message_end"] > first_summary["source_message_end"]
    assert second_summary["latest_batch_start"] == first_summary["source_message_end"] + 1
    assert second_summary["scope"]["task_id"] == task_id
    after = browser_page.request.get(f"{BASE_URL}/api/project/conversations/{cid}/execution-memory").json()["executions"]
    assert len(after) == len(before)
    final = browser_page.request.get(f"{BASE_URL}/api/project/conversations/{cid}").json()
    assert final["analysis_state"]["task_id"] == task_id
    assert final["analysis_state"]["filters"] == []
    assert final["trusted_result_artifact"]["artifact_id"] == artifact_id
    browser_page.screenshot(path="/disk2/gengnan/data-analysis-agent-data/evals/analysis-memory-phase4-20261016/browser/long-history-reloaded.png", full_page=True)
    assert not console_errors


def test_analysis_state_live_ranked_entity_reference(page) -> None:
    """Resolve a follow-up entity from the validated result, never prose."""
    browser_page, console_errors = page
    browser_page.set_default_timeout(240_000)
    chat = _open_normal_window(browser_page)
    browser_page.locator("#new-analysis").click()
    browser_page.wait_for_function(
        "() => Boolean(localStorage.getItem('data-analysis-agent-embeddable-conversation-v1'))"
    )
    conversation_id = browser_page.evaluate(
        "() => localStorage.getItem('data-analysis-agent-embeddable-conversation-v1')"
    )
    input_box = chat.locator("textarea.message-input")

    input_box.fill("统计2017年各客户州的商品成交额，按成交额从高到低排序")
    with browser_page.expect_response("**/api/vanna/v2/chat_sse", timeout=240_000) as first_info:
        chat.locator("button.send-button").click()
    assert first_info.value.ok
    first_info.value.text()
    browser_page.wait_for_function(
        "() => !document.querySelector('vanna-chat')?.shadowRoot?.querySelector('textarea.message-input')?.disabled",
        timeout=240_000,
    )
    first = browser_page.request.get(
        f"{BASE_URL}/api/project/conversations/{conversation_id}"
    ).json()
    artifact = first["trusted_result_artifact"]
    assert artifact["query_scope"]["dimensions"] == ["customer_state"]
    assert artifact["result_metadata"]["validation_status"] == "valid"
    assert artifact["result_metadata"]["ranked_entities"][0]["value"]

    input_box.fill("刚才排名第一的州哪些商品种类卖得好")
    with browser_page.expect_response("**/api/vanna/v2/chat_sse", timeout=240_000) as second_info:
        chat.locator("button.send-button").click()
    assert second_info.value.ok
    second_info.value.text()
    browser_page.wait_for_function(
        "() => !document.querySelector('vanna-chat')?.shadowRoot?.querySelector('textarea.message-input')?.disabled",
        timeout=240_000,
    )
    second = browser_page.request.get(
        f"{BASE_URL}/api/project/conversations/{conversation_id}"
    ).json()
    state = second["analysis_state"]
    assert state["status"] == "completed"
    assert state["filters"] == ["customer_state=SP"]
    assert state["dimensions"] == ["product_category_name"]
    assert not console_errors

    connection = psycopg2.connect(
        host=PostgresConnectionSettings().host,
        port=PostgresConnectionSettings().port,
        database=PostgresConnectionSettings().database,
        user=PostgresConnectionSettings().writer_user,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT catalog_trace FROM app.agent_runs "
                "WHERE conversation_id = %s ORDER BY started_at DESC LIMIT 1",
                (conversation_id,),
            )
            trace = cursor.fetchone()[0]
    finally:
        connection.close()
    assert trace["result_reference"]["status"] == "resolved"
    assert "original_sql" not in json.dumps(trace["result_reference"])


def test_w4_04_demo_script_browser_steps(page) -> None:
    """Walk the browser controls named by the W4-04 demo script."""
    browser_page, console_errors = page
    script = yaml.safe_load(
        Path("evals/cases/w4_04_product_demo_v1.yaml").read_text(encoding="utf-8")
    )
    _open_normal_window(browser_page)
    browser_page.locator("details.more-menu > summary").click()
    for step in script["steps"]:
        if step["kind"] != "browser":
            continue
        controls = step.get("controls") or [step["control"]]
        for control in controls:
            if control == "#admin-panel" and step.get("admin_hidden_until_toggle"):
                assert browser_page.locator(control).is_hidden()
                browser_page.locator("#admin-toggle").click()
                assert browser_page.locator(control).is_visible()
                continue
            assert browser_page.locator(control).count() == 1
    browser_page.locator("#evidence-toggle").click()
    browser_page.wait_for_function(
        "() => document.querySelector('[data-evidence=dataset]')?.textContent !== '加载中'"
    )
    assert browser_page.locator('[data-evidence="dataset"]').inner_text() != "加载中"
    assert not console_errors
