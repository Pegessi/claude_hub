"""Render native Goal state changes against mocked APIs on an owned Vite server.

Usage: python tests/browser_chat_native_goal.py OWNED_URL NEW_OUTPUT_DIR BROWSER
No Hub backend or provider is contacted.
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

TAB = "native-goal-tab"
THREAD = "native-thread"
NOW = "2026-10-20T00:00:00Z"
GOAL_LIKE_ASSISTANT_TEXT = "\n".join(
    [
        "Provider-native answer preserves a trailing Goal-like block:",
        '<goal-status state="complete">VISIBLE_TRAILING_STATUS</goal-status>',
    ]
)


def origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlparse(url)
    return parsed.scheme, parsed.hostname, parsed.port


def snapshot(state: str, revision: int) -> dict:
    return {
        "id": "native-goal",
        "tab_id": TAB,
        "objective": "Finish the native provider fixture",
        "status": state,
        "execution_owner": "provider_native",
        "provider_thread_id": THREAD,
        "provider_goal_created_at": 100,
        "provider_updated_at": revision,
        "provider_status": state,
        "token_usage": 20,
        "usage_quality": "exact",
        "turns_completed": 0,
        "dispatch_state": "idle",
        "created_at": NOW,
        "updated_at": NOW,
    }


def notification(sequence: int, goal: dict | None) -> dict:
    payload: dict = {"threadId": THREAD}
    if goal:
        payload["goal"] = {
            "createdAt": goal["provider_goal_created_at"],
            "updatedAt": goal["provider_updated_at"],
            "status": goal["provider_status"],
        }
    return {
        "stream_sequence": sequence,
        "session_id": f"terminal-tab-{TAB}",
        "tab_id": TAB,
        "agent_type": "codex",
        "type": "status",
        "turn_id": None,
        "message_id": None,
        "call_id": None,
        "payload": {
            "provider_notification": "thread/goal/updated" if goal else "thread/goal/cleared",
            "goal": payload,
        },
        "created_at": NOW,
        "redacted": False,
    }


def turn_event(sequence: int, event_type: str, turn_id: str, payload: dict) -> dict:
    return {
        "stream_sequence": sequence,
        "session_id": f"terminal-tab-{TAB}",
        "tab_id": TAB,
        "agent_type": "codex",
        "type": event_type,
        "run_epoch": 1,
        "turn_id": turn_id,
        "message_id": f"{turn_id}:user" if event_type == "turn_started" else None,
        "call_id": None,
        "payload": payload,
        "created_at": NOW,
        "redacted": False,
    }


def transcript_events() -> list[dict]:
    turn_id = "native-visible-output-turn"
    return [
        turn_event(
            0,
            "turn_started",
            turn_id,
            {
                "summary": "Please quote the Goal protocol",
                "attachments": [],
                "mode": "default",
                "metadata": {"origin": "web", "goal_execution_owner": "provider_native"},
            },
        ),
        turn_event(
            1,
            "text_delta",
            turn_id,
            {"text": GOAL_LIKE_ASSISTANT_TEXT},
        ),
        turn_event(
            2,
            "turn_completed",
            turn_id,
            {"status": "completed", "assistant_text": GOAL_LIKE_ASSISTANT_TEXT},
        ),
    ]


async def main(browser_run: BrowserRun) -> None:
    base = sys.argv[1].rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.port in {None, 5173, 8173, 10025, 18183}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit("Use an owned numeric-loopback review server")
    output = Path(sys.argv[2]).resolve()
    output.mkdir(mode=0o700)
    report = {
        "result": "FAIL",
        "api_requests": [],
        "blocked_external_requests": [],
        "unknown_requests": [],
        "page_errors": [],
        "checks": [],
        "boundary": "Real SPA rendering with mocked APIs; no live Hub/provider traffic.",
    }
    current: dict | None = snapshot("active", 200)
    stale: list[dict | None] = []
    events: list[dict] = transcript_events()
    supports_goals = True
    page = None

    async with browser_run.context(
        viewport={"width": 1280, "height": 900},
        launch_args=["--disable-background-networking", "--disable-component-update"],
    ) as context:
        try:

            async def block_socket(socket) -> None:
                await socket.close(code=1000, reason="native Goal fixture owns all APIs")

            async def route_all(route) -> None:
                nonlocal current
                request = route.request
                url = urlparse(request.url)
                if origin(request.url) != origin(base):
                    report["blocked_external_requests"].append(request.url)
                    await route.abort("blockedbyclient")
                    return
                path = url.path
                if not path.startswith("/api/"):
                    await route.continue_()
                    return
                report["api_requests"].append([request.method, path])
                body: object
                if path == "/api/auth/check":
                    body = {"auth_required": False, "user": None}
                elif path == "/api/tabs":
                    body = [
                        {
                            "id": TAB,
                            "name": "Native Goal",
                            "cwd": "/fixture/project",
                            "agent_type": "codex",
                            "session_kind": "chat",
                            "port": 0,
                            "is_active": True,
                            "created_at": NOW,
                        }
                    ]
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
                        "supports_images": True,
                        "supports_goals": supports_goals,
                        "goal_execution_owner": "provider_native",
                        "agent_type": "codex",
                        "supports_mode_switch": False,
                    }
                elif request.method == "GET" and path == f"/api/workspaces/tabs/{TAB}/messages":
                    body = {"messages": []}
                elif path.endswith("/events") or path.endswith("/wait"):
                    if path.endswith("/wait"):
                        await asyncio.sleep(0.1)
                    since = int(parse_qs(url.query).get("since_sequence", ["-1"])[0])
                    rows = [item for item in events if item["stream_sequence"] > since]
                    body = {
                        "events": rows,
                        "next_sequence": rows[-1]["stream_sequence"] if rows else since,
                        "has_more": False,
                    }
                elif path.endswith("/live"):
                    await route.fulfill(
                        status=200, content_type="text/event-stream", body=": mock\n\n"
                    )
                    return
                elif path.endswith("/goal/current"):
                    body = stale.pop(0) if stale else current
                elif path == "/api/goals/native-goal/pause" and request.method == "POST":
                    assert current is not None and current.get("provider_stop_pending")
                    assert current["status"] == "complete"
                    current = {**current, "provider_stop_pending": False, "dispatch_state": "idle"}
                    body = current
                elif path == "/api/goals/native-goal/clear" and request.method == "POST":
                    current = None
                    events.append(notification(len(events), None))
                    body = None
                elif path.endswith("/view"):
                    body = {"ok": True}
                elif "network" in path:
                    body = {"hostname": "fixture", "addresses": []}
                elif path == "/api/dot/identity":
                    body = {"name": "Dot", "avatar": "", "avatar_image": None}
                else:
                    report["unknown_requests"].append([request.method, path])
                    await route.fulfill(status=501, json={"detail": "unmocked API"})
                    return
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(body)
                )

            await context.route_web_socket("**/*", block_socket)
            await context.route("**/*", route_all)
            page = await context.new_page()
            await page.add_init_script("""window.__copiedTurns = [];
                Object.defineProperty(navigator, 'clipboard', { configurable: true,
                  value: { writeText: async text => window.__copiedTurns.push(text) } });""")
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            await page.goto(base + f"/?tab={TAB}", wait_until="domcontentloaded")
            transcript = page.get_by_role("log", name="Chat conversation")
            await expect(transcript).to_contain_text("VISIBLE_TRAILING_STATUS")
            first_turn = page.locator(".structured-turn").first
            await first_turn.hover()
            await first_turn.get_by_role("button", name="Copy conversation", exact=True).click()
            copied = await page.evaluate("window.__copiedTurns.at(-1)")
            assert copied == (
                "User:\nPlease quote the Goal protocol\n\n"
                f"Assistant:\n{GOAL_LIKE_ASSISTANT_TEXT}"
            )
            report["checks"].append("ordinary/native Goal-like text remains visible and copyable")

            bar = page.get_by_role("region", name="Chat Goal status")
            await expect(bar).to_have_attribute("data-status", "active")
            await expect(bar).to_have_attribute("data-execution-owner", "provider_native")
            await bar.locator(".goal-status-summary").click()
            await expect(bar.get_by_text("Managed by provider", exact=True)).to_be_visible()
            await bar.get_by_role("button", name="Collapse Goal details").click()

            # No turn completes here. Each first read deliberately returns stale
            # state, proving that notifications trigger bounded reconciliation.
            for state, revision in [
                ("paused", 201),
                ("active", 202),
                ("usageLimited", 203),
                ("budgetLimited", 204),
            ]:
                stale.append(current)
                current = snapshot(state, revision)
                events.append(notification(len(events), current))
                await expect(bar).to_have_attribute("data-status", state)
                if state in {"usageLimited", "budgetLimited"}:
                    label = "Usage limited" if state == "usageLimited" else "Budget limited"
                    await expect(bar.get_by_text(label, exact=True)).to_be_visible()
                    await expect(
                        bar.get_by_role("button", name="Resume", exact=True)
                    ).to_have_count(0)
                    await expect(
                        bar.get_by_role("button", name="Complete", exact=True)
                    ).to_be_visible()
                    await expect(bar.locator('input[type="number"]')).to_have_count(0)
                report["checks"].append(f"notification reconciled {state}")
            await page.screenshot(path=str(output / "native-goal-limited.png"), full_page=True)

            current = {
                **snapshot("complete", 205),
                "provider_stop_pending": True,
                "dispatch_state": "uncertain",
            }
            events.append(notification(len(events), current))
            await expect(bar).to_have_attribute("data-status", "complete")
            await expect(bar.get_by_role("button", name="Retry stop", exact=True)).to_be_visible()
            await bar.get_by_role("button", name="Retry stop", exact=True).click()
            await expect(bar.get_by_role("button", name="Retry stop", exact=True)).to_have_count(0)
            await expect(bar.locator(".goal-status-summary strong")).to_have_text("Complete")
            assert current is not None and current["status"] == "complete"
            assert ["POST", "/api/goals/native-goal/pause"] in report["api_requests"]
            assert ["POST", "/api/goals/native-goal/clear"] not in report["api_requests"]
            report["checks"].append("retry interruption preserves completed native Goal")

            await bar.get_by_role("button", name="Clear", exact=True).click()
            await expect(bar).to_have_count(0)
            await page.get_by_role("button", name="Add to chat", exact=True).click()
            await expect(
                page.get_by_role("menuitem", name="Set a Goal", exact=True)
            ).to_have_attribute("aria-disabled", "false")
            report["checks"].append("confirmed clear preserves Goal support")
            await page.screenshot(path=str(output / "native-goal-cleared.png"), full_page=True)

            supports_goals = False
            await page.reload(wait_until="domcontentloaded")
            await page.get_by_role("button", name="Add to chat", exact=True).click()
            await expect(page.get_by_role("menuitem", name="Set a Goal", exact=True)).to_have_count(
                0
            )
            report["checks"].append("unavailable Goal capability remains distinct from absence")
            assert not report["unknown_requests"]
            assert not report["page_errors"]
            assert all(
                urlparse(url).hostname == "fonts.googleapis.com" and urlparse(url).path == "/css2"
                for url in report["blocked_external_requests"]
            )
            report["result"] = "PASS"
        finally:
            output.mkdir(parents=True, exist_ok=True)
            (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            if page is not None and report["result"] != "PASS":
                await page.screenshot(path=str(output / "failure.png"), full_page=True)
            print(json.dumps(report, indent=2))


if __name__ == "__main__":
    run_browser_main(main)
