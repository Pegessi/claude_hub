"""Exercise steering an active Goal in the real rendered SPA.

Run only with a task-owned Vite server, never 5173/8173:
  python tests/browser_chat_goal_steer.py <owned-loopback-url> <new-output-dir> <browser>

All APIs are mocked. No Hub backend or provider is contacted.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import struct
import sys
import zlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_lifecycle import BrowserRun, run_browser_main  # noqa: E402

TAB = "goal-steer-tab"
DONE_TURN = "goal-turn-1"
LIVE_TURN = "goal-turn-2"
PLAN_TURN = "goal-plan-turn"
NOW = "2026-10-12T08:00:00Z"
# The browser receives sanitized stream text: the backend removes the
# <goal-plan> block before publishing deltas, so the fixture omits it too.
PLAN_TEXT = (
    "Scope: add the bounded cache and verify it.\n\n"
    "1. Extract the lookup behind a small helper.\n"
    "2. Cover the helper with targeted tests."
)
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


def fixture_png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 800, 600, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\0" + b"\x30\x80\xc0" * 800) * 600))
        + chunk(b"IEND", b"")
    )


PNG_IMAGE = fixture_png()

PLAN_GOAL = {
    "id": "goal-plan-card",
    "tab_id": TAB,
    "objective": "Add a bounded cache",
    "status": "blocked",
    "execution_owner": "hub_managed",
    "token_usage": 120,
    "usage_quality": "exact",
    "turns_completed": 1,
    "dispatch_state": "idle",
    "pending_step_id": None,
    "current_turn_id": None,
    "completed_turn_ids": [PLAN_TURN],
    "plan_first": True,
    "plan_pending": False,
    "plan_turn_id": PLAN_TURN,
    "plan_approved": False,
    "plan": {
        "summary": "Add a bounded cache with tests, nothing else.",
        "steps": [
            {
                "title": "Extract the cache",
                "detail": "Move the lookup behind one helper",
                "acceptance": "no duplicated lookup paths remain",
            },
            {
                "title": "Cover it with tests",
                "detail": "Hit the bound and the eviction path",
                "acceptance": "targeted tests pass",
            },
        ],
        "budget": {
            "helpers": [
                {"role": "implementation", "provider": "claude", "model": "opus"},
                {"role": "verification", "provider": "codex", "model": "gpt-5"},
            ],
            "max_parallelism": 2,
            "duration_estimate": "about 12 minutes",
        },
    },
    "status_message": "Awaiting approval of the plan above.",
    "created_at": NOW,
    "updated_at": NOW,
}

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
    current_goal = dict(GOAL)
    idle_fixture = False
    plan_fixture = False
    fail_next_send = False
    goal_updates = []
    goal_creates = []
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
                nonlocal current_goal, fail_next_send, plan_fixture
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
                elif (
                    request.method == "GET"
                    and path.startswith("/api/workspaces/tabs/")
                    and path.endswith("/messages")
                ):
                    body = {"messages": []}
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
                    fixture_events = list(EVENTS)
                    if idle_fixture:
                        fixture_events.append(
                            event(
                                4,
                                "turn_completed",
                                LIVE_TURN,
                                {"status": "completed"},
                                LIVE_TURN,
                            )
                        )
                    if plan_fixture:
                        fixture_events.append(
                            event(
                                5,
                                "turn_started",
                                PLAN_TURN,
                                {
                                    "summary": "Plan this Goal before starting",
                                    "mode": "default",
                                    "metadata": {
                                        "origin": "goal",
                                        "protocol": "goal-continuation-v1",
                                    },
                                },
                                f"{PLAN_TURN}:user",
                            )
                        )
                        fixture_events.append(
                            event(
                                6,
                                "text_delta",
                                PLAN_TURN,
                                {"text": PLAN_TEXT},
                                f"{PLAN_TURN}:assistant",
                            )
                        )
                        fixture_events.append(
                            event(
                                7,
                                "turn_completed",
                                PLAN_TURN,
                                {"status": "completed"},
                                PLAN_TURN,
                            )
                        )
                    rows = [item for item in fixture_events if item["stream_sequence"] > since]
                    next_sequence = rows[-1]["stream_sequence"] if rows else since
                    body = {
                        "events": rows,
                        "next_sequence": next_sequence,
                        "has_more": False,
                    }
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
                    if fail_next_send:
                        fail_next_send = False
                        current_goal = {
                            **current_goal,
                            "status": "paused",
                            "dispatch_state": "uncertain",
                            "pending_image_count": 1,
                        }
                        await route.fulfill(
                            status=503,
                            content_type="application/json",
                            body=json.dumps(
                                {
                                    "detail": "Image delivery interrupted; reattach images and recover"
                                }
                            ),
                        )
                        return
                    if current_goal:
                        current_goal = {
                            **current_goal,
                            "status": "active",
                            "dispatch_state": "dispatched",
                            "pending_image_count": 0,
                        }
                    body = {"ok": True}
                elif path.endswith("/goal/current"):
                    body = current_goal
                elif path.endswith("/objective") and request.method == "PATCH":
                    update = json.loads(request.post_data or "{}")
                    goal_updates.append(update)
                    current_goal = {**current_goal, "objective": update["objective"]}
                    body = current_goal
                elif path.endswith("/goal") and request.method == "POST":
                    creation = json.loads(request.post_data or "{}")
                    goal_creates.append(creation)
                    current_goal = {**GOAL, "objective": creation["objective"]}
                    body = current_goal
                elif path.endswith("/view"):
                    body = {"ok": True}
                elif "network" in path:
                    body = {"hostname": "fixture", "addresses": []}
                elif path == "/api/dot/identity" and request.method == "GET":
                    # The Dot launcher in the tab bar reads Dot's name and avatar.
                    body = {"name": "Dot", "avatar": "", "avatar_image": None}
                elif request.method == "GET" and path == "/api/subagents":
                    # Every Chat polls Hub sub-agent links; this fixture has none.
                    body = []
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
            # Match remote HTTP deployments where randomUUID is unavailable.
            await context.add_init_script(
                "Object.defineProperty(globalThis.crypto, 'randomUUID', {value: undefined, configurable: true})"
            )
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
            queue = page.get_by_role("region", name="Queued messages")
            await expect(queue).to_be_visible()
            item = queue.locator(".queued-draft").first
            await item.get_by_role("button", name="Edit", exact=True).click()
            await item.get_by_role("textbox").fill("Updated queued instruction")
            await item.get_by_role("button", name="Save", exact=True).click()
            await expect(item.locator("summary")).to_contain_text("Updated queued instruction")
            await composer.fill("Cancel this input")
            await composer.press("Enter")
            await queue.locator(".queued-draft").nth(1).get_by_role(
                "button", name="Cancel", exact=True
            ).click()
            await expect(queue.locator(".queued-draft")).to_have_count(1)
            assert not send_payloads

            await page.get_by_role("button", name="Edit Goal", exact=True).click()
            dialog = page.get_by_role("dialog", name="Edit Goal", exact=True)
            await dialog.get_by_role("textbox").fill("Updated Goal outcome")
            await dialog.get_by_role("button", name="Save Goal", exact=True).click()
            await expect(dialog).not_to_be_visible()
            assert goal_updates[-1]["objective"] == "Updated Goal outcome"

            # A queued image opens at original resolution and is steered without Stop.
            await item.get_by_role("button", name="Cancel", exact=True).click()
            await page.locator('input[type="file"]').set_input_files(
                {
                    "name": "shot.png",
                    "mimeType": "image/png",
                    "buffer": PNG_IMAGE,
                }
            )
            await expect(page.locator(".composer-attachments .attachment-thumb")).to_have_count(1)
            await composer.fill("Look at this reference")
            await composer.press("Enter")
            item = queue.locator(".queued-draft").first
            await item.locator("summary").click()
            await item.get_by_role("button", name="View shot.png", exact=True).click()
            await expect(page.get_by_role("dialog", name="Attachment preview")).to_be_visible()
            await expect(page.locator(".attachment-preview-dialog img")).to_have_attribute(
                "src", re.compile("^data:image/png;base64,")
            )
            await page.get_by_role("button", name="Close preview").click()
            await expect(page.get_by_role("dialog", name="Attachment preview")).not_to_be_visible()
            await page.screenshot(
                path=str(output / "goal-queued-image-controls.png"), full_page=True
            )
            report["screenshots"].append("goal-queued-image-controls.png")
            await composer.press("Meta+Enter")
            await expect(queue).not_to_be_visible()
            assert len(send_payloads) == 1, send_payloads
            assert send_payloads[0]["text"] == "Look at this reference"
            assert send_payloads[0]["delivery"] == "steer"
            assert len(send_payloads[0]["attachments"]) == 1
            assert not any(
                path.endswith("/cancel") or path.endswith("/pause")
                for _, path in report["api_requests"]
            )
            await expect(page.get_by_role("button", name="Pause", exact=True)).to_be_visible()

            # Interrupted image steering offers explicit recovery with a new
            # intent ID even when the exact same image is reattached.
            await page.locator('input[type="file"]').set_input_files(
                {"name": "recovery.png", "mimeType": "image/png", "buffer": PNG_IMAGE}
            )
            await composer.fill("Recover using the same reference")
            fail_next_send = True
            await composer.press("Meta+Enter")
            recover = page.get_by_role("button", name="Recover Goal", exact=True)
            await expect(recover).to_be_enabled()
            failed_id = send_payloads[-1]["client_turn_id"]
            await page.get_by_role("button", name="Remove recovery.png", exact=True).click()
            await page.locator('input[type="file"]').set_input_files(
                {"name": "recovery.png", "mimeType": "image/png", "buffer": PNG_IMAGE}
            )
            await expect(recover).to_be_enabled()
            await recover.click()
            await expect(recover).not_to_be_visible()
            assert send_payloads[-1]["client_turn_id"] != failed_id
            assert (
                base64.b64decode(send_payloads[-1]["attachments"][0]["data_url"].split(",", 1)[1])
                == PNG_IMAGE
            )
            await expect(page.get_by_role("button", name="Pause", exact=True)).to_be_visible()

            # Goal creation includes composer images in its first request.
            current_goal = None
            idle_fixture = True
            await page.reload(wait_until="domcontentloaded")
            await expect(composer).to_be_enabled()
            await page.get_by_role("button", name="Add to chat", exact=True).click()
            await page.get_by_role("menuitem", name="Set a Goal").click()
            dialog = page.get_by_role("dialog", name="Set a Goal", exact=True)
            await dialog.get_by_role("textbox").fill("Build from the attached reference")
            await page.locator('input[type="file"]').set_input_files(
                {
                    "name": "goal-reference.png",
                    "mimeType": "image/png",
                    "buffer": PNG_IMAGE,
                }
            )
            await expect(
                dialog.get_by_role("button", name="View goal-reference.png")
            ).to_be_visible()
            await expect(
                dialog.get_by_role("button", name="Start Goal", exact=True)
            ).to_be_enabled()
            await dialog.get_by_role("button", name="Start Goal", exact=True).click()
            await expect(dialog).not_to_be_visible()
            assert len(goal_creates[-1]["attachments"]) == 1
            assert goal_creates[-1]["objective"] == "Build from the attached reference"
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.screenshot(path=str(output / "goal-controls-mobile.png"), full_page=True)
            report["screenshots"].append("goal-controls-mobile.png")

            # The setup dialog carries the remembered "Plan first" choice and
            # sends it as an additive optional field. Clear the previous Goal
            # first: a Chat with a Goal cannot start a second one.
            current_goal = None
            await page.set_viewport_size({"width": 1440, "height": 900})
            await page.reload(wait_until="domcontentloaded")
            await expect(page.get_by_role("button", name="Add to chat", exact=True)).to_be_visible()
            await page.get_by_role("button", name="Add to chat", exact=True).click()
            await page.get_by_role("menuitem", name="Set a Goal").click()
            dialog = page.get_by_role("dialog", name="Set a Goal", exact=True)
            plan_toggle = dialog.get_by_label("Plan first", exact=True)
            await expect(plan_toggle).to_be_checked()
            await dialog.get_by_role("textbox").fill("Plan the cache before building it")
            await expect(dialog.get_by_text("Nothing starts until you approve")).to_be_visible()
            # The dialog fade-in is a 220ms entrance animation; shoot after it.
            await page.wait_for_timeout(400)
            await page.screenshot(path=str(output / "goal-setup-plan-first.png"), full_page=True)
            report["screenshots"].append("goal-setup-plan-first.png")
            # The same open dialog reflows to a phone width.
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.wait_for_timeout(200)
            await expect(plan_toggle).to_be_checked()
            await page.screenshot(
                path=str(output / "goal-setup-plan-first-mobile.png"), full_page=True
            )
            report["screenshots"].append("goal-setup-plan-first-mobile.png")
            await page.set_viewport_size({"width": 1440, "height": 900})
            await dialog.get_by_role("button", name="Start Goal", exact=True).click()
            await expect(dialog).not_to_be_visible()
            assert goal_creates[-1]["plan_first"] is True, goal_creates[-1]
            assert goal_creates[-1]["objective"] == "Plan the cache before building it"

            # A planning turn that parked on needs_input renders the approval
            # card from the Goal snapshot, not from the transcript text.
            current_goal = dict(PLAN_GOAL)
            plan_fixture = True
            await page.reload(wait_until="domcontentloaded")
            await expect(
                page.get_by_text("Scope: add the bounded cache and verify it.")
            ).to_be_visible()
            card = page.locator(".goal-plan-card")
            await expect(card).to_be_visible()
            await expect(card).to_have_attribute("data-mode", "review")
            await expect(card.locator(".goal-plan-eyebrow")).to_have_text("Execution plan")
            await expect(card.locator(".goal-plan-budget")).to_contain_text("Execution budget")
            await expect(card.locator(".goal-plan-budget")).to_contain_text(
                "2 helpers · claude/opus, codex/gpt-5 · max 2 in parallel · about 12 minutes"
            )
            await expect(card.locator(".goal-plan-budget-helpers li")).to_have_count(2)
            await expect(card.locator(".goal-plan-steps li")).to_have_count(2)
            assert "goal-plan" not in await page.locator(".structured-pane").first.inner_text()
            await card.scroll_into_view_if_needed()
            await page.screenshot(path=str(output / "goal-plan-card-desktop.png"), full_page=True)
            report["screenshots"].append("goal-plan-card-desktop.png")

            # Reload at each width: the app chooses its mobile shell from the
            # viewport it boots in, so resizing alone would keep the desktop one.
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.reload(wait_until="domcontentloaded")
            await expect(
                page.get_by_text("Scope: add the bounded cache and verify it.")
            ).to_be_visible()
            card = page.locator(".goal-plan-card")
            await expect(card).to_have_attribute("data-mode", "review")
            await card.scroll_into_view_if_needed()
            approve_button = card.get_by_role("button", name="Approve & start", exact=True)
            await expect(approve_button).to_be_visible()
            assert (await approve_button.bounding_box())["height"] >= 44
            await page.screenshot(path=str(output / "goal-plan-card-mobile.png"), full_page=True)
            report["screenshots"].append("goal-plan-card-mobile.png")

            # Revise only prepares the composer; nothing is sent.
            await page.set_viewport_size({"width": 1440, "height": 900})
            await page.reload(wait_until="domcontentloaded")
            card = page.locator(".goal-plan-card")
            await expect(card).to_have_attribute("data-mode", "review")
            sends_before_revise = len(send_payloads)
            await card.get_by_role("button", name="Revise", exact=True).click()
            await expect(composer).to_have_value("Revise the plan: ")
            assert len(send_payloads) == sends_before_revise
            approve_button = card.get_by_role("button", name="Approve & start", exact=True)

            # Approve sends the fixed approval message on the Goal steer path.
            await composer.fill("")
            await approve_button.click()
            await expect(card).to_have_attribute("data-mode", "waiting")
            await expect(
                card.get_by_role("button", name="Approve & start", exact=True)
            ).not_to_be_visible()
            assert send_payloads[-1]["text"] == "Approve the plan. Start implementing it now."
            assert send_payloads[-1]["delivery"] == "steer"
            assert not any(
                path.endswith("/resume") or path.endswith("/pause")
                for _, path in report["api_requests"]
            )

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
