"""Full-app native sub-agent dock smoke with every API request mocked.

A provider-native child (here a TraeX `spawnAgent` thread) has no Chat tab, so
the pane-chrome Agents pill lists it beside Hub children and opens a read-only
detail window instead of jumping to a tab. This covers the whole chain:
timeline part -> dock row -> window -> back to the inline transcript region.

Run only with a dedicated Vite server, never 5173/8173:
  python tests/browser_native_subagent_dock.py http://127.0.0.1:5291 /tmp/native-subagent-dock-ui [browser]
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

PARENT = "fixture-native-parent"
OTHER = "fixture-native-free"
WIDE = {"width": 1680, "height": 900}
THREAD = "01a0e38b-0000-7031-b97e-cccccccccccc"
# A second child that failed before producing anything. Its *status* survives
# the shallow page (`conversation_event_visible` keeps failed/cancelled child
# statuses), so a reloaded page carries a stub thread for it.
THREAD_FAILED = "01a0e38b-0000-7031-b97e-dddddddddddd"
TURN = "fixture-native-turn"
NOW = "2026-11-10T10:00:00Z"
MOBILE = {"width": 390, "height": 844}
# Dock rows are keyed by the timeline turn key (`turn-<turn_id>`) plus the
# provider thread id, which is what the pill's row test id carries.
ROW_ID = f"subagent-row-turn-{TURN}:{THREAD}"
DESKTOP = {"width": 1280, "height": 900}


def tab(tab_id, name, **extra):
    row = {
        "id": tab_id,
        "name": name,
        "cwd": "/fixture/project",
        "agent_type": "codex",
        "session_kind": "chat",
        "port": 0,
        "is_active": True,
        "created_at": NOW,
    }
    row.update(extra)
    return row


def event(sequence, event_type, payload, *, message_id=None, call_id=None):
    return {
        "stream_sequence": sequence,
        "session_id": "fixture-session",
        "tab_id": PARENT,
        "agent_type": "codex",
        "type": event_type,
        "turn_id": TURN,
        "message_id": message_id,
        "call_id": call_id,
        "payload": payload,
        "created_at": NOW,
        "redacted": False,
    }


def child_event(sequence, event_type, payload, *, thread=THREAD, message_id=None, call_id=None):
    return event(
        sequence,
        event_type,
        {**payload, "subagent_thread": thread},
        message_id=message_id,
        call_id=call_id,
    )


# One completed parent turn that delegated to a single native child thread.
EVENTS = [
    event(
        0,
        "turn_started",
        {"summary": "Check the parser", "mode": "default"},
        message_id=f"{TURN}:user",
    ),
    event(
        1,
        "tool_call_started",
        {
            "tool_call_id": "spawn-native",
            "name": "spawnAgent",
            "args": {"prompt": "inspect parser", "receiverThreadIds": [THREAD]},
        },
        call_id="spawn-native",
    ),
    event(
        2,
        "tool_call_completed",
        {"tool_call_id": "spawn-native", "status": "completed", "result": "{}"},
        call_id="spawn-native",
    ),
    child_event(
        3,
        "text_delta",
        {"text": "child report ready"},
        message_id=f"{TURN}:sub:{THREAD}:assistant",
    ),
    child_event(
        4,
        "status",
        {
            "text": "Sub-agent running",
            "provider_status": "subagent_lifecycle",
            "subagent_status": "active",
            "snapshot": True,
        },
        message_id=f"subagent-lifecycle:{THREAD}",
    ),
    # Survives the shallow conversation page, unlike prose and tool calls.
    child_event(
        5,
        "status",
        {
            "text": "Sub-agent turn failed",
            "provider_status": "subagent_lifecycle",
            "subagent_status": "failed",
            "snapshot": True,
        },
        thread=THREAD_FAILED,
        message_id=f"subagent-lifecycle:{THREAD_FAILED}",
    ),
    event(6, "text_delta", {"text": "Parent answer."}, message_id=f"{TURN}:assistant"),
    event(7, "turn_completed", {"status": "completed"}, message_id=TURN),
]


# A plain question-and-answer turn: no child threads at all.
OTHER_EVENTS = [
    {
        **event(0, "turn_started", {"summary": "Other question"}),
        "tab_id": OTHER,
        "turn_id": "fixture-other-turn",
    },
    {
        **event(1, "text_delta", {"text": "Other answer."}),
        "tab_id": OTHER,
        "turn_id": "fixture-other-turn",
    },
    {
        **event(2, "turn_completed", {"status": "completed"}),
        "tab_id": OTHER,
        "turn_id": "fixture-other-turn",
    },
]


# What `view=conversation` returns for this turn: the delivered answer and the
# turn edges. Tool calls and child events stay behind the detail stream
# (`conversation_event_visible`), which is what the descriptor below announces.
SHALLOW_SEQUENCES = {0, 5, 6, 7}
DETAIL = {
    "turn_id": TURN,
    "first_sequence": 0,
    "last_sequence": 7,
    "event_count": 8,
}
LAST_SEQUENCE = 7
# `useAgentStream` sends `since_sequence` (inclusive cursor). The live stream is
# delivered batch by batch from /wait, the way a running delegation arrives.
PARENT_BATCHES = {
    -1: EVENTS[0:3],
    2: EVENTS[3:5],
    4: EVENTS[5:8],
}
OTHER_BATCHES = {-1: OTHER_EVENTS}


async def wait_until(predicate, *, timeout_ms=5000, interval_ms=100):
    deadline = time.perf_counter() + timeout_ms / 1000
    while time.perf_counter() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval_ms / 1000)
    raise AssertionError("condition not reached before timeout")


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
    unexpected_external_requests = []
    blocked_external_requests = []
    event_tabs = []
    detail_requests = []
    state = {"history": False}

    async with browser_run.context(viewport=DESKTOP) as context:

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
                body = [tab(PARENT, "Parent Chat"), tab(OTHER, "Other Chat")]
            elif path == "/api/tabs/archived" and request.method == "GET":
                body = []
            elif path == "/api/tabs/status" and request.method == "GET":
                body = [
                    {
                        "tab_id": PARENT,
                        "status": "idle",
                        "status_text": "Idle",
                        "detail": "",
                    },
                    {
                        "tab_id": OTHER,
                        "status": "idle",
                        "status_text": "Idle",
                        "detail": "",
                    },
                ]
            elif path == "/api/subagents" and request.method == "GET":
                # No Hub children: every dock row here is provider-native.
                query = parse_qs(parsed.query)
                assert query.get("include_closed") == ["true"]
                body = []
            elif path.startswith("/api/workspaces/tabs/") and path.endswith("/messages"):
                body = {"messages": []}
            elif path.startswith("/api/tabs/sidebar-preferences"):
                body = {"pinned_tab_ids": []}
            elif path == "/api/env-presets":
                body = {"custom_presets": [], "hidden_builtin_ids": []}
            elif path == "/api/feishu/bot/bots":
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
            elif path.endswith("/stream/events"):
                query = parse_qs(parsed.query)
                if f"/tabs/{PARENT}/" in path:
                    event_tabs.append(PARENT)
                    full = EVENTS
                elif f"/tabs/{OTHER}/" in path:
                    # A second Chat tab with no native child: switching to it
                    # must not leave the first tab's rows on the pill.
                    event_tabs.append(OTHER)
                    full = OTHER_EVENTS
                else:
                    full = []
                since = int((query.get("since_sequence") or ["-1"])[0])
                turn_detail = (query.get("turn_id") or [None])[0]
                through = int((query.get("through_sequence") or ["-1"])[0])
                if turn_detail is not None:
                    # Execution detail for one turn: everything the shallow page
                    # hid, bounded by the descriptor's `through_sequence`.
                    detail_requests.append(turn_detail)
                    upper = through if through >= 0 else max(row["stream_sequence"] for row in full)
                    events = [
                        row
                        for row in full
                        if row["turn_id"] == turn_detail and since < row["stream_sequence"] <= upper
                    ]
                    details = []
                elif not state["history"]:
                    # No history until the reload: the live stream owns the
                    # transcript, and an empty page must echo the cursor rather
                    # than rewind it.
                    events, details = [], []
                else:
                    events = [
                        row
                        for row in full
                        if row["stream_sequence"] in SHALLOW_SEQUENCES
                        and row["stream_sequence"] > since
                    ]
                    details = [dict(DETAIL)] if full is EVENTS and since < LAST_SEQUENCE else []
                body = {
                    "events": events,
                    "next_sequence": events[-1]["stream_sequence"] if events else since,
                    "has_more": False,
                    "history_details": details,
                }
            elif path.endswith("/wait"):
                # The real long-poll blocks; the client re-issues immediately
                # when a poll returns at once, so hold each empty poll briefly.
                try:
                    payload = json.loads(request.post_data or "{}")
                except json.JSONDecodeError:
                    payload = {}
                cursor = int(payload.get("since_sequence", -1))
                if f"/tabs/{PARENT}/" in path:
                    batches = PARENT_BATCHES
                elif f"/tabs/{OTHER}/" in path:
                    batches = OTHER_BATCHES
                else:
                    batches = {}
                batch = batches.get(cursor, [])
                if not batch:
                    await asyncio.sleep(0.25)
                body = {
                    "events": batch,
                    "next_sequence": batch[-1]["stream_sequence"] if batch else cursor,
                    "has_more": False,
                }
            elif path.endswith("/live"):
                try:
                    await route.fulfill(
                        status=200,
                        content_type="text/event-stream",
                        body=": fixture\n\n",
                    )
                except AssertionError:
                    try:
                        await route.abort()
                    except Exception:
                        pass
                return
            elif path.endswith("/goal/current"):
                body = None
            elif path.endswith("/view"):
                body = {"ok": True}
            elif "network" in path:
                body = {"hostname": "fixture", "addresses": []}
            elif path == "/api/dot/identity":
                body = {"name": "Dot", "avatar": "", "avatar_image": None}
            else:
                unknown_requests.append((request.method, path))
                await route.fulfill(
                    status=501,
                    content_type="application/json",
                    body=json.dumps({"detail": f"unmocked API: {request.method} {path}"}),
                )
                return
            try:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(body)
                )
            except AssertionError:
                try:
                    await route.abort()
                except Exception:
                    pass

        await context.route_web_socket("**/*", block_websocket)
        await context.route("**/*", api)
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        started = time.perf_counter()
        await page.goto(base + f"/?tab={PARENT}")

        # The inline region is folded into the turn's process: the child is
        # reachable from the dock without expanding anything first.
        pill = page.get_by_test_id("subagent-dock-pill")
        await expect(pill).to_be_visible(timeout=15000)
        await expect(pill).to_contain_text("1 running")
        await expect(page.get_by_test_id("native-subagent-overlay")).to_have_count(0)

        await pill.click()
        popover = page.get_by_test_id("subagent-popover")
        await expect(popover).to_be_visible()
        row = popover.get_by_test_id(ROW_ID)
        await expect(row).to_have_attribute("data-kind", "native")
        await expect(row).to_have_attribute("data-state", "running")
        await expect(row).to_contain_text("TraeX sub-agent")
        await expect(row).to_contain_text("child report ready")
        await expect(row).to_contain_text("View")
        await page.screenshot(path=str(output / "00-dock-popover-native.png"))

        # Opening a native row renders the detail window in the Chat body, so
        # the popover closes instead of stacking surfaces.
        await row.click()
        await expect(page.get_by_test_id("subagent-popover")).to_have_count(0)
        overlay = page.get_by_test_id("native-subagent-overlay")
        await expect(overlay).to_be_visible()
        window = overlay.locator(".native-subagent-window")
        await expect(window).to_have_attribute("role", "dialog")
        await expect(window).to_have_attribute("aria-modal", "true")
        await expect(window.locator(".native-subagent-badge")).to_have_text("Native sub-agent")
        await expect(window.locator(".native-subagent-thread")).to_have_text(THREAD)
        await expect(window.locator(".native-subagent-meta")).to_contain_text("Turn #1")
        await expect(window.locator(".native-subagent-head .tool-status")).to_have_text("Active")
        # Same rows as the transcript: delegated prompt plus the child's own
        # report, with the dispatch result one expansion away.
        await expect(window.locator(".subthread-launch .subthread-instruction-arrow")).to_have_text(
            "→ Original task"
        )
        await expect(window).to_contain_text("inspect parser")
        await expect(window).to_contain_text("child report ready")
        await expect(window.locator(".subthread-launch-result > summary")).to_have_text(
            "Dispatch result"
        )
        assert await page.evaluate("document.body.style.overflow") == "hidden"
        await page.screenshot(path=str(output / "01-native-window.png"))

        # Modal hygiene: Tab cannot walk out of the window, in either direction,
        # including from a click that dropped focus on <body>.
        for _ in range(6):
            await page.keyboard.press("Tab")
            assert await page.evaluate(
                "Boolean(document.activeElement && document.activeElement.closest("
                "'[data-testid=native-subagent-overlay]'))"
            ), "Tab must stay inside the window"
        await page.keyboard.press("Shift+Tab")
        assert await page.evaluate(
            "Boolean(document.activeElement && document.activeElement.closest("
            "'[data-testid=native-subagent-overlay]'))"
        ), "Shift+Tab must stay inside the window"
        await window.locator(".native-subagent-message, .native-subagent-meta").first.click()
        assert (
            await page.evaluate(
                "Boolean(document.activeElement && document.activeElement.closest("
                "'[data-testid=native-subagent-overlay]'))"
            )
            is False
        ), "the tap must have moved focus out of the window"
        await page.keyboard.press("Tab")
        assert await page.evaluate(
            "Boolean(document.activeElement && document.activeElement.closest("
            "'[data-testid=native-subagent-overlay]'))"
        ), "Tab must pull focus back into the window"

        # Done closes it and returns focus to the pill it was opened from.
        await window.get_by_test_id("native-subagent-close").click()
        await expect(page.get_by_test_id("native-subagent-overlay")).to_have_count(0)
        await expect(pill).to_be_focused()
        assert await page.evaluate("document.body.style.overflow") == ""

        # Esc closes too, including after a tap moved focus to <body>.
        await pill.click()
        await row.click()
        await expect(overlay).to_be_visible()
        await window.locator(".native-subagent-badge").click()
        assert (
            await page.evaluate(
                "Boolean(document.activeElement && document.activeElement.closest("
                "'[data-testid=native-subagent-overlay]'))"
            )
            is False
        ), "the tap must have moved focus out of the window"
        await page.keyboard.press("Escape")
        await expect(page.get_by_test_id("native-subagent-overlay")).to_have_count(0)

        # A top-layer <dialog> (the image/attachment lightbox opened from these
        # rows) owns Escape: the window must stay open beneath it.
        await pill.click()
        await row.click()
        await expect(overlay).to_be_visible()
        await page.evaluate("""() => {
              const dialog = document.createElement('dialog')
              dialog.id = 'review-top-layer-dialog'
              dialog.textContent = 'preview'
              document.body.appendChild(dialog)
              dialog.showModal()
            }""")
        await page.keyboard.press("Escape")
        await expect(overlay).to_be_visible()
        await page.evaluate("() => document.getElementById('review-top-layer-dialog').remove()")
        await page.keyboard.press("Escape")
        await expect(page.get_by_test_id("native-subagent-overlay")).to_have_count(0)

        # A backdrop tap dismisses without touching the transcript.
        await pill.click()
        await row.click()
        await expect(overlay).to_be_visible()
        await overlay.click(position={"x": 12, "y": 120})
        await expect(page.get_by_test_id("native-subagent-overlay")).to_have_count(0)

        # "Show in transcript" expands the folded turn and lands on the child's
        # own inline row, which is the place the conversation continues.
        await pill.click()
        await row.click()
        await expect(overlay).to_be_visible()
        await window.get_by_role("button", name="Show in transcript").click()
        await expect(page.get_by_test_id("native-subagent-overlay")).to_have_count(0)
        process_fold = page.get_by_test_id("history-details-toggle")
        await expect(process_fold).to_have_attribute("aria-expanded", "true")
        inline = page.locator(f'[data-subagent-thread-id="{THREAD}"]')
        await expect(inline).to_have_count(1)
        assert await inline.evaluate("element => element.open"), "the inline row must be expanded"
        region = page.get_by_test_id("subagent-region")
        assert await region.evaluate("element => element.open"), "the region must be expanded"
        await expect(inline).to_contain_text("child report ready")
        # This turn is short enough that the reveal has nothing to scroll, so no
        # scroll event rearms the tail it detached: the reader must not be left
        # detached (with the Latest pill) at the bottom of the transcript.
        await expect(page.get_by_role("button", name="Scroll to latest message")).to_have_count(0)
        await page.screenshot(path=str(output / "02-show-in-transcript.png"))

        # Wide panes rest the list in the gutter rail instead of a popover;
        # that rail row is the default desktop entry point and must open the
        # window too (and keep focus on itself, since it stays mounted).
        await page.set_viewport_size(WIDE)
        rail = page.get_by_test_id("subagent-rail")
        await expect(rail).to_be_visible()
        rail_row = rail.get_by_test_id(ROW_ID)
        await expect(rail_row).to_have_attribute("data-kind", "native")
        await rail_row.click()
        await expect(overlay).to_be_visible()
        # The window fades in over 160ms; capture it settled, not mid-fade.
        await expect(window).to_be_visible()
        await page.wait_for_timeout(250)
        await page.screenshot(path=str(output / "03-wide-rail-window.png"))
        await window.get_by_test_id("native-subagent-close").click()
        await expect(page.get_by_test_id("native-subagent-overlay")).to_have_count(0)
        await expect(rail_row).to_be_focused()
        await page.set_viewport_size(DESKTOP)

        # The pill belongs to the pane, the timeline to the cached tab: switching
        # away with a window OPEN must remove it (it is teleported to <body>, so
        # hiding the pane does not hide it) and release the scroll lock, and the
        # activated tab must list its own children (none on the second Chat). The
        # modal covers the sidebar, so the switch is dispatched the way a
        # keyboard or CLI switch would reach it.
        await pill.click()
        await row.click()
        await expect(overlay).to_be_visible()
        await page.evaluate(
            """id => document.querySelector(`.chat-sidebar [data-tab-id="${id}"]`)?.click()""",
            OTHER,
        )
        await expect(page.get_by_text("Other answer.", exact=True)).to_be_visible()
        await expect(page.get_by_test_id("native-subagent-overlay")).to_have_count(0)
        # Deactivation must release the window's body scroll lock.
        assert await page.evaluate("document.body.style.overflow") == ""
        await expect(page.get_by_test_id("subagent-dock-pill")).to_have_count(0)
        # A hidden cached pane may not publish its own rows onto this pill.
        await page.wait_for_timeout(1200)
        await expect(page.get_by_test_id("subagent-dock-pill")).to_have_count(0)
        await page.evaluate(
            """id => document.querySelector(`.chat-sidebar [data-tab-id="${id}"]`)?.click()""",
            PARENT,
        )
        await expect(pill).to_be_visible()
        await expect(pill).to_contain_text("1 running")
        # The window was dropped on deactivation: nothing may reopen it.
        await expect(page.get_by_test_id("native-subagent-overlay")).to_have_count(0)

        # Phones: the same pill, then the sheet, then the window as a bottom
        # sheet — and no horizontal overflow at any step.
        await page.set_viewport_size(MOBILE)
        await page.wait_for_function(
            "document.querySelector('.chat-sidebar').classList.contains('mobile-drawer')"
        )
        sidebar = page.locator(".chat-sidebar")
        if "mobile-drawer--open" in (await sidebar.get_attribute("class") or ""):
            await page.get_by_role("button", name="Close sessions", exact=True).click()
        await page.wait_for_function(
            "document.querySelector('.chat-sidebar').getBoundingClientRect().right <= 1"
        )
        await expect(page.locator("[data-testid=subagent-dock-pill]")).to_have_count(1)
        await expect(pill).to_be_visible()
        await pill.click()
        sheet = page.get_by_test_id("subagent-sheet")
        await expect(sheet).to_be_visible()
        await expect(sheet.get_by_test_id(ROW_ID)).to_have_attribute("data-kind", "native")
        await page.screenshot(path=str(output / "04-mobile-sheet-native.png"))
        await sheet.get_by_test_id(ROW_ID).click()
        await expect(sheet).to_have_count(0)
        await expect(overlay).to_be_visible()
        bounds = await window.bounding_box()
        assert bounds is not None, "the mobile window must be laid out"
        assert bounds["width"] <= 391, bounds
        assert bounds["y"] + bounds["height"] >= 840, bounds
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        await page.wait_for_timeout(250)
        await page.screenshot(path=str(output / "05-mobile-window-native.png"))
        await window.get_by_test_id("native-subagent-close").click()
        await expect(page.get_by_test_id("native-subagent-overlay")).to_have_count(0)
        assert await page.evaluate("document.body.style.overflow") == ""
        await page.set_viewport_size(DESKTOP)

        # ── A reloaded page sees a real shallow history: the delivered answer,
        # no child events, plus a descriptor for the turn's execution detail.
        # The dock must not invent a child row it cannot fill in, and opening
        # that turn must be what makes the child reachable.
        state["history"] = True
        await page.reload()
        await expect(page.get_by_text("Parent answer.", exact=True)).to_have_count(1)
        # The shallow page carries only the failed child's status, which the
        # reducer turns into a stub thread. That stub must not become a row:
        # its window could show nothing, and the active sibling is not known yet.
        await expect(page.get_by_test_id("subagent-dock-pill")).to_have_count(0)
        assert detail_requests == [], "a lazy turn must not be read before it is opened"
        process_fold = page.get_by_test_id("history-details-toggle")
        await expect(process_fold).to_have_attribute("aria-expanded", "false")
        await process_fold.click()
        await expect(process_fold).to_have_attribute("aria-expanded", "true")
        assert detail_requests == [TURN], detail_requests
        # Now the children are readable: the inline region and the pill agree.
        inline = page.locator(f'[data-subagent-thread-id="{THREAD}"]')
        await expect(page.locator(f'[data-subagent-thread-id="{THREAD}"]')).to_have_count(1)
        await expect(page.locator(f'[data-subagent-thread-id="{THREAD_FAILED}"]')).to_have_count(1)
        await expect(pill).to_be_visible()
        await expect(pill).to_contain_text("1 running")
        await page.screenshot(path=str(output / "06-historical-detail.png"))

        # A historical child opens the same window with the same rows.
        await pill.click()
        await page.get_by_test_id("subagent-popover").get_by_test_id(ROW_ID).click()
        await expect(overlay).to_be_visible()
        await expect(window.locator(".native-subagent-thread")).to_have_text(THREAD)
        await expect(window).to_contain_text("inspect parser")
        await expect(window).to_contain_text("child report ready")
        await expect(window.locator(".subthread-launch-result > summary")).to_have_text(
            "Dispatch result"
        )
        await page.screenshot(path=str(output / "07-historical-window.png"))
        await window.get_by_test_id("native-subagent-close").click()
        await expect(page.get_by_test_id("native-subagent-overlay")).to_have_count(0)

        # Let one poll interval pass so an unmocked request would surface.
        await page.wait_for_timeout(5600)
        await expect(pill).to_contain_text("1 running")
        assert not unknown_requests, unknown_requests
        assert not unexpected_external_requests, unexpected_external_requests
        assert not errors, errors

    timings = {"elapsed_ms": round((time.perf_counter() - started) * 1000)}
    report = {
        "result": "PASS",
        "coverage": [
            "native child appears in the pane-chrome Agents pill with no Hub children",
            "popover row carries the native kind, provider label, child report and View action",
            "clicking a native row opens the Chat body's detail window (popover closes)",
            "window replays the inline rows: delegated prompt, child report, dispatch result",
            "dialog semantics: aria-modal, Tab trap, body scroll lock",
            "Done returns focus to the pill and releases the lock",
            "Esc closes after focus left the window; a backdrop tap closes",
            "Show in transcript expands the folded turn and expands the child's inline row",
            "wide panes: the gutter rail's row opens the window and keeps focus",
            "a Chat tab switch lists the activated tab's children and leaves exactly one window",
            "mobile: same pill, sheet, then the window as a bottom sheet with no overflow",
            "reloaded shallow history lists no child (not even the failed stub) until its turn is opened",
            "historical child opens the same window with prompt, report and dispatch result",
        ],
        "event_tabs": event_tabs,
        "detail_requests": detail_requests,
        "timings": timings,
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
