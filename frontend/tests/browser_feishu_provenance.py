"""Check source badges in the built SPA using only mocked API responses.

Usage: browser_feishu_provenance.py <owned-loopback-url> <new-output-dir> <existing-browser>
No Hub backend, provider, or real Feishu application is contacted.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright, expect


def origin(url):
    parsed = urlparse(url)
    return parsed.scheme, parsed.hostname, parsed.port


def history():
    messages = [
        ("web-turn", "Direct browser message", {"origin": "web"}),
        (
            "feishu-turn",
            "Remote message",
            {
                "origin": "feishu",
                "feishu": {
                    "app_id": "cli-bot",
                    "chat_id": "oc-chat",
                    "message_id": "om-source-message",
                    "sender_open_id": "ou-feishu-sender",
                },
            },
        ),
        ("feishu-legacy-looking-id", "Older message", None),
    ]
    events = []
    for index, (turn_id, text, metadata) in enumerate(messages):
        payload = {"summary": text, "attachments": [], "mode": "default"}
        if metadata is not None:
            payload["metadata"] = metadata
        for event_type, body in (
            ("turn_started", payload),
            ("text_delta", {"text": "Fixture reply"}),
            ("turn_completed", {"status": "completed", "assistant_text": "Fixture reply"}),
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
                    "message_id": f"{turn_id}:user" if event_type == "turn_started" else None,
                    "payload": body,
                    "created_at": "2026-10-06T08:00:00Z",
                    "redacted": False,
                }
            )
    return events


async def main():
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
    executable = Path(sys.argv[3]).resolve(strict=True)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise SystemExit("An existing executable browser is required; this script never downloads")
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
    initial_binding = dict(
        owner_open_id="ou-owner",
        sender_open_id="ou-feishu-sender",
        app_id="cli-bot",
        chat_id="oc-chat",
        tab_id=tab["id"],
        workspace_id=None,
        created_at=tab["created_at"],
    )
    binding = initial_binding
    events = history()
    report = dict(
        result="FAIL",
        api_requests=[],
        fallback_api_paths=[],
        blocked_external_requests=[],
        blocked_websockets=[],
        page_errors=[],
        screenshots=[],
        boundary="All APIs mocked; no Hub backend, provider, or real Feishu traffic.",
    )
    browser = context = page = None
    failure = None
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                headless=True,
                executable_path=str(executable),
                args=["--disable-background-networking", "--disable-component-update"],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                service_workers="block",
            )

            async def block_websocket(socket):
                report["blocked_websockets"].append(socket.url)
                await socket.close(code=1000, reason="mocked source badge smoke")

            async def route_all(route):
                request = route.request
                url = urlparse(request.url)
                if origin(request.url) != origin(base):
                    report["blocked_external_requests"].append(request.url)
                    await route.abort("blockedbyclient")
                    return
                path = url.path
                if not path.startswith("/api/"):
                    await route.continue_()
                    return
                report["api_requests"].append([request.method, path])
                if path == "/api/auth/check":
                    body = {"auth_required": False, "user": None}
                elif path == "/api/tabs":
                    body = [tab]
                elif path == "/api/feishu/bot/binding":
                    body = {"binding": binding}
                elif path.endswith("/goal/current"):
                    body = None
                elif path.endswith("/capabilities"):
                    body = dict(
                        structured=True,
                        send_message=True,
                        supports_goals=True,
                        supports_images=False,
                        agent_type="codex",
                        supports_mode_switch=False,
                    )
                elif path.endswith(("/events", "/wait")):
                    if path.endswith("/wait"):
                        await asyncio.sleep(0.25)
                    since = int(parse_qs(url.query).get("since_sequence", ["-1"])[0])
                    rows = [event for event in events if event["stream_sequence"] > since]
                    body = dict(
                        events=rows,
                        next_sequence=rows[-1]["stream_sequence"] if rows else since,
                        has_more=False,
                    )
                elif path.endswith("/live"):
                    await route.fulfill(
                        status=200, content_type="text/event-stream", body=": mock\n\n"
                    )
                    return
                elif path.endswith("/view"):
                    body = {"ok": True}
                elif "network" in path:
                    body = {"hostname": "fixture", "addresses": []}
                else:
                    report["fallback_api_paths"].append(path)
                    body = []
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(body)
                )

            await context.route_web_socket("**/*", block_websocket)
            await context.route("**/*", route_all)
            page = await context.new_page()
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))

            async def check_sources():
                await expect(page.get_by_role("log", name="Chat conversation")).to_be_visible()
                for kind, text in (
                    ("web", "Direct browser message"),
                    ("feishu", "Remote message"),
                    ("unknown", "Older message"),
                ):
                    bubble = page.locator(
                        f'.conversation-bubble--user[data-message-origin="{kind}"]'
                    )
                    await expect(bubble).to_have_count(1)
                    await expect(bubble).to_contain_text(text)
                labels = page.get_by_test_id("conversation-message-source")
                await expect(labels).to_have_count(1)
                await expect(labels.locator(".conversation-message-source__label")).to_have_text(
                    "Feishu · ou-feishu-sender"
                )
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

            for state, current_binding in (
                ("bound-current", initial_binding),
                ("bound-other", {**initial_binding, "tab_id": "ui-tab-other"}),
                ("unbound", None),
            ):
                binding = current_binding
                if state == "bound-current":
                    await page.goto(base + "/?tab=ui-tab-1", wait_until="domcontentloaded")
                else:
                    await page.reload(wait_until="domcontentloaded")
                await check_sources()
                await page.get_by_test_id("feishu-binding-trigger").click()
                await expect(page.get_by_test_id("feishu-binding-status")).to_have_attribute(
                    "data-state", state
                )
                await page.keyboard.press("Escape")
                await expect(page.get_by_test_id("feishu-binding-panel")).to_be_hidden()
                filename = f"desktop-{state}.png"
                await page.screenshot(path=str(output / filename), full_page=True)
                report["screenshots"].append(filename)
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.wait_for_function(
                "() => { const d = document.querySelector('.chat-sidebar.mobile-drawer'); "
                "return !d || d.getBoundingClientRect().right <= 0 }"
            )
            await check_sources()
            await page.screenshot(path=str(output / "mobile-unbound.png"), full_page=True)
            report["screenshots"].append("mobile-unbound.png")
            assert not report["page_errors"], report["page_errors"]
            report["result"] = "PASS"
        except BaseException as exc:
            failure = exc
            report["error_type"] = type(exc).__name__
            if page is not None:
                try:
                    await page.screenshot(path=str(output / "failure.png"), full_page=True)
                except Exception:
                    pass
        finally:
            try:
                if context is not None:
                    await context.close()
            finally:
                if browser is not None:
                    await browser.close()
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    if failure is not None:
        raise failure


if __name__ == "__main__":
    asyncio.run(main())
