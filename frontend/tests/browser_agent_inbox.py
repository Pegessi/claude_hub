"""Exercise the top-bar Needs-you inbox in the real SPA with every API mocked.

The inbox replaced the permanent `Status N` / `Agents N` pills, and its whole
point is what it does with the keyboard: a container-level keydown must not
swallow what the panel's own controls do natively. This scenario pins that
contract — the filter segment, the row open, the way out, and the phone sheet.

Run only with a task-owned Vite server, never 5173/8173:
  python tests/browser_agent_inbox.py <owned-loopback-url> <new-output-dir> <browser>

All APIs are mocked. No Hub backend or provider is contacted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import ViewportSize, expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_lifecycle import BrowserRun, run_browser_main  # noqa: E402

DESKTOP: ViewportSize = {"width": 1440, "height": 900}
MOBILE: ViewportSize = {"width": 390, "height": 844}
NOW = "2026-11-09T10:00:00Z"

# Two manual Chats: one finished and unread (needs you), one still running.
NEEDS = "inbox-needs"
WORKING = "inbox-working"
# A workspace-attributed Chat, so the Agents filter is not empty either.
AGENT = "inbox-agent"
WS = "fixture-workspace"


def tab(tab_id: str, name: str, **overrides: object) -> dict:
    body = {
        "id": tab_id,
        "name": name,
        "cwd": "/fixture/inbox",
        "agent_type": "claude",
        "session_kind": "chat",
        "port": 0,
        "is_active": False,
        "created_at": NOW,
        "archived": False,
        "last_viewed_at": None,
        "is_unread": False,
    }
    body.update(overrides)
    return body


async def main(browser_run: BrowserRun) -> None:
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    base = sys.argv[1].rstrip("/")
    parsed_base = urlparse(base)
    if (
        parsed_base.scheme != "http"
        or parsed_base.hostname not in {"127.0.0.1", "::1"}
        or parsed_base.port in {None, 5173, 8173, 10025, 18183}
        or parsed_base.username is not None
        or parsed_base.password is not None
        or parsed_base.path not in {"", "/"}
        or parsed_base.query
        or parsed_base.fragment
    ):
        raise SystemExit("Use an owned numeric-loopback review server")
    output = Path(sys.argv[2]).resolve()

    requests: list[tuple[str, str]] = []
    unknown_requests: list[tuple[str, str]] = []
    status_reads: list[str] = []
    page_errors: list[str] = []
    viewed: list[str] = []
    report: dict = {
        "result": "FAIL",
        "api_requests": requests,
        "unknown_requests": unknown_requests,
        "page_errors": page_errors,
        "viewed_tabs": viewed,
        "screenshots": [],
        "boundary": "Real SPA rendering with mocked APIs; no backend or provider traffic.",
    }
    failure: BaseException | None = None
    page = None

    def writes(method: str, suffix: str) -> list[str]:
        return [path for written, path in requests if written == method and path.endswith(suffix)]

    async def shot(name: str) -> None:
        assert page is not None
        await page.screenshot(path=str(output / name))
        report["screenshots"].append(name)

    async with browser_run.context(viewport=DESKTOP) as context:

        async def block_websocket(socket) -> None:
            await socket.close(code=1000, reason="mocked UI smoke")

        async def api(route) -> None:
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
                    tab(NEEDS, "unread review", is_unread=True),
                    tab(WORKING, "streaming turn"),
                    tab(
                        AGENT,
                        "workspace agent",
                        workspace_id=WS,
                        workspace_name="Repo",
                    ),
                ]
            elif path == "/api/tabs/archived":
                body = []
            elif path == "/api/tabs/status":
                status_reads.append(path)
                body = [
                    {
                        "tab_id": NEEDS,
                        "status": "idle",
                        "status_text": "Idle",
                        "detail": "Ready",
                        "pending_question": False,
                        "activity": None,
                        "activity_since": None,
                        "last_reply_excerpt": "All 12 tests pass now.",
                        "last_reply_at": NOW,
                    },
                    {
                        "tab_id": WORKING,
                        "status": "working",
                        "status_text": "Working",
                        "detail": "Responding",
                        "pending_question": False,
                        "activity": 'Running "pytest -q"',
                        "activity_since": NOW,
                        "last_reply_excerpt": None,
                        "last_reply_at": None,
                    },
                    {
                        "tab_id": AGENT,
                        "status": "idle",
                        "status_text": "Idle",
                        "detail": "Ready",
                        "pending_question": True,
                        "activity": "Asking you",
                        "activity_since": NOW,
                        "last_reply_excerpt": "Which branch should I rebase?",
                        "last_reply_at": None,
                    },
                ]
            elif path.startswith("/api/workspaces/tabs/") and path.endswith("/messages"):
                body = {"messages": []}
            elif path == "/api/subagents":
                body = []
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
                    "agent_type": "claude",
                    "supports_mode_switch": False,
                }
            elif path.endswith("/stream/events"):
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
                        status=200, content_type="text/event-stream", body=": fixture\n\n"
                    )
                except Exception:
                    pass
                return
            elif path.endswith("/goal/current"):
                body = None
            elif path.endswith("/view"):
                viewed.append(path)
                body = {"ok": True}
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
            except Exception:
                # A pane switch aborts the previous tab's in-flight reads.
                try:
                    await route.abort()
                except Exception:
                    pass

        await context.route_web_socket("**/*", block_websocket)
        await context.route("**/*", api)
        page = await context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        try:
            await page.goto(base + "/")
            trigger = page.locator(".inbox-trigger").first
            await expect(trigger).to_be_visible()
            # The badge counts needs-you rows only: one unread turn plus one
            # blocked question; the streaming turn must not inflate it.
            await expect(trigger.locator(".inbox-trigger-badge")).to_have_text("2")
            await expect(trigger.locator(".inbox-trigger-label")).to_have_text("Needs you")
            await shot("00-trigger.png")

            await trigger.click()
            panel = page.locator(".inbox-panel")
            await expect(panel).to_be_visible()
            await expect(panel.locator('.inbox-group[data-group="needs"]')).to_contain_text(
                "Waiting for your answer"
            )
            await expect(panel.locator('.inbox-group[data-group="working"]')).to_contain_text(
                'Running "pytest -q"'
            )
            # Finished history is collapsed until asked for.
            await expect(panel.locator('.inbox-group[data-group="done"] .inbox-row')).to_have_count(
                0
            )
            await shot("01-panel.png")

            # A filter segment is a button: Enter must activate the filter and
            # must NOT navigate to the cursor row.
            scope_chats = panel.locator(".inbox-scope button", has_text="Chats").first
            await scope_chats.focus()
            active_before = await page.locator(".tab-bar .tab.active").get_attribute("data-tab-id")
            viewed_before = len(viewed)
            await page.keyboard.press("Enter")
            await expect(scope_chats).to_have_attribute("data-active", "true")
            await expect(panel.locator(".inbox-row")).to_have_count(2)
            assert (
                await page.locator(".tab-bar .tab.active").get_attribute("data-tab-id")
                == active_before
            ), "Enter on a filter segment must not open the cursor row"
            assert (
                len(viewed) == viewed_before
            ), f"Enter on a filter segment must not mark a tab viewed: {viewed}"
            await shot("02-filter-enter.png")

            # The same key on the panel itself does open the cursor row. A
            # filter change keeps the cursor on a row that is still visible, so
            # it sits on the unread Chat here.
            await panel.locator(".inbox-scope button", has_text="All").first.click()
            await expect(panel.locator(".inbox-row.is-cursor")).to_have_attribute(
                "data-row-tab-id", NEEDS
            )
            await page.locator(".inbox-content").focus()
            await page.keyboard.press("Enter")
            await expect(panel).to_have_count(0)
            await expect(page.locator("[data-testid=inbox-sheet]")).to_have_count(0)
            assert (
                f"/api/tabs/{NEEDS}/view" in viewed
            ), f"Enter on the panel must open the cursor row: {viewed}"

            # Escape closes the panel from anywhere inside it, including a field.
            await trigger.click()
            await expect(panel).to_be_visible()
            # Expand the unread (not blocked) Chat: only that row offers the
            # inline reply, and Escape must still get out of the field.
            await panel.locator('.inbox-group[data-group="needs"] .inbox-row-expand').nth(1).click()
            reply = panel.locator(".inbox-reply-input").first
            await expect(reply).to_be_visible()
            await reply.fill("keep going")
            await page.keyboard.press("Escape")
            await expect(panel).to_have_count(0)

            # A reload never re-opens a modal surface by itself.
            await page.reload()
            await expect(page.locator(".inbox-panel")).to_have_count(0)
            await expect(page.locator("[data-testid=inbox-sheet]")).to_have_count(0)

            # Phone: the same entry opens a full-height sheet, never a popover.
            await page.set_viewport_size(MOBILE)
            trigger = page.locator(".inbox-trigger").first
            await expect(trigger).to_be_visible()
            # The label costs a third of the top bar on a phone, so the entry is
            # its dot and count; the name survives as aria-label and as a hover
            # tip for a narrow window that does have a pointer.
            await expect(trigger.locator(".inbox-trigger-label")).to_have_count(0)
            assert await trigger.get_attribute("aria-label") == "Needs you, 2 waiting"
            compact = await trigger.bounding_box()
            assert compact and compact["width"] <= 60, f"compact entry is {compact}"
            await trigger.hover()
            tip = trigger.locator(".inbox-trigger-tip")
            await expect(tip).to_be_visible()
            await expect(tip).to_have_text("Needs you")
            await page.mouse.move(4, 400)
            await expect(tip).to_be_hidden()
            # Opening on the phone refreshes instead of waiting for the poll.
            reads_before_open = len(status_reads)
            await trigger.click()
            sheet = page.locator("[data-testid=inbox-sheet]")
            await expect(sheet).to_be_visible()
            # Tight window: the 5 s background poll must not be what satisfies
            # this assertion, only the open-triggered refresh.
            for _ in range(8):
                if len(status_reads) > reads_before_open:
                    break
                await page.wait_for_timeout(80)
            assert len(status_reads) > reads_before_open, "phone open refreshes the inbox"
            await expect(page.locator(".inbox-panel")).to_have_count(0)
            await expect(sheet.locator(".inbox-row")).to_have_count(3)
            # Focus lands on the list container that owns the keyboard contract,
            # not on the Done button (which would disable ↑/↓/Enter/Esc).
            focused_class = await page.evaluate(
                "() => (document.activeElement && document.activeElement.className) || ''"
            )
            assert "inbox-content" in str(focused_class), f"focus went to {focused_class!r}"
            await shot("03-mobile-sheet.png")
            assert (
                await sheet.get_by_role("button", name="Done").count() == 1
            ), "phone sheet keeps its Done control"
            await page.keyboard.press("Escape")
            await expect(sheet).to_have_count(0)

            await page.wait_for_timeout(1200)
            assert not unknown_requests, unknown_requests
            assert not page_errors, page_errors
            report["result"] = "PASS"
        except BaseException as exc:
            failure = exc
            report["error_type"] = type(exc).__name__
            if page is not None:
                try:
                    await page.screenshot(path=str(output / "failure.png"), full_page=True)
                except Exception:
                    pass

    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    if failure is not None:
        raise failure


if __name__ == "__main__":
    run_browser_main(main)
