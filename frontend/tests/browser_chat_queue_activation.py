"""Exercise hidden queued-Chat activation in the real rendered SPA.

Run only with a task-owned Vite server, never 5173/8173:
  python tests/browser_chat_queue_activation.py <owned-loopback-url> <new-output-dir> <browser>

All APIs are mocked. No Hub backend or provider is contacted.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright, expect

TAB_A = "queue-tab-a"
TAB_B = "queue-tab-b"
OTHER_TABS = [TAB_B, *(f"queue-tab-{index}" for index in range(2, 11))]
TURN = "queue-turn-a"
NOW = "2026-10-11T08:00:00Z"


def origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlparse(url)
    return parsed.scheme, parsed.hostname, parsed.port


def event(sequence: int, event_type: str, payload: dict) -> dict:
    return {
        "stream_sequence": sequence,
        "session_id": f"terminal-{TAB_A}",
        "tab_id": TAB_A,
        "agent_type": "traex",
        "type": event_type,
        "turn_id": TURN,
        "message_id": f"{TURN}:user" if event_type == "turn_started" else TURN,
        "payload": payload,
        "created_at": NOW,
        "redacted": False,
    }


STARTED = event(0, "turn_started", {"summary": "Initial work", "mode": "default"})
COMPLETED = event(1, "turn_completed", {"status": "completed"})


async def main() -> None:
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
    browser_path = Path(sys.argv[3]).resolve(strict=True)
    if not browser_path.is_file() or not os.access(browser_path, os.X_OK):
        raise SystemExit("An existing executable browser is required")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)

    tabs = [
        {
            "id": tab_id,
            "name": name,
            "cwd": "/fixture/project",
            "agent_type": "traex",
            "session_kind": "chat",
            "port": 0,
            "is_active": True,
            "created_at": NOW,
        }
        for tab_id, name in (
            (TAB_A, "Queued Chat"),
            *((tab_id, f"Other Chat {index}") for index, tab_id in enumerate(OTHER_TABS, 1)),
        )
    ]
    completion_released = asyncio.Event()
    completion_visible = False
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
    browser = context = page = None

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                headless=True,
                executable_path=str(browser_path),
                args=["--disable-background-networking", "--disable-component-update"],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                service_workers="block",
            )

            async def block_websocket(socket) -> None:
                report["blocked_websockets"].append(socket.url)
                await socket.close(code=1000, reason="mocked queue smoke")

            async def route_all(route) -> None:
                nonlocal completion_visible
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
                        "agent_type": "traex",
                        "supports_mode_switch": False,
                    }
                elif path.endswith("/events"):
                    tab_id = TAB_A if f"/{TAB_A}/" in path else TAB_B
                    since = int(parse_qs(parsed.query).get("since_sequence", ["-1"])[0])
                    rows = []
                    if tab_id == TAB_A:
                        candidates = [STARTED, *([COMPLETED] if completion_visible else [])]
                        rows = [item for item in candidates if item["stream_sequence"] > since]
                    next_sequence = rows[-1]["stream_sequence"] if rows else since
                    body = {"events": rows, "next_sequence": next_sequence, "has_more": False}
                elif path.endswith("/wait"):
                    tab_id = TAB_A if f"/{TAB_A}/" in path else TAB_B
                    since = int(parse_qs(parsed.query).get("since_sequence", ["-1"])[0])
                    if tab_id == TAB_A and not completion_visible:
                        await completion_released.wait()
                        completion_visible = True
                    else:
                        await asyncio.sleep(0.2)
                    rows = [COMPLETED] if tab_id == TAB_A and since < 1 else []
                    next_sequence = rows[-1]["stream_sequence"] if rows else since
                    body = {"events": rows, "next_sequence": next_sequence, "has_more": False}
                elif path.endswith("/live"):
                    await route.fulfill(
                        status=200, content_type="text/event-stream", body=": mock\n\n"
                    )
                    return
                elif path.endswith("/stream/send") and request.method == "POST":
                    send_payloads.append(json.loads(request.post_data or "{}"))
                    body = {"ok": True}
                elif path.startswith("/api/tabs/sidebar-preferences"):
                    # Read, one-shot pin migration and per-tab pin updates
                    # share one shape; this scenario does not depend on pins.
                    body = {"pinned_tab_ids": []}
                elif path.endswith("/goal/current"):
                    body = None
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
            await page.goto(base + f"/?tab={TAB_A}", wait_until="domcontentloaded")

            composer = page.locator(".composer-textarea").first
            await expect(composer).to_be_enabled()
            await composer.fill("Queued after the first turn")
            await composer.press("Enter")
            await expect(page.locator(".composer-queue")).to_have_text("1 message queued")

            await page.locator(".chat-sidebar__show-more").click()
            for tab_id in OTHER_TABS:
                await page.locator(
                    f'.chat-sidebar__item[data-tab-id="{tab_id}"] .chat-sidebar__item-main'
                ).click()
            await expect(
                page.locator(f'.chat-sidebar__item[data-tab-id="{OTHER_TABS[-1]}"]')
            ).to_have_class(re.compile(r"(^|\s)active(\s|$)"))
            await page.screenshot(path=str(output / "queued-while-hidden.png"), full_page=True)
            report["screenshots"].append("queued-while-hidden.png")

            completion_released.set()
            active_a = page.locator(f'.chat-sidebar__item[data-tab-id="{TAB_A}"]')
            await expect(active_a).to_have_class(re.compile(r"(^|\s)active(\s|$)"), timeout=5000)
            await expect(page.locator(".composer-queue")).to_have_count(0, timeout=5000)
            deadline = asyncio.get_running_loop().time() + 5
            while not send_payloads and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            assert len(send_payloads) == 1, send_payloads
            assert send_payloads[0]["text"] == "Queued after the first turn"
            await asyncio.sleep(0.25)
            assert len(send_payloads) == 1, send_payloads
            await page.screenshot(path=str(output / "activated-and-sent.png"), full_page=True)
            report["screenshots"].append("activated-and-sent.png")

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
