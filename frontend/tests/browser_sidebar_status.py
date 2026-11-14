"""Exercise the session status light where a blocked question meets the sidebar.

A provider that holds its turn open on a question keeps the status poll
reporting `working`. The session is not working, though: nothing moves until
the user answers. This scenario pins that presentation in the real SPA — the
sidebar row and the TabBar indicator both read the finished treatment and say
`Waiting for your answer`, while a genuinely running turn keeps its own light
and its working pulse — at desktop and narrow widths.

Run only with a task-owned Vite server, never 5173/8173:
  python tests/browser_sidebar_status.py <owned-loopback-url> <new-output-dir> <browser>

All APIs are mocked. No Hub backend or provider is contacted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import ViewportSize, expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from appearance_assertions import GAME_PALETTES, select_appearance  # noqa: E402
from browser_lifecycle import BrowserRun, run_browser_main  # noqa: E402

DESKTOP: ViewportSize = {"width": 1440, "height": 900}
NARROW: ViewportSize = {"width": 820, "height": 800}
# The drawer chrome only exists at or below useViewport's 768px breakpoint.
PHONE: ViewportSize = {"width": 390, "height": 844}
NOW = "2026-11-11T15:30:00Z"

# The reported payload: a running turn that is really blocked on a card.
BLOCKED = "sidebar-blocked"
# Its control: the same poll without a question is ordinary work.
WORKING = "sidebar-working"
# A finished turn the user has not read keeps the completion treatment.
DONE = "sidebar-done"
# A provider that failed still owns its row.
FAILED = "sidebar-failed"
# A failed provider that still carries a live card: the row must name both facts,
# because an answer can be refused in that state.
FAILED_WAITING = "sidebar-failed-waiting"


def tab(tab_id: str, name: str, **overrides: object) -> dict:
    body = {
        "id": tab_id,
        "name": name,
        "cwd": "/fixture/sidebar",
        "agent_type": "cursor",
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
    page_errors: list[str] = []
    report: dict = {
        "result": "FAIL",
        "api_requests": requests,
        "unknown_requests": unknown_requests,
        "page_errors": page_errors,
        "screenshots": [],
        "boundary": "Real SPA rendering with mocked APIs; no backend or provider traffic.",
    }
    failure: BaseException | None = None
    page = None

    async def shot(name: str) -> None:
        assert page is not None
        await page.screenshot(path=str(output / name))
        report["screenshots"].append(name)

    async def expect_row_presentation(
        tab_id: str, expected_state: str, label: str, pulsing: bool
    ) -> None:
        """Assert one row's status light: state, spoken label, and pulse."""
        assert page is not None
        light = page.locator(
            f'.chat-sidebar__item[data-tab-id="{tab_id}"] .chat-sidebar__item-status'
        )
        await expect(light).to_have_attribute("data-status", expected_state)
        await expect(light).to_have_attribute("aria-label", label)
        # The tooltip repeats the same sentence, so the two cannot disagree.
        await expect(light).to_have_attribute("title", label)
        effects = page.locator(f'.chat-sidebar__item[data-tab-id="{tab_id}"] .working-effect')
        await expect(effects).to_have_count(1 if pulsing else 0)

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
                    # Unread on purpose: an outstanding question is not an
                    # unread result, so the row must not wear the completion
                    # badge while it waits.
                    tab(BLOCKED, "mr review, waiting on me", is_unread=True),
                    tab(WORKING, "long streaming turn"),
                    tab(DONE, "finished review", is_unread=True),
                    tab(FAILED, "provider error"),
                    tab(FAILED_WAITING, "provider error, question waiting"),
                ]
            elif path == "/api/tabs/archived":
                body = []
            elif path == "/api/tabs/status":
                body = [
                    {
                        "tab_id": BLOCKED,
                        # The provider never finished its turn: the poll keeps
                        # saying "working" while the card waits for an answer.
                        "status": "working",
                        "status_text": "Working",
                        "detail": "Responding",
                        "pending_question": True,
                        "activity": "Asking you",
                        "activity_since": NOW,
                        "last_reply_excerpt": "Reply to all four comments?",
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
                        "tab_id": FAILED,
                        "status": "attention",
                        "status_text": "Needs attention",
                        "detail": "provider exited unexpectedly",
                        # No live card here: a failed provider with nothing to
                        # answer keeps its own state and label.
                        "pending_question": False,
                        "activity": None,
                        "activity_since": None,
                        "last_reply_excerpt": None,
                        "last_reply_at": NOW,
                    },
                    {
                        "tab_id": FAILED_WAITING,
                        "status": "attention",
                        "status_text": "Needs attention",
                        "detail": "provider exited unexpectedly",
                        "pending_question": True,
                        "activity": None,
                        "activity_since": None,
                        "last_reply_excerpt": None,
                        "last_reply_at": NOW,
                    },
                    {
                        "tab_id": DONE,
                        "status": "idle",
                        "status_text": "Idle",
                        "detail": "Ready",
                        "pending_question": False,
                        "activity": None,
                        "activity_since": None,
                        "last_reply_excerpt": "All 12 tests pass now.",
                        "last_reply_at": NOW,
                    },
                ]
            elif path.startswith("/api/tabs/sidebar-preferences"):
                body = {"pinned_tab_ids": []}
            elif path.startswith("/api/workspaces/tabs/") and path.endswith("/messages"):
                body = {"messages": []}
            elif path == "/api/subagents":
                body = []
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
                    "agent_type": "cursor",
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
                        status=200,
                        content_type="text/event-stream",
                        body=": fixture\n\n",
                    )
                except Exception:
                    pass
                return
            elif path.endswith("/goal/current"):
                body = None
            elif path.endswith("/view"):
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
            sidebar = page.locator(".chat-sidebar").first
            await expect(sidebar).to_be_visible()

            # Desktop: the blocked question reads as finished work waiting on
            # the user, never as the pulse a running turn carries.
            await expect_row_presentation(
                BLOCKED,
                "idle",
                "Session status: Waiting for your answer",
                pulsing=False,
            )
            await expect_row_presentation(
                WORKING, "working", "Session status: Working", pulsing=True
            )
            # An unread finished turn is unchanged by the mapping above.
            await expect(
                page.locator(
                    f'.chat-sidebar__item[data-tab-id="{DONE}"] .chat-sidebar__item-status'
                )
            ).to_have_attribute("aria-label", "Completed, unread")
            await expect(
                page.locator(
                    f'.chat-sidebar__item[data-tab-id="{DONE}"] .chat-sidebar__item-status'
                )
            ).to_have_attribute("data-status", "idle")
            await expect(
                page.locator(
                    f'.chat-sidebar__item[data-tab-id="{DONE}"] .chat-sidebar__item-status'
                )
            ).to_have_attribute("data-unread", "true")
            # The blocked row is unread in the fixture, but answering is the
            # action there: it must not wear the unread completion badge.
            await expect(
                page.locator(
                    f'.chat-sidebar__item[data-tab-id="{BLOCKED}"] .chat-sidebar__item-status'
                )
            ).to_have_attribute("data-unread", "false")
            # A provider that failed still owns the row: no card on this one,
            # so the failure reads exactly as before this change.
            await expect_row_presentation(
                FAILED, "attention", "Session status: Needs attention", pulsing=False
            )
            # Both facts survive: the failure keeps its state and the label says
            # a question is waiting, without the completed/awaiting marker.
            await expect_row_presentation(
                FAILED_WAITING,
                "attention",
                "Session status: Needs attention — a question is waiting",
                pulsing=False,
            )
            await expect(
                page.locator(
                    f'.chat-sidebar__item[data-tab-id="{FAILED_WAITING}"] .chat-sidebar__item-status'
                )
            ).to_have_attribute("data-awaiting", "false")
            # The row itself must not claim a running session either: the
            # sidebar's item-level attribute drives the working border effect.
            await expect(
                page.locator(f'.chat-sidebar__item[data-tab-id="{BLOCKED}"]')
            ).to_have_attribute("data-runtime-status", "idle")
            await expect(
                page.locator(f'.chat-sidebar__item[data-tab-id="{WORKING}"]')
            ).to_have_attribute("data-runtime-status", "working")
            # The TabBar shares the same composable, so both lights agree.
            tabbar_indicator = page.locator(
                f'.tab-bar .tab[data-tab-id="{BLOCKED}"] .tab-indicator'
            )
            await expect(tabbar_indicator).to_have_attribute("data-status", "idle")
            await expect(tabbar_indicator).to_have_attribute(
                "aria-label", "Waiting for your answer"
            )
            await shot("00-desktop-status.png")

            # Compare two real active tabs, not a hand-edited data attribute.
            # A game skin must retain the waiting cue as well as its active ring.
            report["game_waiting_indicators"] = []
            for mode in ("dark", "light"):
                for palette in GAME_PALETTES:
                    await select_appearance(page, mode, palette)
                    paints = {}
                    for tab_id, waiting in ((BLOCKED, True), (DONE, False)):
                        await page.locator(
                            f'.chat-sidebar__item[data-tab-id="{tab_id}"] .chat-sidebar__item-main'
                        ).click()
                        active = page.locator(f'.tab-bar .tab.active[data-tab-id="{tab_id}"]')
                        indicator = active.locator(".tab-indicator")
                        await expect(indicator).to_have_attribute("data-status", "idle")
                        await expect(indicator).to_have_attribute(
                            "data-awaiting", str(waiting).lower()
                        )
                        await indicator.evaluate("""async el => {
                          getComputedStyle(el).boxShadow;
                          await Promise.all(el.getAnimations().filter(a =>
                            Number.isFinite(a.effect?.getComputedTiming().endTime)).map(a => a.finished));
                        }""")
                        paints[str(waiting)] = await indicator.evaluate(
                            """el => ({
                          shadow:getComputedStyle(el).boxShadow,
                          color:getComputedStyle(el).backgroundColor,
                          width:el.getBoundingClientRect().width,height:el.getBoundingClientRect().height})"""
                        )
                        await shot(
                            f"status-{palette.lower().replace(' ', '-')}-{mode}-{waiting}.png"
                        )
                    assert paints["True"]["shadow"] != paints["False"]["shadow"], (
                        palette,
                        mode,
                        paints,
                    )
                    assert paints["True"]["shadow"] != "none", (palette, mode, paints)
                    assert paints["True"]["color"] == paints["False"]["color"], (
                        palette,
                        mode,
                        paints,
                    )
                    assert all(
                        paints["True"][key] == paints["False"][key] == 7
                        for key in ("width", "height")
                    )
                    report["game_waiting_indicators"].append(
                        {"palette": palette, "mode": mode, **paints}
                    )
            await select_appearance(page, "dark", "Classic")
            await page.locator(
                f'.chat-sidebar__item[data-tab-id="{BLOCKED}"] .chat-sidebar__item-main'
            ).click()

            # Narrow pane: the sentence must still be reachable and the 8px
            # light must not grow or displace the row's name.
            await page.set_viewport_size(NARROW)
            await expect(sidebar).to_be_visible()
            await expect_row_presentation(
                BLOCKED,
                "idle",
                "Session status: Waiting for your answer",
                pulsing=False,
            )
            await expect_row_presentation(
                WORKING, "working", "Session status: Working", pulsing=True
            )
            light_box = await page.locator(
                f'.chat-sidebar__item[data-tab-id="{BLOCKED}"] .chat-sidebar__item-status'
            ).bounding_box()
            sidebar_box = await sidebar.bounding_box()
            assert light_box and sidebar_box, "the row light is laid out"
            assert light_box["width"] == 8 and light_box["height"] == 8, light_box
            assert (
                sidebar_box["x"] <= light_box["x"] <= sidebar_box["x"] + sidebar_box["width"]
            ), f"the light left the sidebar: light={light_box} sidebar={sidebar_box}"
            name_box = await page.locator(
                f'.chat-sidebar__item[data-tab-id="{BLOCKED}"] .chat-sidebar__item-name'
            ).bounding_box()
            assert name_box, "the row keeps its name"
            assert (
                name_box["x"] >= light_box["x"] + light_box["width"]
            ), f"the light overlaps the name: light={light_box} name={name_box}"
            await shot("01-narrow-status.png")

            # Phone: the sidebar is a closed drawer, so the state a thumb can
            # reach is the TabBar's. It must say the same thing as the row. A
            # long tab name can push the light past the visible strip (a tab-bar
            # layout limit, not this state), so the contract asserted here is
            # the indicator's own state rather than its pixel visibility.
            await page.set_viewport_size(PHONE)
            # The drawer animates closed; shoot the settled page, not a slide.
            await page.wait_for_timeout(400)
            tabbar_indicator = page.locator(
                f'.tab-bar .tab[data-tab-id="{BLOCKED}"] .tab-indicator'
            )
            await expect(tabbar_indicator).to_have_count(1)
            await expect(tabbar_indicator).to_have_attribute("data-status", "idle")
            await expect(tabbar_indicator).to_have_attribute("data-awaiting", "true")
            await expect(tabbar_indicator).to_have_attribute(
                "aria-label", "Waiting for your answer"
            )
            await expect(tabbar_indicator).to_have_attribute("title", "Waiting for your answer")
            working_indicator = page.locator(
                f'.tab-bar .tab[data-tab-id="{WORKING}"] .tab-indicator'
            )
            await expect(working_indicator).to_have_attribute("data-status", "working")
            # The TabBar names the state itself; only the sidebar's row adds
            # the "Session status:" prefix for its longer spoken label.
            await expect(working_indicator).to_have_attribute("aria-label", "Working")
            await shot("02-phone-tab-indicator.png")

            # Open the mobile drawer while an active Chat pane makes .app
            # position:fixed. The drawer must sit above .chat-drawer-backdrop
            # (un-dimmed, pointer-reachable, and vertically scrollable).
            await select_appearance(page, "light", "Zenless")
            await page.locator(".mobile-app-menu-trigger").click()
            await page.locator(".mobile-app-menu-item", has_text="Chats").click()
            await expect(
                page.locator(".chat-sidebar.mobile-drawer.mobile-drawer--open")
            ).to_be_visible()
            await expect(page.locator(".chat-drawer-backdrop")).to_be_visible()
            await page.wait_for_timeout(220)
            drawer_stack = await page.evaluate("""() => {
              const drawer = document.querySelector('.chat-sidebar.mobile-drawer.mobile-drawer--open');
              const body = document.querySelector('.chat-sidebar__body');
              const backdrop = document.querySelector('.chat-drawer-backdrop');
              if (!drawer || !body || !backdrop) return { missing: true };
              const rect = drawer.getBoundingClientRect();
              const centerHit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
              const outsideHit = document.elementFromPoint(rect.right + 24, rect.top + rect.height / 2);
              const spacer = document.createElement('div');
              spacer.style.height = '1200px';
              body.appendChild(spacer);
              body.scrollTop = 180;
              const scrolled = body.scrollTop;
              body.scrollTop = 0;
              spacer.remove();
              const originalParent = backdrop.parentNode;
              const originalNext = backdrop.nextSibling;
              document.body.appendChild(backdrop);
              const teleportedBugHit = document.elementFromPoint(
                rect.left + rect.width / 2,
                rect.top + rect.height / 2,
              ) === backdrop;
              originalParent.insertBefore(backdrop, originalNext);
              return {
                drawerHitInside: Boolean(centerHit && drawer.contains(centerHit)),
                centerHitClass: centerHit?.className ?? null,
                outsideHitsBackdrop: outsideHit === backdrop,
                teleportedBugHit,
                scrolled,
              };
            }""")
            assert drawer_stack.get("drawerHitInside") is True, drawer_stack
            assert drawer_stack.get("outsideHitsBackdrop") is True, drawer_stack
            assert drawer_stack.get("teleportedBugHit") is True, drawer_stack
            assert (drawer_stack.get("scrolled") or 0) >= 150, drawer_stack
            await page.locator(
                f'.chat-sidebar__item[data-tab-id="{WORKING}"] .chat-sidebar__item-main'
            ).click()
            await page.wait_for_function(
                "document.querySelector('.chat-sidebar').getBoundingClientRect().right <= 1"
            )

            await page.wait_for_timeout(800)
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
