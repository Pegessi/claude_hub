"""Exercise steering an active Goal in the real rendered SPA.

Run only with a task-owned Vite server, never 5173/8173:
  python tests/browser_chat_goal_steer.py <owned-loopback-url> <new-output-dir> <browser>

All APIs are mocked. No Hub backend or provider is contacted.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_lifecycle import BrowserRun, run_browser_main  # noqa: E402

TAB = "goal-steer-tab"
DONE_TURN = "goal-turn-1"
LIVE_TURN = "goal-turn-2"
NOW = "2026-10-12T08:00:00Z"
ENVELOPE = '<goal-checkpoint>{"verified_progress":"x"}</goal-checkpoint>\n<goal-status state="continue">more work</goal-status>'


def origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlparse(url)
    return parsed.scheme, parsed.hostname, parsed.port


def event(sequence: int, event_type: str, turn: str, payload: dict, message_id: str) -> dict:
    return {
        "stream_sequence": sequence,
        "session_id": f"terminal-{TAB}",
        "tab_id": TAB,
        "agent_type": "codex",
        "type": event_type,
        "turn_id": turn,
        "message_id": message_id,
        "payload": payload,
        "created_at": NOW,
        "redacted": False,
    }


EVENTS = [
    event(
        0,
        "turn_started",
        DONE_TURN,
        {"summary": "Goal step one", "mode": "default"},
        f"{DONE_TURN}:user",
    ),
    event(
        1,
        "text_delta",
        DONE_TURN,
        {"text": f"Step one finished.\n{ENVELOPE}"},
        f"{DONE_TURN}:assistant",
    ),
    event(2, "turn_completed", DONE_TURN, {"status": "completed"}, DONE_TURN),
    event(
        3,
        "turn_started",
        LIVE_TURN,
        {"summary": "Goal step two", "mode": "default"},
        f"{LIVE_TURN}:user",
    ),
]

GOAL = {
    "id": "goal-steer",
    "tab_id": TAB,
    "objective": "Polish the fixture",
    "status": "active",
    "token_usage": None,
    "usage_quality": "unavailable",
    "turns_completed": 1,
    "dispatch_state": "dispatched",
    "pending_step_id": None,
    "current_turn_id": LIVE_TURN,
    "completed_turn_ids": [DONE_TURN],
    "created_at": NOW,
    "updated_at": NOW,
}


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
    # run_browser_main validates the browser and creates the output dir.

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
    send_payloads: list[dict] = []
    report = {
        "result": "FAIL",
        "api_requests": [],
        "blocked_external_requests": [],
        "blocked_websockets": [],
        "unknown_requests": [],
        "page_errors": [],
        "send_payloads": send_payloads,
        "screenshots": [],
        "boundary": "Real SPA rendering with mocked APIs; no backend or provider traffic.",
    }
    failure: BaseException | None = None
    page = None

    async with browser_run.context(
        viewport={"width": 1280, "height": 900},
        launch_args=["--disable-background-networking", "--disable-component-update"],
    ) as context:
        try:

            async def block_websocket(socket) -> None:
                report["blocked_websockets"].append(socket.url)
                await socket.close(code=1000, reason="mocked goal smoke")

            async def route_all(route) -> None:
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
                elif path.endswith("/events"):
                    since = int(parse_qs(parsed.query).get("since_sequence", ["-1"])[0])
                    rows = [item for item in EVENTS if item["stream_sequence"] > since]
                    next_sequence = rows[-1]["stream_sequence"] if rows else since
                    body = {"events": rows, "next_sequence": next_sequence, "has_more": False}
                elif path.endswith("/wait"):
                    since = int(parse_qs(parsed.query).get("since_sequence", ["-1"])[0])
                    await asyncio.sleep(0.2)
                    body = {"events": [], "next_sequence": since, "has_more": False}
                elif path.endswith("/live"):
                    await route.fulfill(
                        status=200, content_type="text/event-stream", body=": mock\n\n"
                    )
                    return
                elif path.endswith("/stream/send") and request.method == "POST":
                    send_payloads.append(json.loads(request.post_data or "{}"))
                    body = {"ok": True}
                elif path.endswith("/goal/current"):
                    body = GOAL
                elif path.endswith("/view"):
                    body = {"ok": True}
                elif "network" in path:
                    body = {"hostname": "fixture", "addresses": []}
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

            await expect(page.get_by_text("Step one finished.")).to_be_visible()
            assert "goal-status" not in await page.locator(".structured-pane").first.inner_text()

            composer = page.locator(".composer-textarea").first
            await expect(composer).to_be_enabled()
            await expect(page.get_by_text("⌘/Ctrl+Enter steer Goal")).to_be_visible()

            await composer.fill("Queue me for later")
            await composer.press("Enter")
            await expect(page.locator(".composer-queue")).to_have_text("1 message queued")
            await asyncio.sleep(0.3)
            assert not send_payloads, send_payloads
            await page.screenshot(path=str(output / "goal-steer-composer.png"), full_page=True)
            report["screenshots"].append("goal-steer-composer.png")

            await composer.evaluate("""(el) => {
                    const data = new DataTransfer()
                    data.items.add(new File([new Uint8Array([137, 80, 78, 71])], 'shot.png',
                        { type: 'image/png' }))
                    el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true }))
                }""")
            await expect(page.locator(".composer-error")).to_have_text(
                "Pause or complete the active Goal before attaching images."
            )
            await expect(page.locator(".composer-attachments .attachment-thumb")).to_have_count(0)

            await composer.fill("Steer the Goal now")
            await composer.press("Meta+Enter")
            deadline = asyncio.get_running_loop().time() + 5
            while not send_payloads and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            assert len(send_payloads) == 1, send_payloads
            assert send_payloads[0]["text"] == "Steer the Goal now"
            assert send_payloads[0]["delivery"] == "steer"
            await expect(page.locator(".composer-queue")).to_have_text("1 message queued")

            assert not report["unknown_requests"], report["unknown_requests"]
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

    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    if failure is not None:
        raise failure


if __name__ == "__main__":
    run_browser_main(main)
