"""Exercise restart choices and observation recovery in the actual SPA.

python tests/browser_service_restart.py <owned-loopback-url> <new-output-dir> <browser>
All APIs are fixtures; no real service restart or provider executes here.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_lifecycle import BrowserRun, run_browser_main  # noqa: E402

TAB = "restart-chat"
NOW = "2026-10-15T08:00:00Z"


def event(sequence: int, kind: str, payload: dict, turn: str) -> dict:
    return {
        "stream_sequence": sequence,
        "session_id": f"terminal-tab-{TAB}",
        "tab_id": TAB,
        "agent_type": "cursor",
        "type": kind,
        "turn_id": turn,
        "message_id": f"{turn}:user" if kind == "turn_started" else turn,
        "payload": payload,
        "created_at": NOW,
        "redacted": False,
    }


async def main(browser_run: BrowserRun) -> None:
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
            "name": "Restart Chat",
            "cwd": "/fixture/project",
            "agent_type": "cursor",
            "session_kind": "chat",
            "port": 0,
            "is_active": True,
            "created_at": NOW,
        }
    ]
    events = [
        event(0, "turn_started", {"summary": "Work in progress", "mode": "default"}, "old"),
        event(1, "text_delta", {"text": "Working before restart"}, "old"),
    ]
    instance = "instance-0"
    operation = None
    polls = 0
    outage = False
    failures = 0
    posts: list[dict] = []
    report: dict = {
        "result": "FAIL",
        "api_requests": [],
        "unknown_requests": [],
        "blocked_external_requests": [],
        "blocked_websockets": [],
        "page_errors": [],
        "restart_posts": posts,
        "screenshots": [],
        "boundary": "Actual Vue/Pinia/Chat observer with mocked APIs; no backend/provider/restart executes.",
    }
    failure = None
    page = None
    async with browser_run.context(
        viewport={"width": 1280, "height": 900},
        launch_args=["--disable-background-networking"],
    ) as context:
        try:

            async def block_socket(socket):
                report["blocked_websockets"].append(socket.url)
                await socket.close(code=1000, reason="restart UI fixtures")

            async def route_all(route):
                nonlocal operation, instance, polls, outage, failures
                request = route.request
                parsed = urlparse(request.url)
                if (parsed.scheme, parsed.netloc) != (
                    parsed_base.scheme,
                    parsed_base.netloc,
                ):
                    report["blocked_external_requests"].append(request.url)
                    await route.abort("blockedbyclient")
                    return
                path = parsed.path
                if not path.startswith("/api/"):
                    await route.continue_()
                    return
                report["api_requests"].append([request.method, path])
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
                        "supports_goals": False,
                        "supports_images": False,
                        "agent_type": "cursor",
                        "supports_mode_switch": False,
                    }
                elif path.endswith("/events") or path.endswith("/wait"):
                    if path.endswith("/wait"):
                        if outage:
                            failures += 1
                            if failures <= 8:
                                await route.fulfill(status=503, body="restarting")
                                return
                            outage = False
                            events.extend(
                                [
                                    event(
                                        2,
                                        "turn_completed",
                                        {"status": "cancelled"},
                                        "old",
                                    ),
                                    event(
                                        3,
                                        "turn_started",
                                        {
                                            "summary": "Please continue.",
                                            "mode": "default",
                                            "metadata": {"origin": "restart_continuation"},
                                        },
                                        "continued",
                                    ),
                                    event(
                                        4,
                                        "text_delta",
                                        {"text": "Recovered after restart"},
                                        "continued",
                                    ),
                                    event(
                                        5,
                                        "turn_completed",
                                        {"status": "completed"},
                                        "continued",
                                    ),
                                ]
                            )
                        since = json.loads(request.post_data or "{}").get("since_sequence", -1)
                        await asyncio.sleep(0.1)
                    else:
                        since = int(parse_qs(parsed.query).get("since_sequence", ["-1"])[0])
                    rows = [item for item in events if item["stream_sequence"] > since]
                    body = {
                        "events": rows,
                        "next_sequence": rows[-1]["stream_sequence"] if rows else since,
                        "has_more": False,
                    }
                elif path.endswith("/live"):
                    await route.fulfill(
                        status=200,
                        content_type="text/event-stream",
                        body=": fixture\n\n",
                    )
                    return
                elif path == "/api/system/restart":
                    if request.method == "POST":
                        payload = json.loads(request.post_data or "{}")
                        posts.append(payload)
                        assert request.headers.get("x-claude-hub-restart") == "1"
                        operation = {
                            "id": payload["request_id"],
                            "instance_id": instance,
                            "status": "preparing",
                            "message": "Preparing service",
                            "resume_running_chats": payload["resume_running_chats"],
                        }
                        polls = 0
                        if len(posts) == 1:
                            outage = True
                            # The request was accepted, but the response was lost.
                            await route.abort("connectionreset")
                            return
                    elif operation is not None:
                        polls += 1
                        if polls >= 3:
                            instance = f"instance-{len(posts)}"
                            operation.update(status="succeeded", message="Back online")
                            if operation["resume_running_chats"]:
                                operation["continuation"] = {
                                    "total": 1,
                                    "pending": int(polls == 3),
                                    "started": int(polls > 3),
                                    "skipped": 0,
                                    "attention": 0,
                                }
                    body = {
                        "instance_id": instance,
                        "available": True,
                        "reason": "",
                        "operation": operation,
                    }
                elif path.endswith("/goal/current"):
                    body = None
                elif path.endswith("/view"):
                    body = {"ok": True}
                elif "network" in path:
                    body = {"hostname": "fixture", "addresses": []}
                else:
                    report["unknown_requests"].append([request.method, path])
                    await route.fulfill(status=501, body="unknown fixture route")
                    return
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(body)
                )

            await context.route_web_socket("**/*", block_socket)
            await context.route("**/*", route_all)
            page = await context.new_page()
            await page.clock.install()
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            await page.goto(base + f"/?tab={TAB}", wait_until="domcontentloaded")
            await expect(page.get_by_text("Working before restart", exact=True)).to_be_visible()
            composer = page.locator(".composer-textarea:visible").first
            await composer.fill("Keep this unsent draft")

            async def open_dialog():
                await page.get_by_label("Extensions", exact=True).click()
                await page.get_by_role("button", name="Build and restart", exact=True).click()
                dialog = page.get_by_role("dialog", name="Build and restart service", exact=True)
                await expect(dialog).to_be_visible()
                return dialog

            dialog = await open_dialog()
            checkbox = dialog.get_by_role(
                "checkbox", name=re.compile("Continue interrupted Chats automatically")
            )
            await expect(checkbox).to_be_checked()
            await expect(dialog).to_contain_text("Please continue.")
            await page.screenshot(path=str(output / "restart-desktop.png"), full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            await expect(checkbox).to_be_in_viewport()
            await expect(
                dialog.get_by_role("button", name="Build and restart", exact=True)
            ).to_be_in_viewport()
            await page.screenshot(path=str(output / "restart-mobile.png"), full_page=True)
            report["screenshots"] = ["restart-desktop.png", "restart-mobile.png"]
            await page.set_viewport_size({"width": 1280, "height": 900})
            await dialog.get_by_role("button", name="Build and restart", exact=True).click()
            for _ in range(30):
                await page.clock.fast_forward(5000)
                await asyncio.sleep(0.1)
                if failures >= 9:
                    break
            await expect(page.get_by_text("Recovered after restart", exact=True)).to_be_visible()
            await expect(page.get_by_role("heading", name="Service is back online")).to_be_visible()
            await expect(page.locator(".restart-dialog")).to_contain_text("1 started")
            await page.locator(".restart-dialog").get_by_role(
                "button", name="Close", exact=True
            ).click()
            await expect(composer).to_have_value("Keep this unsent draft")
            assert len(posts) == 1 and posts[0]["resume_running_chats"] is True

            # The explicit opt-out is a new request; it must not inherit the
            # previous checked choice or send a second continuation.
            dialog = await open_dialog()
            await dialog.get_by_role(
                "checkbox", name=re.compile("Continue interrupted Chats automatically")
            ).uncheck()
            await dialog.get_by_role("button", name="Build and restart", exact=True).click()
            for _ in range(6):
                await page.clock.fast_forward(1000)
                await asyncio.sleep(0.1)
            await expect(page.get_by_role("heading", name="Service is back online")).to_be_visible()
            assert len(posts) == 2 and posts[1]["resume_running_chats"] is False
            assert posts[0]["request_id"] != posts[1]["request_id"]
            assert not report["unknown_requests"], report["unknown_requests"]
            assert not report["page_errors"], report["page_errors"]
            assert not any(path.endswith(("/retry", "/send")) for _, path in report["api_requests"])
            report.update(
                result="PASS",
                outage_wait_attempts=failures,
                transient_failures=min(failures, 8),
                draft_preserved=True,
                lost_response_single_post=True,
                provider_replacements=0,
            )
        except BaseException as exc:
            failure = exc
            report["error_type"] = type(exc).__name__
            if page is not None:
                try:
                    await page.screenshot(path=str(output / "failure.png"), full_page=True)
                except Exception:
                    pass
    (output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    if failure is not None:
        raise failure


if __name__ == "__main__":
    run_browser_main(main)
