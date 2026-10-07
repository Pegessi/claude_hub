"""Full-app native sub-agent region smoke with every API request mocked.

Run only with a dedicated Vite server, never 5173/8173:
  python tests/browser_chat_subagent_region.py http://127.0.0.1:5287 /tmp/subagent-region-ui [browser]
"""

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, expect

F6 = "01a0e38b-0000-7031-b97e-aaaaaaaaaaaa"
FEC = "01a0e38b-0000-7031-b97e-bbbbbbbbbbbb"
TURN = "fixture-parent-turn"
TAB = "fixture-subagent-tab"
NOW = "2026-10-06T10:00:00Z"


def event(sequence, event_type, payload, *, message_id=None, call_id=None):
    return {
        "stream_sequence": sequence,
        "session_id": "fixture-session",
        "tab_id": TAB,
        "agent_type": "codex",
        "type": event_type,
        "turn_id": TURN,
        "message_id": message_id,
        "call_id": call_id,
        "payload": payload,
        "created_at": NOW,
        "redacted": False,
    }


def child_event(sequence, event_type, thread_id, payload, *, message_id=None, call_id=None):
    return event(
        sequence,
        event_type,
        {**payload, "subagent_thread": thread_id},
        message_id=message_id,
        call_id=call_id,
    )


EVENTS = [
    event(
        0,
        "turn_started",
        {"summary": "Run two checks", "mode": "default"},
        message_id=f"{TURN}:user",
    ),
    event(
        1,
        "tool_call_started",
        {
            "tool_call_id": "spawn-left",
            "name": "spawnAgent",
            "args": {"prompt": "inspect parser", "receiverThreadIds": [F6]},
        },
        call_id="spawn-left",
    ),
    event(
        2,
        "tool_call_completed",
        {"tool_call_id": "spawn-left", "status": "completed", "result": "{}"},
        call_id="spawn-left",
    ),
    child_event(
        3,
        "text_delta",
        F6,
        {"text": "first child report"},
        message_id=f"{TURN}:sub:{F6}:assistant",
    ),
    child_event(
        4,
        "status",
        F6,
        {
            "text": "Sub-agent turn failed",
            "provider_status": "subagent_lifecycle",
            "subagent_status": "failed",
            "snapshot": True,
        },
        message_id=f"subagent-lifecycle:{F6}",
    ),
    event(
        5,
        "tool_call_started",
        {
            "tool_call_id": "spawn-right",
            "name": "spawnAgent",
            "args": {"prompt": "inspect UI", "receiverThreadIds": [FEC]},
        },
        call_id="spawn-right",
    ),
    event(
        6,
        "tool_call_completed",
        {"tool_call_id": "spawn-right", "status": "completed", "result": "{}"},
        call_id="spawn-right",
    ),
    child_event(
        7,
        "text_delta",
        FEC,
        {"text": "second child still working"},
        message_id=f"{TURN}:sub:{FEC}:assistant",
    ),
    event(8, "text_delta", {"text": "Parent answer."}, message_id=f"{TURN}:assistant"),
    event(9, "turn_completed", {"status": "completed"}, message_id=TURN),
    {
        **event(10, "turn_started", {"summary": "Next question"}),
        "turn_id": "fixture-next-turn",
    },
    {
        **event(11, "text_delta", {"text": "Next answer."}),
        "turn_id": "fixture-next-turn",
    },
    {
        **event(12, "turn_completed", {"status": "completed"}),
        "turn_id": "fixture-next-turn",
    },
]


async def main():
    if len(sys.argv) not in (3, 4):
        raise SystemExit(__doc__)
    base, output_arg = sys.argv[1:3]
    executable = sys.argv[3] if len(sys.argv) == 4 else None
    parsed_base = urlparse(base)
    if (
        parsed_base.scheme != "http"
        or parsed_base.hostname not in ("127.0.0.1", "::1")
        or parsed_base.port in (None, 5173, 8173, 10025, 18183)
        or parsed_base.username is not None
        or parsed_base.password is not None
    ):
        raise SystemExit("Use an owned numeric-loopback review server")
    output = Path(output_arg)
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    requests = []
    unknown_requests = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, executable_path=executable)
        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900}, service_workers="block"
            )

            async def block_websocket(socket):
                await socket.close(code=1000, reason="mocked UI smoke")

            async def api(route):
                request = route.request
                parsed = urlparse(request.url)
                if (parsed.scheme, parsed.hostname, parsed.port) != (
                    parsed_base.scheme,
                    parsed_base.hostname,
                    parsed_base.port,
                ):
                    await route.abort("blockedbyclient")
                    return
                path = parsed.path
                if not path.startswith("/api/"):
                    await route.continue_()
                    return
                requests.append((request.method, path))
                if path == "/api/auth/check":
                    body = {"auth_required": False, "user": None}
                elif path == "/api/tabs":
                    body = [
                        {
                            "id": TAB,
                            "name": "Native sub-agents",
                            "cwd": "/fixture/project",
                            "agent_type": "codex",
                            "session_kind": "chat",
                            "port": 0,
                            "is_active": True,
                            "created_at": NOW,
                        }
                    ]
                elif (
                    path in {"/api/tabs/status", "/api/env-presets", "/api/tabs/archived"}
                    and request.method == "GET"
                ):
                    body = []
                elif path == "/api/feishu/bot/binding" and request.method == "GET":
                    # The native-region change is tested against the current Bot UI.
                    body = {"binding": None}
                elif path.endswith("/capabilities"):
                    body = {
                        "structured": True,
                        "send_message": True,
                        "supports_goals": False,
                        "supports_images": False,
                        "agent_type": "codex",
                        "supports_mode_switch": False,
                    }
                elif path.endswith("/events"):
                    body = {
                        "events": EVENTS,
                        "next_sequence": len(EVENTS),
                        "has_more": False,
                    }
                elif path.endswith("/wait"):
                    body = {
                        "events": [],
                        "next_sequence": len(EVENTS),
                        "has_more": False,
                    }
                elif path.endswith("/live"):
                    await route.fulfill(
                        status=200,
                        content_type="text/event-stream",
                        body=": fixture\n\n",
                    )
                    return
                elif path.endswith("/goal/current"):
                    body = None
                elif path.endswith("/work"):
                    body = []
                elif path.endswith("/view"):
                    body = {"ok": True}
                elif "network" in path:
                    body = {"hostname": "fixture", "addresses": []}
                else:
                    unknown_requests.append((request.method, path))
                    await route.fulfill(
                        status=501,
                        content_type="application/json",
                        body=json.dumps({"detail": f"unmocked API: {request.method} {path}"}),
                    )
                    return
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(body)
                )

            await context.route_web_socket("**/*", block_websocket)
            await context.route("**/*", api)
            page = await context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(base + f"/?tab={TAB}")

            turns = page.locator(".structured-turn")
            await expect(turns).to_have_count(2)
            old_turn = turns.nth(0)
            next_turn = turns.nth(1)
            await expect(next_turn).to_contain_text("Next question")
            await expect(next_turn).to_contain_text("Next answer.")
            await expect(next_turn.get_by_test_id("subagent-region")).to_have_count(0)
            process_fold = old_turn.locator(".process-fold")
            await expect(process_fold).to_have_count(1)
            await expect(process_fold).to_have_attribute("aria-expanded", "false")
            region = old_turn.get_by_test_id("subagent-region")
            await expect(region).to_have_count(1)
            await expect(region.locator(":scope > summary")).to_be_visible()
            await expect(region.locator(".subagent-region-status.failed")).to_contain_text(
                "1 个本轮失败"
            )
            assert not await region.evaluate("element => element.open")
            await page.screenshot(path=str(output / "collapsed-desktop.png"), full_page=True)
            await process_fold.click()
            await expect(process_fold).to_have_attribute("aria-expanded", "true")
            await expect(region).to_have_count(1)
            await region.locator(":scope > summary").click()
            rows = region.locator(".subagent-thread-row")
            await expect(rows).to_have_count(2)
            await expect(rows.nth(0)).to_have_attribute("data-subagent-thread-id", F6)
            await expect(rows.nth(1)).to_have_attribute("data-subagent-thread-id", FEC)
            await expect(rows.nth(0)).to_contain_text(F6)
            await expect(rows.nth(1)).to_contain_text(FEC)
            await expect(rows.nth(0)).to_contain_text("本轮失败")
            await expect(rows.nth(1)).to_contain_text("本轮活动中")
            await expect(region.locator(".subagent-region-status.failed")).to_contain_text(
                "1 个本轮失败"
            )
            await expect(region.locator(".subagent-region-status.failed")).to_contain_text(
                "1 个本轮活动中"
            )
            await expect(page.locator(".subagent-card--standalone")).to_have_count(0)

            await rows.nth(0).locator(":scope > summary").click()
            await expect(rows.nth(0)).to_contain_text("初始任务")
            await expect(rows.nth(0)).to_contain_text("inspect parser")
            await expect(rows.nth(0)).to_contain_text("first child report")
            await page.screenshot(path=str(output / "desktop.png"), full_page=True)

            await page.set_viewport_size({"width": 390, "height": 844})
            await page.wait_for_function(
                "document.querySelector('.chat-sidebar').classList.contains('mobile-drawer')"
            )
            sidebar = page.locator(".chat-sidebar")
            if "mobile-drawer--open" in (await sidebar.get_attribute("class") or ""):
                await page.get_by_role("button", name="Close sessions", exact=True).click()
            await page.wait_for_function(
                "document.querySelector('.chat-sidebar').getBoundingClientRect().right <= 1"
            )
            await expect(region).to_be_visible()
            for index in range(2):
                bounds = await rows.nth(index).bounding_box()
                assert bounds is not None and bounds["x"] >= 0
                assert bounds["x"] + bounds["width"] <= 391
            await page.screenshot(path=str(output / "mobile.png"), full_page=True)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            assert not unknown_requests, unknown_requests
            assert not errors, errors
            await context.close()

        finally:
            await browser.close()

    report = {
        "result": "PASS",
        "coverage": [
            "one region per parent turn",
            "full thread id isolation",
            "spawn/child deduplication",
            "dispatch versus child lifecycle",
            "expanded child process",
            "mobile overflow",
        ],
        "api_requests": len(requests),
        "unknown_requests": unknown_requests,
        "page_errors": errors,
        "boundary": "All API requests mocked; no backend or provider request.",
    }
    (output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


asyncio.run(main())
