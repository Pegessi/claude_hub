"""Check source badges in the built SPA using only mocked API responses.

Usage: browser_feishu_provenance.py <owned-loopback-url> <new-output-dir> <existing-browser>
No Hub backend, provider, or real Feishu application is contacted.
"""

import asyncio
import base64
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from browser_lifecycle import BrowserRun, run_browser_main
from playwright.async_api import expect


def origin(url):
    parsed = urlparse(url)
    return parsed.scheme, parsed.hostname, parsed.port


PREVIEW_ID = "11111111111111111111111111111111"
PREVIEW_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
    "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
BOT_AVATAR_PNG = PREVIEW_PNG


def active_binding(tab_id: str, created_at: str) -> dict:
    return {
        "pairing_id": "pair-provenance",
        "state": "active",
        "tab_id": tab_id,
        "workspace_id": None,
        "created_at": created_at,
        "is_mine": True,
        "owner_kind": "oauth",
        "chat_id": "oc-chat",
    }


def bot_pool(binding: dict | None, pool_revision: int) -> dict:
    return {
        "pool_revision": pool_revision,
        "bots": [
            {
                "bot_id": "bot-provenance",
                "name": "Provenance Bot",
                "feishu_name": "Hub 助手",
                "feishu_avatar_url": "https://avatar.example.test/hub-bot.png",
                "app_id": "cli-bot",
                "source": "stored",
                "enabled": True,
                "credentials_editable": True,
                "deletable": True,
                "revision": 1,
                "generation": 1,
                "configured": True,
                "app_secret_configured": True,
                "connection_status": "connected",
                "updated_at": "2026-10-06T08:00:00Z",
                "binding": binding,
                "my_claims": [],
            }
        ],
        "pool_editable": True,
        "deprecated_env": [],
        "focus_bot_id": "bot-provenance",
    }


LONG_FIRST_MESSAGE = (
    "This long first message checks that readable Chat text stays clear of "
    "the floating pane name and Feishu connection control on a narrow screen. "
    "Short fixture text cannot prove that boundary."
)


def history():
    messages = [
        ("web-turn", LONG_FIRST_MESSAGE, {"origin": "web"}),
        (
            "feishu-turn",
            "Remote message",
            {
                "origin": "feishu",
                "feishu": {
                    "app_id": "cli-bot",
                    "bot_name": "Hub 助手",
                    "chat_id": "oc-chat",
                    "message_id": "om-source-message",
                    "sender_open_id": "ou-feishu-sender",
                },
            },
        ),
        ("feishu-legacy-looking-id", "Older message", None),
        (
            "hub-turn",
            "Injected message",
            {
                "origin": "hub_message",
                "hub_message": {
                    "message_id": "hub-1",
                    "actor": "local CLI",
                    "source_tab_id": "unverified-tab",
                },
            },
        ),
    ]
    events = []
    for index, (turn_id, text, metadata) in enumerate(messages):
        payload = {
            "summary": text,
            "attachments": (
                [
                    {
                        "id": PREVIEW_ID,
                        "mime_type": "image/png",
                        "bytes": len(PREVIEW_PNG),
                        "width": 1,
                        "height": 1,
                    }
                ]
                if turn_id == "feishu-turn"
                else []
            ),
            "mode": "default",
        }
        if metadata is not None:
            payload["metadata"] = metadata
        for event_type, body in (
            ("turn_started", payload),
            ("text_delta", {"text": "Fixture reply"}),
            (
                "turn_completed",
                {"status": "completed", "assistant_text": "Fixture reply"},
            ),
        ):
            events.append(
                {
                    "stream_sequence": len(events),
                    "session_id": "terminal-tab-ui-tab-1",
                    "tab_id": "ui-tab-1",
                    "agent_type": "codex",
                    "type": event_type,
                    "run_epoch": index + 1,
                    "turn_id": turn_id,
                    "message_id": (f"{turn_id}:user" if event_type == "turn_started" else None),
                    "payload": body,
                    "created_at": "2026-10-06T08:00:00Z",
                    "redacted": False,
                }
            )
    return events


async def main(browser_run: BrowserRun) -> None:
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    base = sys.argv[1].rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.port in {None, 5173, 8173, 10025, 18183}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit("Use an owned numeric-loopback static server, not a shared Hub")
    output = Path(sys.argv[2]).resolve()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    tab = dict(
        id="ui-tab-1",
        name="Mixed entry Chat",
        cwd="/fixture/project",
        agent_type="codex",
        session_kind="chat",
        port=0,
        is_active=True,
        created_at="2026-10-06T08:00:00Z",
    )
    binding = active_binding(tab["id"], tab["created_at"])
    pool_revision = 0
    events = history()
    pool_response_gate = asyncio.Event()
    stream_base = f"/api/workspaces/tabs/{tab['id']}/stream"
    goal_current_path = f"/api/tabs/{tab['id']}/goal/current"
    viewed_path = f"/api/tabs/{tab['id']}/view"
    final_sequence = events[-1]["stream_sequence"]
    report = dict(
        result="FAIL",
        api_requests=[],
        unknown_api_requests=[],
        unexpected_external_requests=[],
        blocked_external_requests=[],
        bot_avatar_requests=[],
        blocked_websockets=[],
        page_errors=[],
        screenshots=[],
        boundary="All APIs mocked; no Hub backend, provider, or real Feishu traffic.",
    )
    page = None
    try:
        async with browser_run.context(
            viewport={"width": 1280, "height": 900},
            launch_args=[
                "--disable-background-networking",
                "--disable-component-update",
            ],
        ) as context:
            try:

                async def block_websocket(socket):
                    report["blocked_websockets"].append(socket.url)
                    await socket.close(code=1000, reason="mocked source badge smoke")

                async def route_all(route):
                    request = route.request
                    url = urlparse(request.url)
                    method = request.method
                    path = url.path
                    query = parse_qs(url.query, keep_blank_values=True)
                    if request.url == "https://avatar.example.test/hub-bot.png":
                        report["bot_avatar_requests"].append(request.url)
                        await route.fulfill(
                            status=200, content_type="image/png", body=BOT_AVATAR_PNG
                        )
                        return
                    if origin(request.url) != origin(base):
                        report["blocked_external_requests"].append(request.url)
                        if url.scheme != "https" or url.hostname not in {
                            "fonts.googleapis.com",
                            "fonts.gstatic.com",
                        }:
                            report["unexpected_external_requests"].append(request.url)
                        await route.abort("blockedbyclient")
                        return
                    if path.endswith(f"/attachments/{PREVIEW_ID}"):
                        await route.fulfill(status=200, content_type="image/png", body=PREVIEW_PNG)
                        return
                    if not path.startswith("/api/"):
                        if method in {"GET", "HEAD"}:
                            await route.continue_()
                        else:
                            report["unknown_api_requests"].append([method, request.url])
                            await route.fulfill(status=405, body="blocked static method")
                        return
                    report["api_requests"].append([method, request.url])
                    if method == "GET" and path == "/api/auth/check" and not query:
                        body = {"auth_required": False, "user": None}
                    elif method == "GET" and path.endswith("/messages"):
                        body = {
                            "messages": [
                                {
                                    "message_id": "pending-1",
                                    "state": "waiting",
                                    "waiting_reason": "busy",
                                },
                                {"message_id": "uncertain-1", "state": "uncertain"},
                            ]
                        }
                    elif method == "GET" and path == "/api/tabs" and not query:
                        body = [tab]
                    elif (
                        method == "GET"
                        and path in {"/api/tabs/status", "/api/tabs/archived"}
                        and not query
                    ):
                        body = []
                    elif path.startswith("/api/tabs/sidebar-preferences"):
                        # Read, one-shot pin migration and per-tab pin updates
                        # share one shape; provenance never depends on pins.
                        body = {"pinned_tab_ids": []}
                    elif method == "GET" and path == "/api/env-presets" and not query:
                        body = {"custom_presets": [], "hidden_builtin_ids": []}
                    elif method == "GET" and path == "/api/feishu/bot/bots" and not query:
                        await pool_response_gate.wait()
                        body = bot_pool(binding, pool_revision)
                    elif method == "GET" and path == goal_current_path and not query:
                        body = None
                    elif method == "GET" and path == "/api/dot/identity" and not query:
                        # The Dot launcher in the tab bar reads Dot's name and avatar.
                        body = {"name": "Dot", "avatar": "", "avatar_image": None}
                    elif method == "POST" and path == viewed_path and not query:
                        if request.post_data not in {None, ""}:
                            report["unknown_api_requests"].append([method, request.url])
                            await route.fulfill(status=422, body="unexpected view body")
                            return
                        body = {"ok": True}
                    elif method == "GET" and path == f"{stream_base}/capabilities" and not query:
                        body = {
                            "structured": True,
                            "send_message": True,
                            "supports_goals": True,
                            "supports_images": False,
                            "agent_type": "codex",
                            "supports_mode_switch": False,
                        }
                    elif (
                        method == "GET"
                        and path == f"{stream_base}/events"
                        and query
                        == {
                            "since_sequence": ["-1"],
                            "limit": ["5000"],
                            "compact": ["true"],
                            "view": ["conversation"],
                        }
                    ):
                        body = {
                            "events": events,
                            "next_sequence": final_sequence,
                            "has_more": False,
                        }
                    elif method == "POST" and path == f"{stream_base}/wait" and not query:
                        try:
                            submitted = json.loads(request.post_data or "")
                        except json.JSONDecodeError:
                            submitted = None
                        if submitted != {
                            "since_sequence": final_sequence,
                            "timeout_seconds": 30,
                        }:
                            report["unknown_api_requests"].append([method, request.url])
                            await route.fulfill(status=422, body="unexpected wait cursor")
                            return
                        await asyncio.sleep(0.25)
                        body = {
                            "events": [],
                            "next_sequence": final_sequence,
                            "has_more": False,
                        }
                    elif (
                        method == "GET"
                        and path == f"{stream_base}/live"
                        and query == {"since_sequence": [str(final_sequence)]}
                    ):
                        await route.fulfill(
                            status=200,
                            content_type="text/event-stream",
                            body=": mock\n\n",
                        )
                        return
                    else:
                        report["unknown_api_requests"].append([method, request.url])
                        await route.fulfill(
                            status=501,
                            content_type="application/json",
                            body=json.dumps({"detail": f"unmocked API: {method} {request.url}"}),
                        )
                        return
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(body),
                    )

                await context.route_web_socket("**/*", block_websocket)
                await context.route("**/*", route_all)
                page = await context.new_page()
                page.on("pageerror", lambda error: report["page_errors"].append(str(error)))

                async def check_sources():
                    await expect(page.get_by_role("log", name="Chat conversation")).to_be_visible()
                    for kind, text in (
                        ("web", LONG_FIRST_MESSAGE),
                        ("feishu", "Remote message"),
                        ("unknown", "Older message"),
                        ("hub_message", "Injected message"),
                    ):
                        bubble = page.locator(
                            f'.conversation-bubble--user[data-message-origin="{kind}"]'
                        )
                        await expect(bubble).to_have_count(1)
                        await expect(bubble).to_contain_text(text)
                    hub = page.locator(
                        '.conversation-bubble--user[data-message-origin="hub_message"]'
                    )
                    await expect(hub).to_contain_text("Hub message · local CLI")
                    await expect(
                        hub.get_by_test_id("conversation-message-source-avatar")
                    ).to_have_count(0)
                    await expect(
                        page.get_by_role("complementary", name="Hub message inbox")
                    ).to_contain_text("Delivery uncertain")
                    labels = page.locator(
                        '.conversation-bubble--user[data-message-origin="feishu"]'
                    ).get_by_test_id("conversation-message-source")
                    await expect(labels).to_have_count(1)
                    await expect(
                        labels.locator(".conversation-message-source__label")
                    ).to_have_text("Hub 助手")
                    source_mark = labels.get_by_test_id("conversation-message-source-avatar")
                    avatar = source_mark.locator("img")
                    await expect(source_mark).to_have_count(1)
                    await expect(avatar).to_have_attribute(
                        "src", "https://avatar.example.test/hub-bot.png"
                    )
                    await expect(avatar).to_have_attribute("crossorigin", "anonymous")
                    await expect(avatar).to_be_visible()
                    feishu_bubble = page.locator(
                        '.conversation-bubble--user[data-message-origin="feishu"]'
                    )
                    preview = feishu_bubble.locator(".turn-attachment-img")
                    await expect(preview).to_have_count(1)
                    await expect(preview).to_be_visible()
                    await expect(
                        feishu_bubble.get_by_text("Preview unavailable", exact=True)
                    ).to_have_count(0)
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth + 1"
                    )

                async def assert_chat_chrome_clearance():
                    trigger = page.get_by_test_id("feishu-binding-trigger")
                    pane = page.locator(".terminal-pane").filter(has=trigger)
                    name = pane.locator(".pane-session-name")
                    await expect(trigger).to_be_visible()
                    await expect(name).to_be_visible()
                    pane_box = await pane.bounding_box()
                    name_box = await name.bounding_box()
                    trigger_box = await trigger.bounding_box()
                    assert pane_box is not None and name_box is not None and trigger_box is not None
                    assert name_box["x"] >= pane_box["x"] + 7
                    # The name pill and the Feishu control share one flex
                    # row, so the only requirement is that they do not
                    # overlap; the row's own gap sets the exact spacing.
                    assert name_box["x"] + name_box["width"] <= trigger_box["x"]
                    assert (
                        trigger_box["x"] + trigger_box["width"]
                        <= pane_box["x"] + pane_box["width"] + 1
                    )

                for state, current_binding, expected_view in (
                    (
                        "bound-current",
                        active_binding(tab["id"], tab["created_at"]),
                        "bound-current",
                    ),
                    (
                        "unbound-other",
                        active_binding("ui-tab-other", tab["created_at"]),
                        "unbound",
                    ),
                    ("unbound", None, "unbound"),
                ):
                    binding = current_binding
                    pool_revision += 1
                    if state == "bound-current":
                        await page.goto(base + "/?tab=ui-tab-1", wait_until="domcontentloaded")
                        # History can paint before the Bot pool resolves. With no
                        # persisted event avatar, the source starts on the local
                        # brand SVG and updates in place once this delayed pool
                        # response supplies the exact App ID's current avatar.
                        initial_source_mark = page.locator(
                            '.conversation-bubble--user[data-message-origin="feishu"]'
                        ).get_by_test_id("conversation-message-source-avatar")
                        await expect(
                            initial_source_mark.locator(".feishu-brand-mark")
                        ).to_be_visible()
                        await expect(initial_source_mark.locator("img")).to_have_count(0)
                        pool_response_gate.set()
                    else:
                        await page.reload(wait_until="domcontentloaded")
                    await check_sources()
                    await assert_chat_chrome_clearance()
                    trigger = page.get_by_test_id("feishu-binding-trigger")
                    await trigger.click()
                    panel = page.get_by_test_id("feishu-binding-panel")
                    status = page.get_by_test_id("feishu-binding-status")
                    await expect(status).to_have_attribute("data-state", expected_view)
                    await expect(status).to_have_text(
                        "Connected with Hub 助手"
                        if expected_view == "bound-current"
                        else "Not connected"
                    )
                    if state == "unbound-other":
                        option = panel.get_by_label("Bot", exact=True).locator(
                            'option[value="bot-provenance"]'
                        )
                        await expect(option).to_have_js_property("disabled", True)
                        await expect(option).to_contain_text("In use by Chat ui-tab-other")
                        await expect(
                            panel.get_by_role("button", name="Generate pairing code", exact=True)
                        ).to_be_disabled()
                    elif state == "unbound":
                        await expect(panel.get_by_label("Bot", exact=True)).to_have_value(
                            "bot-provenance"
                        )
                        await expect(
                            panel.get_by_role("button", name="Generate pairing code", exact=True)
                        ).to_be_enabled()
                    await panel.get_by_role("button", name="Close", exact=True).click()
                    await expect(panel).to_be_hidden()
                    filename = f"desktop-{state}.png"
                    await page.screenshot(path=str(output / filename), full_page=True)
                    report["screenshots"].append(filename)
                feishu_metadata = next(
                    event["payload"]["metadata"]["feishu"]
                    for event in events
                    if event["type"] == "turn_started" and event["turn_id"] == "feishu-turn"
                )
                # Keep this Chat bound to cli-bot while the message identifies
                # cli-other-bot. The brand fallback must not borrow the active
                # Chat Bot merely because exact App ID matching failed.
                binding = active_binding(tab["id"], tab["created_at"])
                pool_revision += 1
                feishu_metadata["app_id"] = "cli-other-bot"
                await page.reload(wait_until="domcontentloaded")
                unmatched_source_mark = page.locator(
                    '.conversation-bubble--user[data-message-origin="feishu"]'
                ).get_by_test_id("conversation-message-source-avatar")
                await expect(unmatched_source_mark.locator("img")).to_have_count(0)
                await expect(unmatched_source_mark.locator(".feishu-brand-mark")).to_be_visible()
                feishu_metadata["app_id"] = "cli-bot"
                await page.evaluate("localStorage.setItem('claude_hub_layout_type', '3x1')")
                await page.reload(wait_until="domcontentloaded")
                await check_sources()
                await assert_chat_chrome_clearance()
                await page.get_by_test_id("feishu-binding-trigger").click()
                panel = page.get_by_test_id("feishu-binding-panel")
                await expect(panel).to_be_visible()
                await panel.get_by_role("button", name="Close", exact=True).click()
                await expect(panel).to_be_hidden()
                await page.screenshot(path=str(output / "desktop-grid.png"), full_page=True)
                report["screenshots"].append("desktop-grid.png")
                await page.evaluate("localStorage.setItem('claude_hub_layout_type', '1x1')")
                await page.reload(wait_until="domcontentloaded")
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.wait_for_function(
                    "() => { const d = document.querySelector('.chat-sidebar.mobile-drawer'); "
                    "return !d || d.getBoundingClientRect().right <= 0 }"
                )
                await check_sources()
                await assert_chat_chrome_clearance()
                await page.get_by_test_id("feishu-binding-trigger").click()
                panel = page.get_by_test_id("feishu-binding-panel")
                await expect(panel).to_be_visible()
                await panel.get_by_role("button", name="Close", exact=True).click()
                await expect(panel).to_be_hidden()
                await page.get_by_role("log", name="Chat conversation").evaluate(
                    "element => { element.scrollTop = 0 }"
                )
                await page.evaluate("() => new Promise(requestAnimationFrame)")
                overlap = await page.evaluate("""() => {
                  const bubble = document.querySelector(
                    '.structured-turn .conversation-bubble--user .markdown-content');
                  const name = document.querySelector('.pane-session-name');
                  const trigger = document.querySelector('[data-testid="feishu-binding-trigger"]');
                  if (!bubble || !name || !trigger) return {missing: true};
                  const walker = document.createTreeWalker(bubble, NodeFilter.SHOW_TEXT);
                  const textRects = [];
                  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
                    if (!node.textContent.trim()) continue;
                    const range = document.createRange();
                    range.selectNodeContents(node);
                    for (const rect of range.getClientRects()) {
                      if (rect.width > 0 && rect.height > 0) textRects.push(rect.toJSON());
                    }
                  }
                  const controls = [name, trigger].map(element => element.getBoundingClientRect().toJSON());
                  const intersections = [];
                  for (const text of textRects) for (const control of controls) {
                    if (text.right > control.left && text.left < control.right &&
                        text.bottom > control.top && text.top < control.bottom) {
                      intersections.push({text, control});
                    }
                  }
                  return {missing: false, textRects, controls, intersections};
                }""")
                report["first_message_clearance"] = overlap
                assert not overlap["missing"] and overlap["textRects"], overlap
                assert not overlap["intersections"], overlap
                await page.screenshot(path=str(output / "mobile-unbound.png"), full_page=True)
                report["screenshots"].append("mobile-unbound.png")

                hub_bubble = page.locator(
                    '.conversation-bubble--user[data-message-origin="hub_message"]'
                )
                await hub_bubble.scroll_into_view_if_needed()
                await expect(hub_bubble).to_be_in_viewport()
                hub_badge = hub_bubble.get_by_test_id("conversation-message-source")
                await expect(hub_badge).to_be_visible()
                await expect(hub_badge).to_have_text("Hub message · local CLI")
                # source_tab_id is an unverified caller hint, never identity.
                await expect(hub_bubble).not_to_contain_text("unverified-tab")
                await expect(
                    hub_bubble.get_by_test_id("conversation-message-source-avatar")
                ).to_have_count(0)
                hub_geometry = await hub_badge.evaluate("""element => {
                  const badge = element.getBoundingClientRect();
                  const bubble = element.closest('.conversation-bubble').getBoundingClientRect();
                  return {badge: badge.toJSON(), bubble: bubble.toJSON(), width: innerWidth};
                }""")
                report["mobile_hub_badge_geometry"] = hub_geometry
                assert hub_geometry["badge"]["left"] >= hub_geometry["bubble"]["left"]
                assert hub_geometry["badge"]["right"] <= min(
                    hub_geometry["bubble"]["right"], hub_geometry["width"]
                )
                await page.screenshot(path=str(output / "mobile-hub-message.png"))
                report["screenshots"].append("mobile-hub-message.png")

                assert not report["unknown_api_requests"], report["unknown_api_requests"]
                assert not report["unexpected_external_requests"], report[
                    "unexpected_external_requests"
                ]
                assert not report["page_errors"], report["page_errors"]
                assert report["bot_avatar_requests"]
                report["result"] = "PASS"
            except BaseException as exc:
                report["result"] = "FAIL"
                report["error_type"] = type(exc).__name__
                if page is not None and not isinstance(exc, asyncio.CancelledError):
                    try:
                        await page.screenshot(
                            path=str(output / "failure.png"),
                            full_page=True,
                            timeout=2000,
                        )
                    except Exception:
                        pass
                raise
    finally:
        pool_response_gate.set()
        if (
            not browser_run.cleanup_complete
            or browser_run.cancelled
            or browser_run.signal_number is not None
        ):
            report["result"] = "FAIL"
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    run_browser_main(main)
