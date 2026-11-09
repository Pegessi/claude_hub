"""Full-app Hub sub-agent jump card smoke with every API request mocked.

Covers InfoWall d2627d2f: a Hub sub-agent (`claude-hub agent run`) attributed
to its parent workspace groups under the Workspace Agents panel instead of
the manual tab strip, and the parent Chat shows an Agents pill/popover that
focuses the live child or restores an archived child in one click.

Run only with a dedicated Vite server, never 5173/8173:
  python tests/browser_subagent_jump_card.py http://127.0.0.1:5291 /tmp/subagent-jump-ui [browser]
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

PARENT = "fixture-parent-tab"
LIVE = "fixture-live-child"
ARCH = "fixture-archived-child"
WS = "fixture-workspace"
NOW = "2026-11-03T10:00:00Z"
MOBILE = {"width": 390, "height": 844}
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


def subagent(sub_id, tab_id, *, status, closed, summary, created_at, close_reason=None):
    return {
        "id": sub_id,
        "tab_id": tab_id,
        "parent_tab_id": PARENT,
        "agent_type": "codex",
        "name": ("codex subagent: archived review" if closed else "codex subagent: analyze parser"),
        "keep": False,
        "closed": closed,
        "close_reason": close_reason,
        "created_at": created_at,
        "turn_count": 1,
        "last_turn_id": f"{sub_id}-turn",
        "last_status": status,
        "last_summary": summary,
    }


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
    unarchived = []
    arch_restored = asyncio.Event()
    # Flip once the phone has shown a running child, to prove history-only.
    all_done = asyncio.Event()

    live_sub = subagent(
        "sa-live0000001",
        LIVE,
        status="running",
        closed=False,
        summary=None,
        created_at="2026-11-03T10:05:00Z",
    )
    arch_sub = subagent(
        "sa-arch0000002",
        ARCH,
        status="completed",
        closed=True,
        summary="Found two failing edge cases.",
        created_at="2026-11-03T10:01:00Z",
        close_reason="completed",
    )

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
                rows = [
                    tab(PARENT, "Parent Chat"),
                    tab(
                        LIVE,
                        "codex subagent: analyze parser",
                        workspace_id=WS,
                        workspace_name="Repo",
                        parent_tab_id=PARENT,
                        subagent_id="sa-live0000001",
                    ),
                ]
                if arch_restored.is_set():
                    rows.append(
                        tab(
                            ARCH,
                            "codex subagent: archived review",
                            workspace_id=WS,
                            workspace_name="Repo",
                            parent_tab_id=PARENT,
                            subagent_id="sa-arch0000002",
                        )
                    )
                body = rows
            elif path == "/api/tabs/archived" and request.method == "GET":
                body = (
                    []
                    if arch_restored.is_set()
                    else [
                        tab(
                            ARCH,
                            "codex subagent: archived review",
                            archived=True,
                            archived_at=NOW,
                            workspace_id=WS,
                            workspace_name="Repo",
                            parent_tab_id=PARENT,
                            subagent_id="sa-arch0000002",
                        )
                    ]
                )
            elif path == "/api/tabs/status" and request.method == "GET":
                body = [
                    {
                        "tab_id": PARENT,
                        "status": "idle",
                        "status_text": "Idle",
                        "detail": "",
                    },
                    {
                        "tab_id": LIVE,
                        "status": "working",
                        "status_text": "Working",
                        "detail": "",
                    },
                ]
            elif path == "/api/subagents" and request.method == "GET":
                query = parse_qs(parsed.query)
                assert query.get("include_closed") == ["true"]
                parent_id = query.get("parent_tab_id", [None])[0]
                finished_live = dict(
                    live_sub,
                    closed=True,
                    close_reason="completed",
                    last_status="completed",
                    last_summary="Finished the parser review.",
                )
                live = finished_live if all_done.is_set() else live_sub
                body = [live, arch_sub] if parent_id == PARENT else []
            elif path == f"/api/tabs/{ARCH}/unarchive" and request.method == "POST":
                unarchived.append(path)
                arch_restored.set()
                body = {"ok": True}
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
                for marker in (PARENT, LIVE, ARCH):
                    if f"/tabs/{marker}/" in path:
                        event_tabs.append(marker)
                body = {
                    "events": [],
                    "next_sequence": 0,
                    "has_more": False,
                    "history_details": [],
                }
            elif path.endswith("/wait"):
                body = {"events": [], "next_sequence": 0, "has_more": False}
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
                # Switching panes aborts the old tab's in-flight requests; the
                # route is already finalized and cannot be fulfilled.
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

        # Desktop: the running child shows in the pane-chrome Agents pill; the
        # old full-width list above the composer is gone.
        pill = page.get_by_test_id("subagent-dock-pill")
        await expect(pill).to_be_visible()
        await expect(pill).to_contain_text("1 running")
        await expect(page.get_by_test_id("subagent-jump-card")).to_have_count(0)
        await expect(page.get_by_test_id("subagent-popover")).to_have_count(0)
        await pill.click()
        popover = page.get_by_test_id("subagent-popover")
        await expect(popover).to_be_visible()

        live_row = popover.get_by_test_id("subagent-row-sa-live0000001")
        arch_row = popover.get_by_test_id("subagent-row-sa-arch0000002")
        await expect(live_row).to_have_attribute("data-state", "running")
        await expect(live_row).to_contain_text("analyze parser")
        # History is collapsed while something is running.
        await expect(arch_row).to_be_hidden()
        await popover.get_by_test_id("subagent-earlier-toggle").click()
        await expect(arch_row).to_have_attribute("data-state", "closed")
        await expect(arch_row).to_contain_text("archived review")
        await expect(arch_row).to_contain_text("Found two failing edge cases.")
        await expect(arch_row).to_contain_text("Restore & open")
        await page.screenshot(path=str(output / "00-dock-popover.png"))
        await page.keyboard.press("Escape")
        await expect(popover).to_have_count(0)

        # The attributed child Chat is not a manual terminal-strip tab.
        await expect(page.locator(".tab-bar .tab")).to_have_count(1)
        await expect(page.locator(".tab-bar .tab").first).to_have_attribute("data-tab-id", PARENT)

        # Workspace Agents floating panel groups it under "Repo" with a sub tag.
        agents_trigger = page.locator(".status-trigger", has_text="Agents").first
        await agents_trigger.click()
        managed_panel = page.locator(".status-panel", has_text="Workspace Agents")
        await expect(managed_panel).to_be_visible()
        await expect(
            managed_panel.locator(".agent-group-header span", has_text="Repo")
        ).to_have_count(1)
        managed_row = managed_panel.locator(".agent-row", has_text="analyze parser")
        await expect(managed_row).to_have_count(1)
        await expect(managed_row.locator(".agent-sub-tag")).to_have_text("sub")
        await page.screenshot(path=str(output / "01-workspace-grouped.png"))
        await agents_trigger.click()
        await expect(managed_panel).to_be_hidden()

        # One click focuses the live child tab (its Chat pane hydrates).
        assert LIVE not in event_tabs
        await pill.click()
        await live_row.click()
        await wait_until(lambda: LIVE in event_tabs)

        # Back on the parent Chat, the archived child restores then focuses.
        await page.locator(".tab-bar .tab", has_text="Parent Chat").click()
        await expect(pill).to_be_visible()
        assert not unarchived
        await pill.click()
        await expect(arch_row).to_be_hidden()
        await popover.get_by_test_id("subagent-earlier-toggle").click()
        await arch_row.click()
        await wait_until(lambda: bool(unarchived))
        await wait_until(lambda: ARCH in event_tabs)
        await page.screenshot(path=str(output / "02-archived-restored.png"))

        # Mobile: the same Agents pill keeps its place in the pane chrome, next
        # to the session-name pill, instead of adding a bar above the composer.
        await page.locator(".tab-bar .tab", has_text="Parent Chat").click()
        await page.set_viewport_size(MOBILE)
        await expect(pill).to_be_visible()
        # One control for one pane: no second sub-agent control anywhere.
        await expect(page.locator("[data-testid=subagent-dock-pill]")).to_have_count(1)
        name_pill = page.locator(".pane-session-name").first
        await expect(name_pill).to_be_visible()
        chrome_box = await page.locator(".pane-chrome").first.bounding_box()
        pill_box = await pill.bounding_box()
        name_box = await name_pill.bounding_box()
        assert chrome_box and pill_box and name_box, "chrome cluster must be laid out"
        assert pill_box["y"] < chrome_box["y"] + chrome_box["height"], (chrome_box, pill_box)
        assert (
            abs((pill_box["y"] + pill_box["height"] / 2) - (name_box["y"] + name_box["height"] / 2))
            < 2
        ), (
            "the Agents pill must sit on the session-name pill's row",
            pill_box,
            name_box,
        )
        assert pill_box["x"] < name_box["x"], "active count stays left of the session name"
        assert name_box["x"] + name_box["width"] <= 390, name_box
        await page.screenshot(path=str(output / "03-mobile-chrome-pill.png"))

        await expect(page.get_by_test_id("subagent-sheet")).to_have_count(0)
        await pill.click()
        sheet = page.get_by_test_id("subagent-sheet")
        await expect(sheet).to_be_visible()
        await expect(page.get_by_test_id("subagent-popover")).to_have_count(0)
        await expect(sheet.get_by_test_id("subagent-row-sa-live0000001")).to_have_attribute(
            "data-state", "running"
        )
        # Modal hygiene: the body lock follows the other phone overlays, and a
        # full Tab cycle cannot walk out of the sheet (hidden Earlier rows are
        # not trap edges, so this must hold in the default running state too).
        assert await page.evaluate("document.body.style.overflow") == "hidden"
        for _ in range(6):
            await page.keyboard.press("Tab")
            assert await page.evaluate(
                "Boolean(document.activeElement && document.activeElement.closest("
                "'[data-testid=subagent-sheet]'))"
            ), "Tab must stay inside the sheet"
        await page.keyboard.press("Shift+Tab")
        assert await page.evaluate(
            "Boolean(document.activeElement && document.activeElement.closest("
            "'[data-testid=subagent-sheet]'))"
        ), "Shift+Tab must stay inside the sheet"
        await page.screenshot(path=str(output / "04-mobile-sheet.png"))
        await sheet.get_by_test_id("subagent-earlier-toggle").click()
        await expect(sheet.get_by_test_id("subagent-row-sa-arch0000002")).to_be_visible()
        await page.keyboard.press("Escape")
        await expect(page.get_by_test_id("subagent-sheet")).to_have_count(0)

        # Done and a backdrop tap close it too, and focus returns to the pill.
        await pill.click()
        await expect(sheet).to_be_visible()
        await sheet.get_by_role("button", name="Close sub-agents").click()
        await expect(page.get_by_test_id("subagent-sheet")).to_have_count(0)
        await expect(pill).to_be_focused()
        await pill.click()
        await expect(sheet).to_be_visible()
        await page.get_by_test_id("subagent-sheet-backdrop").click(position={"x": 12, "y": 120})
        await expect(page.get_by_test_id("subagent-sheet")).to_have_count(0)
        assert (
            await page.evaluate("document.body.style.overflow") == ""
        ), "scroll lock must be released"

        # Esc also closes after a tap moved focus out of the sheet's subtree.
        await pill.click()
        await expect(sheet).to_be_visible()
        await sheet.locator(".subagent-sheet-head strong").click()
        assert (
            await page.evaluate(
                "Boolean(document.activeElement && document.activeElement.closest("
                "'[data-testid=subagent-sheet]'))"
            )
        ) is False, "the tap must have moved focus out of the sheet"
        await page.keyboard.press("Escape")
        await expect(page.get_by_test_id("subagent-sheet")).to_have_count(0)

        # A breakpoint change dismisses the reveal instead of morphing it.
        await pill.click()
        await expect(sheet).to_be_visible()
        await page.set_viewport_size(DESKTOP)
        await expect(page.get_by_test_id("subagent-sheet")).to_have_count(0)
        await expect(page.get_by_test_id("subagent-popover")).to_have_count(0)

        # History-only: back on a phone the same pill reads "2 done" and still
        # opens the sheet, so a finished sub-agent has an entry point here.
        await page.set_viewport_size(MOBILE)
        all_done.set()
        await expect(pill).to_contain_text("2 done", timeout=15000)
        await pill.click()
        await expect(sheet).to_be_visible()
        await expect(sheet.get_by_test_id("subagent-row-sa-live0000001")).to_have_attribute(
            "data-state", "closed"
        )
        await expect(sheet.get_by_test_id("subagent-row-sa-arch0000002")).to_be_visible()
        await page.screenshot(path=str(output / "05-mobile-history-sheet.png"))
        await page.keyboard.press("Escape")
        await expect(page.get_by_test_id("subagent-sheet")).to_have_count(0)
        await page.set_viewport_size(DESKTOP)

        # Let the 5s polls run once so unexpected responses surface as errors.
        await page.wait_for_timeout(1200)
        assert not unknown_requests, unknown_requests
        assert not unexpected_external_requests, unexpected_external_requests
        assert not errors, errors

    timings = {"elapsed_ms": round((time.perf_counter() - started) * 1000)}
    report = {
        "result": "PASS",
        "coverage": [
            "desktop Agents pill + popover: Active first, collapsible Earlier, Esc closes",
            "workspace-attributed child excluded from manual tab strip",
            "child grouped under workspace in Workspace Agents panel with sub marker",
            "one-click jump focuses live child Chat",
            "archived child click restores (unarchive) then focuses it",
            "mobile keeps the pill in the pane chrome (no composer bar); sheet closes via Esc"
            " (focused or not), Done, backdrop tap and a breakpoint change; history-only"
            ' reads "2 done" and still opens the sheet',
        ],
        "unarchive_requests": unarchived,
        "event_tabs": event_tabs,
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
