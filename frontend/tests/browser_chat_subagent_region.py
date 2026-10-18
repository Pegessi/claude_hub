"""Full-app native sub-agent region smoke with every API request mocked.

Run only with a dedicated Vite server, never 5173/8173:
  python tests/browser_chat_subagent_region.py http://127.0.0.1:5287 /tmp/subagent-region-ui [browser]
"""

import asyncio
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from browser_lifecycle import BrowserRun, run_browser_main
from playwright.async_api import expect

F6 = "01a0e38b-0000-7031-b97e-aaaaaaaaaaaa"
FEC = "01a0e38b-0000-7031-b97e-bbbbbbbbbbbb"
TURN = "fixture-parent-turn"
TAB = "fixture-subagent-tab"
OTHER_TAB = "fixture-other-chat"
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


async def main(browser_run: BrowserRun) -> None:
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    base, output_arg = sys.argv[1:3]
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
    blocked_external_requests = []
    unexpected_external_requests = []
    detail_requests = []
    summary_requests = []
    edit_requests = []
    edited_events = None
    fail_first_detail = True
    delay_next_detail = False
    delayed_started = asyncio.Event()
    delayed_finished = asyncio.Event()
    timings = {}
    other_events = [
        {
            **event(index, kind, payload),
            "tab_id": OTHER_TAB,
            "turn_id": "other-turn",
        }
        for index, (kind, payload) in enumerate(
            (
                ("turn_started", {"summary": "Other question"}),
                ("text_delta", {"text": "Other answer."}),
                ("turn_completed", {"status": "completed"}),
            )
        )
    ]

    async with browser_run.context(viewport={"width": 1280, "height": 900}) as context:

        async def block_websocket(socket):
            await socket.close(code=1000, reason="mocked UI smoke")

        async def api(route):
            nonlocal fail_first_detail, delay_next_detail, edited_events
            request = route.request
            parsed = urlparse(request.url)
            if (parsed.scheme, parsed.hostname, parsed.port) != (
                parsed_base.scheme,
                parsed_base.hostname,
                parsed_base.port,
            ):
                blocked_external_requests.append(request.url)
                if parsed.scheme != "https" or parsed.hostname not in {
                    "fonts.googleapis.com",
                    "fonts.gstatic.com",
                }:
                    unexpected_external_requests.append(request.url)
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
                    },
                    {
                        "id": OTHER_TAB,
                        "name": "Other Chat",
                        "cwd": "/fixture/project",
                        "agent_type": "codex",
                        "session_kind": "chat",
                        "port": 0,
                        "is_active": True,
                        "created_at": NOW,
                    },
                ]
            elif (
                path in {"/api/tabs/status", "/api/tabs/archived"}
                and request.method == "GET"
                and not parsed.query
            ):
                body = []
            elif path.startswith("/api/tabs/sidebar-preferences"):
                # Read, one-shot pin migration and per-tab pin updates all
                # return the same shape; this scenario never depends on pins.
                body = {"pinned_tab_ids": []}
            elif path == "/api/env-presets" and request.method == "GET" and not parsed.query:
                body = {"custom_presets": [], "hidden_builtin_ids": []}
            elif path == "/api/feishu/bot/bots" and request.method == "GET" and not parsed.query:
                body = {
                    "pool_revision": 0,
                    "bots": [],
                    "pool_editable": True,
                    "deprecated_env": [],
                    "focus_bot_id": None,
                }
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
                query = parse_qs(parsed.query)
                since = int(query.get("since_sequence", ["-1"])[0])
                if "turn_id" in query:
                    assert path == f"/api/workspaces/tabs/{TAB}/stream/events"
                    assert query["turn_id"] == [TURN]
                    assert query.get("through_sequence") == ["9"]
                    assert query.get("compact") == ["true"]
                    assert "view" not in query
                    detail_requests.append(query)
                    if fail_first_detail:
                        fail_first_detail = False
                        await route.fulfill(
                            status=503,
                            content_type="application/json",
                            body=json.dumps({"detail": "Temporary detail read failure"}),
                        )
                        return
                    # A fixed upper bound plus multiple pages must reconstruct
                    # this turn without touching another turn or the live cursor.
                    if delay_next_detail:
                        delay_next_detail = False
                        delayed_started.set()
                        await asyncio.sleep(0.6)
                        delayed_finished.set()
                    else:
                        await asyncio.sleep(0.05)
                    matching = [
                        row
                        for row in EVENTS
                        if row["turn_id"] == TURN and since < row["stream_sequence"] <= 9
                    ]
                    selected = matching[:5]
                    body = {
                        "events": selected,
                        "next_sequence": selected[-1]["stream_sequence"] if selected else 9,
                        "has_more": len(matching) > len(selected),
                    }
                else:
                    assert query.get("view") == ["conversation"]
                    summary_requests.append({"path": path, "since": since})
                    if path == f"/api/workspaces/tabs/{OTHER_TAB}/stream/events":
                        body = {
                            "events": [
                                row for row in other_events if row["stream_sequence"] > since
                            ],
                            "next_sequence": 2,
                            "has_more": False,
                            "history_details": [],
                        }
                    elif edited_events is not None:
                        body = {
                            "events": [
                                row for row in edited_events if row["stream_sequence"] > since
                            ],
                            "next_sequence": max(since, 2),
                            "has_more": False,
                            "history_details": [],
                        }
                    else:
                        # Failed child status remains visible without downloading
                        # the child prose, spawn arguments or results.
                        visible = {0, 4, 8, 9, 10, 11, 12}
                        body = {
                            "events": [
                                row
                                for row in EVENTS
                                if row["stream_sequence"] in visible
                                and row["stream_sequence"] > since
                            ],
                            "next_sequence": 12,
                            "has_more": False,
                            "history_details": (
                                [
                                    {
                                        "turn_id": TURN,
                                        "first_sequence": 0,
                                        "last_sequence": 9,
                                        "event_count": 10,
                                    }
                                ]
                                if since < 9
                                else []
                            ),
                        }
            elif path.endswith("/edit-resend") and request.method == "POST":
                payload = json.loads(request.post_data or "{}")
                assert payload["turn_id"] == TURN
                assert payload["text"] == "Edited question"
                assert payload["client_turn_id"]
                edit_requests.append(payload)
                edited_events = [
                    {**event(index, kind, body), "turn_id": payload["client_turn_id"]}
                    for index, (kind, body) in enumerate(
                        (
                            ("turn_started", {"summary": "Edited question"}),
                            ("text_delta", {"text": "Edited answer."}),
                            ("turn_completed", {"status": "completed"}),
                        )
                    )
                ]
                body = {"ok": True}
            elif path.endswith("/wait"):
                requested_cursor = json.loads(request.post_data or "{}")["since_sequence"]
                expected_cursor = 2 if OTHER_TAB in path else 12
                if edited_events is None or OTHER_TAB in path:
                    assert requested_cursor == expected_cursor
                else:
                    # An old wait may still arrive while edit commits. Preserve
                    # that request's cursor; generation ownership must reject it.
                    expected_cursor = requested_cursor
                await asyncio.sleep(0.1)
                body = {
                    "events": [],
                    "next_sequence": expected_cursor,
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
            elif path.endswith("/view"):
                body = {"ok": True}
            elif "network" in path:
                body = {"hostname": "fixture", "addresses": []}
            elif path == "/api/dot/identity" and request.method == "GET":
                # The Dot launcher in the tab bar reads Dot's name and avatar.
                body = {"name": "Dot", "avatar": "", "avatar_image": None}
            else:
                unknown_requests.append((request.method, path))
                await route.fulfill(
                    status=501,
                    content_type="application/json",
                    body=json.dumps({"detail": f"unmocked API: {request.method} {path}"}),
                )
                return
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

        await context.route_web_socket("**/*", block_websocket)
        await context.route("**/*", api)
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        started = time.perf_counter()
        await page.goto(base + f"/?tab={TAB}")

        turns = page.locator(".structured-turn")
        await expect(turns).to_have_count(2)
        old_turn = turns.nth(0)
        next_turn = turns.nth(1)
        await expect(old_turn).to_contain_text("Run two checks")
        await expect(old_turn).to_contain_text("Parent answer.")
        await expect(next_turn).to_contain_text("Next question")
        await expect(next_turn).to_contain_text("Next answer.")
        timings["first_questions_and_answers_ms"] = (time.perf_counter() - started) * 1000
        await expect(next_turn.get_by_test_id("subagent-region")).to_have_count(0)
        await expect(old_turn).not_to_contain_text("inspect parser")
        await expect(old_turn).not_to_contain_text("first child report")
        visible_failure = old_turn.get_by_text(re.compile("failed", re.IGNORECASE)).and_(
            page.locator(":visible")
        )
        await expect(visible_failure.first).to_be_visible()
        assert detail_requests == [], "Collapsed history must not fetch execution details"
        process_fold = old_turn.get_by_test_id("history-details-toggle")
        await expect(process_fold).to_have_count(1)
        await expect(process_fold).to_have_attribute("aria-expanded", "false")
        await page.screenshot(path=str(output / "collapsed-desktop.png"), full_page=True)
        await process_fold.click()
        retry = old_turn.get_by_test_id("history-details-retry")
        await expect(retry).to_be_visible()
        await expect(old_turn).to_contain_text("Parent answer.")
        assert len(detail_requests) == 1
        await retry.click()
        await expect(process_fold).to_have_attribute("aria-expanded", "true")
        assert len(detail_requests) == 3, detail_requests
        assert [row["since_sequence"] for row in detail_requests] == [["-1"], ["-1"], ["4"]]
        region = old_turn.get_by_test_id("subagent-region")
        await expect(region).to_have_count(1)
        summary = region.locator(":scope > summary")
        await expect(summary).to_be_visible()
        await expect(summary.locator(".subagent-badge")).to_have_text("Native sub-agents")
        await expect(summary.locator(".subagent-headline")).to_have_text("2 sub-agents")
        await expect(summary.locator(".subagent-region-status.failed")).to_have_text(
            "1 failed · 1 active"
        )
        assert not await region.evaluate("element => element.open")
        await region.locator(":scope > summary").click()
        rows = region.locator(".subagent-thread-row")
        await expect(rows).to_have_count(2)
        await expect(rows.nth(0)).to_have_attribute("data-subagent-thread-id", F6)
        await expect(rows.nth(1)).to_have_attribute("data-subagent-thread-id", FEC)
        await expect(rows.nth(0)).to_contain_text(F6)
        await expect(rows.nth(1)).to_contain_text(FEC)
        await expect(rows.nth(0).locator(":scope > summary .tool-status")).to_have_text("Failed")
        await expect(rows.nth(1).locator(":scope > summary .tool-status")).to_have_text("Active")
        await expect(page.locator(".subagent-card--standalone")).to_have_count(0)

        await rows.nth(0).locator(":scope > summary").click()
        await expect(
            rows.nth(0).locator(".subthread-launch .subthread-instruction-arrow")
        ).to_have_text("→ Original task")
        await expect(rows.nth(0).locator(".subthread-launch-result > summary")).to_have_text(
            "Dispatch result"
        )
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

        await page.set_viewport_size({"width": 1280, "height": 900})
        await process_fold.click()
        await expect(process_fold).to_have_attribute("aria-expanded", "false")
        await expect(old_turn.get_by_text("first child report", exact=True)).to_be_hidden()
        await expect(visible_failure.first).to_be_visible()
        await page.locator(f'.chat-sidebar [data-tab-id="{OTHER_TAB}"]').click()
        await expect(page.get_by_text("Other answer.", exact=True)).to_be_visible()
        started = time.perf_counter()
        await page.locator(f'.chat-sidebar [data-tab-id="{TAB}"]').click()
        await expect(page.get_by_text("Parent answer.", exact=True)).to_be_visible()
        timings["cached_switch_questions_and_answers_ms"] = (time.perf_counter() - started) * 1000
        await expect(turns).to_have_count(2)
        await expect(process_fold).to_have_attribute("aria-expanded", "false")
        await process_fold.click()
        await expect(process_fold).to_have_attribute("aria-expanded", "true")
        assert len(detail_requests) == 3, "Cached details must survive folding and Chat switches"
        await expect(page.get_by_text("Parent answer.", exact=True)).to_have_count(1)
        assert any(item["since"] == 12 for item in summary_requests), summary_requests

        # A new page has no in-memory detail cache. Switch away while its
        # expansion read is pending, then allow that old response to finish.
        await page.reload()
        await expect(page.get_by_text("Parent answer.", exact=True)).to_be_visible()
        delay_next_detail = True
        before_delayed = len(detail_requests)
        await process_fold.click()
        await asyncio.wait_for(delayed_started.wait(), timeout=5)
        await page.locator(f'.chat-sidebar [data-tab-id="{OTHER_TAB}"]').click()
        await expect(page.get_by_text("Other answer.", exact=True)).to_be_visible()
        await asyncio.wait_for(delayed_finished.wait(), timeout=5)
        await expect(page.get_by_text("Parent answer.", exact=True)).to_have_count(0)
        await expect(page.get_by_text("first child report", exact=True)).to_have_count(0)
        await page.locator(f'.chat-sidebar [data-tab-id="{TAB}"]').click()
        await expect(page.get_by_text("Parent answer.", exact=True)).to_be_visible()
        await expect(process_fold).to_be_enabled()
        if await process_fold.get_attribute("aria-expanded") == "false":
            await process_fold.click()
        retry = old_turn.get_by_test_id("history-details-retry")
        if await retry.is_visible():
            await retry.click()
        await expect(process_fold).to_have_attribute("aria-expanded", "true")
        assert len(detail_requests) >= before_delayed + 3, detail_requests

        # This pane has a warm history cache from the switches above. Editing
        # the first message truncates/renumbers the mocked store, so the next
        # hydration must start at -1 rather than restoring the cached cursor 12.
        await old_turn.hover()
        await old_turn.get_by_role("button", name="Edit message", exact=True).click()
        await old_turn.get_by_placeholder("Edit your message...").fill("Edited question")
        await old_turn.get_by_role("button", name="Resend", exact=True).click()
        await expect(page.get_by_text("Edited question", exact=True)).to_be_visible()
        await expect(page.get_by_text("Edited answer.", exact=True)).to_be_visible()
        await expect(turns).to_have_count(1)
        await expect(page.get_by_text("Next answer.", exact=True)).to_have_count(0)
        await expect(page.get_by_text("Parent answer.", exact=True)).to_have_count(0)
        assert len(edit_requests) == 1
        assert summary_requests[-1]["since"] == -1, summary_requests
        assert not unknown_requests, unknown_requests
        assert not unexpected_external_requests, unexpected_external_requests
        assert not errors, errors

    report = {
        "result": "PASS",
        "coverage": [
            "one region per parent turn",
            "full thread id isolation",
            "spawn/child deduplication",
            "dispatch versus child lifecycle",
            "expanded child process",
            "summary-first history without eager detail requests",
            "detail failure retry and fixed-cursor pagination",
            "detail cache across folding and Chat switches",
            "warm-cache edit reloads renumbered history without old future turns",
            "mobile overflow",
        ],
        "edit_requests": len(edit_requests),
        "detail_requests": len(detail_requests),
        "summary_requests": summary_requests,
        "timings_ms": timings,
        "api_requests": len(requests),
        "unknown_requests": unknown_requests,
        "blocked_external_requests": blocked_external_requests,
        "unexpected_external_requests": unexpected_external_requests,
        "page_errors": errors,
        "boundary": "All API requests mocked; no backend or provider request.",
    }
    (output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    run_browser_main(main)
