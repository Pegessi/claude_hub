"""Check the Goal controls on a phone in the real rendered SPA.

Run only with a task-owned Vite server, never 5173/8173:
  python tests/browser_chat_goal_mobile.py <owned-loopback-url> <new-output-dir> <browser>

All APIs are mocked. No Hub backend or provider is contacted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_lifecycle import BrowserRun, run_browser_main  # noqa: E402

TAB = "goal-mobile-tab"
NOW = "2026-10-12T08:00:00Z"
PHONE = {"width": 390, "height": 664}
# Long enough to fill a phone screen several times over.
OBJECTIVE = "\n".join(
    f"{index}. Rebuild the evaluation report section {index} and verify every number "
    "against the source table before moving on."
    for index in range(1, 41)
)
GOAL = {
    "id": "goal-mobile",
    "tab_id": TAB,
    "objective": OBJECTIVE,
    "status": "active",
    "token_usage": None,
    "usage_quality": "unavailable",
    "turns_completed": 3,
    "dispatch_state": "dispatched",
    "pending_step_id": None,
    "current_turn_id": None,
    "completed_turn_ids": [],
    "created_at": NOW,
    "updated_at": NOW,
    "checkpoint": {
        "next_step": "Check section 12 against the source table.",
        "blocker": None,
        "remaining": [f"section {index}" for index in range(12, 41)],
        "verified_progress": [{"item": f"section {index}"} for index in range(1, 12)],
    },
}
TAP_TARGET = 44


def origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlparse(url)
    return parsed.scheme, parsed.hostname, parsed.port


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

    tabs = [
        {
            "id": TAB,
            "name": "Goal Chat",
            "cwd": "/fixture/project",
            "agent_type": "codex",
            "session_kind": "chat",
            "port": 0,
            "is_active": True,
            "created_at": NOW,
        }
    ]
    report: dict = {
        "result": "FAIL",
        "api_requests": [],
        "blocked_external_requests": [],
        "blocked_websockets": [],
        "unknown_requests": [],
        "page_errors": [],
        "goal_actions": [],
        "screenshots": [],
        "boundary": "Real SPA rendering with mocked APIs; no backend or provider traffic.",
    }
    current_goal = dict(GOAL)
    failure: BaseException | None = None
    page = None

    async with browser_run.context(
        viewport=PHONE,
        launch_args=["--disable-background-networking", "--disable-component-update"],
    ) as context:
        try:

            async def block_websocket(socket) -> None:
                report["blocked_websockets"].append(socket.url)
                await socket.close(code=1000, reason="mocked goal smoke")

            async def route_all(route) -> None:
                nonlocal current_goal
                request = route.request
                parsed = urlparse(request.url)
                if origin(request.url) != origin(base):
                    report["blocked_external_requests"].append(request.url)
                    await route.abort("blockedbyclient")
                    return
                path = parsed.path
                if not path.startswith("/api/"):
                    await route.continue_()
                    return
                report["api_requests"].append([request.method, path])

                body: object
                if path == "/api/auth/check":
                    body = {"auth_required": False, "user": None}
                elif path == "/api/tabs":
                    body = tabs
                elif path in {"/api/tabs/status", "/api/tabs/archived"}:
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
                        "supports_goals": True,
                        "supports_images": True,
                        "agent_type": "codex",
                        "supports_mode_switch": False,
                    }
                elif path.endswith("/events") or path.endswith("/wait"):
                    body = {"events": [], "next_sequence": -1, "has_more": False}
                elif path.endswith("/live"):
                    await route.fulfill(
                        status=200, content_type="text/event-stream", body=": mock\n\n"
                    )
                    return
                elif path.endswith("/messages"):
                    body = {"messages": []}
                elif path.endswith("/goal/current"):
                    body = current_goal
                elif path == "/api/goals/goal-mobile/pause" and request.method == "POST":
                    report["goal_actions"].append("pause")
                    current_goal = {**current_goal, "status": "paused", "dispatch_state": "idle"}
                    body = current_goal
                elif path.endswith("/view"):
                    body = {"ok": True}
                elif "network" in path:
                    body = {"hostname": "fixture", "addresses": []}
                elif path == "/api/dot/identity" and request.method == "GET":
                    body = {"name": "Dot", "avatar": "", "avatar_image": None}
                else:
                    report["unknown_requests"].append([request.method, path])
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
            await context.route("**/*", route_all)
            page = await context.new_page()
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            await page.goto(base + f"/?tab={TAB}", wait_until="domcontentloaded")

            bar = page.get_by_role("region", name="Chat Goal status")
            await expect(bar).to_be_visible()
            summary = bar.get_by_role("button", name="Active", exact=False).first
            quick = bar.get_by_role("button", name="Pause", exact=True)
            await expect(quick).to_be_visible()
            # No sideways-scrolling strip of small buttons on the bar.
            assert await bar.get_by_role("button", name="Clear", exact=True).count() == 0
            for control in (summary, quick):
                box = await control.bounding_box()
                assert box and box["height"] >= TAP_TARGET - 4, box
            bar_box = await bar.bounding_box()
            assert bar_box and bar_box["height"] <= 64, bar_box
            await page.screenshot(path=str(output / "goal-mobile-bar.png"))
            report["screenshots"].append("goal-mobile-bar.png")

            await summary.click()
            sheet = page.get_by_role("dialog", name="Chat Goal details")
            await expect(sheet).to_be_visible()
            sheet_box = await sheet.bounding_box()
            assert sheet_box and sheet_box["y"] + sheet_box["height"] <= PHONE["height"] + 1
            assert sheet_box["height"] <= PHONE["height"] * 0.86 + 1, sheet_box
            # Every action is on screen and easy to tap, however long the Goal is.
            labels = ["Pause", "Edit Goal", "Complete", "Clear"]
            for label in labels:
                button = sheet.get_by_role("button", name=label, exact=True)
                await expect(button).to_be_in_viewport()
                box = await button.bounding_box()
                assert box and box["height"] >= TAP_TARGET and box["width"] >= 120, (label, box)
            # The long text scrolls inside the sheet; the actions stay put.
            text = sheet.locator(".goal-status-details-body")
            scrolls = await text.evaluate("node => node.scrollHeight > node.clientHeight + 100")
            assert scrolls, "the long objective scrolls inside the sheet"
            clear_box = await sheet.get_by_role("button", name="Clear", exact=True).bounding_box()
            await text.evaluate("node => { node.scrollTop = node.scrollHeight }")
            assert (
                clear_box
                == await sheet.get_by_role("button", name="Clear", exact=True).bounding_box()
            )
            await expect(
                sheet.get_by_text("Check section 12 against the source table.")
            ).to_be_visible()
            await page.screenshot(path=str(output / "goal-mobile-sheet.png"))
            report["screenshots"].append("goal-mobile-sheet.png")

            await sheet.get_by_role("button", name="Collapse Goal details").click()
            await expect(sheet).not_to_be_visible()
            await expect(summary).to_be_focused()

            # Tapping outside the sheet closes it too.
            await summary.click()
            await expect(sheet).to_be_visible()
            await page.mouse.click(PHONE["width"] / 2, 20)
            await expect(sheet).not_to_be_visible()

            # Escape closes it wherever focus is, and the page behind does not scroll.
            await summary.click()
            await expect(sheet).to_be_visible()
            assert await page.evaluate("document.body.style.overflow") == "hidden"
            await page.evaluate("document.activeElement.blur()")
            await page.keyboard.press("Escape")
            await expect(sheet).not_to_be_visible()
            assert await page.evaluate("document.body.style.overflow") != "hidden"

            # Edit Goal closes the sheet so the dialog is on top and usable.
            await summary.click()
            await sheet.get_by_role("button", name="Edit Goal", exact=True).click()
            await expect(sheet).not_to_be_visible()
            dialog = page.get_by_role("dialog", name="Edit Goal", exact=True)
            await expect(dialog).to_be_visible()
            await expect(
                dialog.get_by_role("button", name="Save Goal", exact=True)
            ).to_be_in_viewport()
            await dialog.get_by_role("button", name="Cancel", exact=True).click()
            await expect(dialog).not_to_be_visible()

            await quick.click()
            await expect(bar.get_by_role("button", name="Resume", exact=True)).to_be_visible()
            assert report["goal_actions"] == ["pause"]
            await page.screenshot(path=str(output / "goal-mobile-paused.png"))
            report["screenshots"].append("goal-mobile-paused.png")

            # Wider screens keep the inline row of actions.
            await page.set_viewport_size({"width": 1280, "height": 900})
            for label in ("Resume", "Edit Goal", "Complete", "Clear"):
                await expect(bar.get_by_role("button", name=label, exact=True)).to_be_visible()

            assert not report["unknown_requests"], report["unknown_requests"]
            assert not report["page_errors"], report["page_errors"]
            report["result"] = "PASS"
        except BaseException as exc:
            failure = exc
            report["error_type"] = type(exc).__name__
            report["error"] = str(exc)[:2000]
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
