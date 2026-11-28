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

from playwright.async_api import BrowserContext, expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from appearance_assertions import filled_button_themes  # noqa: E402
from appearance_assertions import select_appearance, wide_chat_themes
from browser_lifecycle import BrowserRun, run_browser_main  # noqa: E402

TAB = "goal-steer-tab"
DONE_TURN = "goal-turn-1"
LIVE_TURN = "goal-turn-2"
PLAN_TURN = "goal-plan-turn"
NOW = "2026-10-12T08:00:00Z"

CONTRAST_SCRIPT = """() => {
  const parse = value => (value.match(/[\d.]+/g) || []).slice(0, 3).map(Number)
  const luminance = channels => {
    const [r, g, b] = channels.map(v => {
      const c = v / 255
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.03928) / 1.055, 2.4)
    })
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
  }
  const ratio = (fg, bg) => {
    const a = luminance(parse(fg))
    const b = luminance(parse(bg))
    const high = Math.max(a, b)
    const low = Math.min(a, b)
    return (high + 0.05) / (low + 0.05)
  }
  const effectiveBackground = element => {
    // Translucent card tints sit on the dialog surface: blend the layers down
    // to the first opaque one so contrast is measured on the colour the eye
    // actually sees.
    let node = element
    const layers = []
    while (node) {
      const raw = getComputedStyle(node).backgroundColor
      const parts = (raw.match(/[\d.]+/g) || []).map(Number)
      if (parts.length >= 3) {
        const alpha = parts.length > 3 ? parts[3] : 1
        if (alpha > 0) layers.push([parts[0], parts[1], parts[2], alpha])
        if (alpha === 1) break
      }
      node = node.parentElement
    }
    if (!layers.length) return 'rgb(255, 255, 255)'
    const base = layers[layers.length - 1].slice(0, 3)
    let out = base.slice()
    for (let i = layers.length - 2; i >= 0; i--) {
      const [r, g, b, a] = layers[i]
      out = [r * a + out[0] * (1 - a), g * a + out[1] * (1 - a), b * a + out[2] * (1 - a)]
    }
    return `rgb(${Math.round(out[0])}, ${Math.round(out[1])}, ${Math.round(out[2])})`
  }
  const out = {}
  for (const [key, selector] of Object.entries({
    title: '#goal-dialog-title',
    label: '.goal-dialog-field-label',
    hint: '.goal-dialog-field-hint',
    note: '.goal-dialog-plan-note',
    tips: '.goal-dialog-tips',
  })) {
    const element = document.querySelector(selector)
    if (!element) continue
    out[key] = ratio(getComputedStyle(element).color, effectiveBackground(element))
  }
  return out
}"""
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
    "plan_answered": False,
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


async def _scenario(context: BrowserContext, *, appearance: bool) -> dict:
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
    approval_fixture = False
    fail_next_send = False
    goal_updates = []
    goal_creates = []
    cross_settings = {
        "preferred_candidates": [],
        "push_remote_branch": False,
        "default_preset": "cross_review_slim",
    }
    settings_get_started = asyncio.Event()
    release_settings_get = asyncio.Event()
    first_settings_get = True
    delay_settings_save = True
    fail_next_settings_get = False
    malformed_next_settings_get = False
    fail_next_settings_save = False
    save_started = asyncio.Event()
    release_save = asyncio.Event()
    failure: BaseException | None = None
    page = None

    try:

        async def block_websocket(socket) -> None:
            report["blocked_websockets"].append(socket.url)
            await socket.close(code=1000, reason="mocked goal smoke")

        async def route_all(route) -> None:
            nonlocal current_goal, fail_next_send, plan_fixture, cross_settings
            nonlocal first_settings_get, delay_settings_save
            nonlocal fail_next_settings_get, malformed_next_settings_get, fail_next_settings_save
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
            elif path in {
                "/api/tabs/status",
                "/api/tabs/archived",
                "/api/workspaces",
                "/api/remote/profiles",
            }:
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
                if approval_fixture:
                    fixture_events.extend(
                        [
                            event(
                                8,
                                "turn_started",
                                "theme-approval",
                                {"summary": "Confirm implementation scope"},
                                "theme-approval:user",
                            ),
                            event(
                                9,
                                "approval_required",
                                "theme-approval",
                                {
                                    "tool_call_id": "theme-approval-call",
                                    "kind": "ask_question",
                                    "questions": [
                                        {
                                            "id": "scope",
                                            "prompt": "Choose the implementation scope",
                                            "options": [
                                                {
                                                    "id": "bounded",
                                                    "label": "Keep scope bounded",
                                                }
                                            ],
                                        }
                                    ],
                                },
                                "theme-approval:question",
                            ),
                        ]
                    )
                rows = [item for item in fixture_events if item["stream_sequence"] > since]
                next_sequence = rows[-1]["stream_sequence"] if rows else since
                body = {
                    "events": rows,
                    "next_sequence": next_sequence,
                    "has_more": False,
                }
            elif request.method == "GET" and path == f"/api/workspaces/tabs/{TAB}/questions":
                body = {"questions": []}
            elif path.endswith("/wait"):
                since = int(parse_qs(parsed.query).get("since_sequence", ["-1"])[0])
                await asyncio.sleep(0.2)
                body = {"events": [], "next_sequence": since, "has_more": False}
            elif path.endswith("/live"):
                await route.fulfill(status=200, content_type="text/event-stream", body=": mock\n\n")
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
                            {"detail": "Image delivery interrupted; reattach images and recover"}
                        ),
                    )
                    return
                if current_goal and current_goal.get("strict_review_in_progress"):
                    payload = send_payloads[-1]
                    current_goal = {
                        **current_goal,
                        "review_steer_turn_ids": current_goal.get("review_steer_turn_ids", [])
                        + [payload["client_turn_id"]],
                        "pending_review_steer": True,
                        "pending_input": "\n".join(
                            text
                            for text in [
                                current_goal.get("pending_input"),
                                payload["text"],
                            ]
                            if text
                        ),
                        "pending_image_count": current_goal.get("pending_image_count", 0)
                        + len(payload.get("attachments", [])),
                        "status_message": "User input queued; waiting for strict cross-model review to finish",
                    }
                elif current_goal:
                    # The approval message is dispatched as the Goal's next
                    # turn, which is what ends the plan's approval request.
                    current_goal = {
                        **current_goal,
                        "status": "active",
                        "dispatch_state": "dispatched",
                        "pending_image_count": 0,
                        "plan_answered": bool(current_goal.get("plan_turn_id")),
                    }
                body = {"ok": True}
            elif path in {
                "/api/goals/cross-review-settings",
                "/api/subagents/settings",
            }:
                if request.method == "GET" and fail_next_settings_get:
                    fail_next_settings_get = False
                    await route.fulfill(
                        status=503,
                        content_type="application/json",
                        body=json.dumps({"detail": "Service unavailable"}),
                    )
                    return
                if request.method == "GET" and malformed_next_settings_get:
                    malformed_next_settings_get = False
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"providers": []}),
                    )
                    return
                if request.method == "GET" and first_settings_get:
                    first_settings_get = False
                    settings_get_started.set()
                    await release_settings_get.wait()
                if request.method == "PUT":
                    if fail_next_settings_save:
                        fail_next_settings_save = False
                        await route.fulfill(
                            status=500,
                            content_type="application/json",
                            body=json.dumps({"detail": "Save failed"}),
                        )
                        return
                    put_payload = json.loads(request.post_data or "{}")
                    raw_preset = (
                        "readonly_brief"
                        if put_payload.get("default_preset") == "readonly_brief"
                        else "cross_review_slim"
                    )
                    push = (
                        False
                        if raw_preset == "readonly_brief"
                        else bool(put_payload.get("push_remote_branch"))
                    )
                    cross_settings = {
                        "preferred_candidates": put_payload.get("preferred_candidates", []),
                        "push_remote_branch": push,
                        "default_preset": "cross_review_slim" if push else raw_preset,
                    }
                    if delay_settings_save:
                        delay_settings_save = False
                        save_started.set()
                        await release_save.wait()
                if request.method == "PATCH":
                    patch_payload = json.loads(request.post_data or "{}")
                    merged = dict(cross_settings)
                    if (
                        "preferred_candidates" in patch_payload
                        and patch_payload["preferred_candidates"] is not None
                    ):
                        merged["preferred_candidates"] = patch_payload["preferred_candidates"]
                    if (
                        "default_preset" in patch_payload
                        and patch_payload["default_preset"] is not None
                    ):
                        merged["default_preset"] = patch_payload["default_preset"]
                        if (
                            patch_payload["default_preset"] == "readonly_brief"
                            and patch_payload.get("push_remote_branch") is None
                        ):
                            merged["push_remote_branch"] = False
                    if (
                        "push_remote_branch" in patch_payload
                        and patch_payload["push_remote_branch"] is not None
                    ):
                        merged["push_remote_branch"] = bool(patch_payload["push_remote_branch"])
                    if merged.get("default_preset") == "readonly_brief":
                        merged["push_remote_branch"] = False
                    cross_settings = merged
                body = {
                    "settings": cross_settings,
                    "providers": [
                        {
                            "provider": "codex",
                            "label": "Codex",
                            "models": ["reviewer-alpha"],
                            "supports_effort": True,
                            "effort_options": [
                                "none",
                                "minimal",
                                "low",
                                "medium",
                                "high",
                                "xhigh",
                                "max",
                                "ultra",
                            ],
                            "quota_state": None,
                        },
                        {
                            "provider": "claude",
                            "label": "Claude",
                            "models": [],
                            "supports_effort": False,
                            "effort_options": [],
                            "quota_state": None,
                        },
                    ],
                }
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
                current_goal = {**GOAL, **creation, "objective": creation["objective"]}
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
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

        await context.route_web_socket("**/*", block_websocket)
        await context.route("**/*", route_all)
        # Match remote HTTP deployments where randomUUID is unavailable.
        await context.add_init_script(
            "Object.defineProperty(globalThis.crypto, 'randomUUID', {value: undefined, configurable: true})"
        )
        page = await context.new_page()
        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
        if appearance:
            current_goal = dict(PLAN_GOAL)
            idle_fixture = True
            plan_fixture = True
        await page.goto(base + f"/?tab={TAB}", wait_until="domcontentloaded")

        if appearance:
            await expect(
                page.get_by_text("Scope: add the bounded cache and verify it.")
            ).to_be_visible()
            card = page.locator(".goal-plan-card")
            await expect(card).to_have_attribute("data-mode", "review")
            await card.scroll_into_view_if_needed()
            off_origin_before_themes = list(report["blocked_external_requests"])
            report["plan_button_theme_contrast"] = await filled_button_themes(
                page,
                card.get_by_role("button", name="Approve & start", exact=True),
                card.get_by_role("button", name="Revise", exact=True),
                screenshot_dir=output,
            )
            report["wide_chat_artwork"] = await wide_chat_themes(
                page,
                output,
                card.get_by_role("button", name="Approve & start", exact=True),
            )
            assert set(report["blocked_external_requests"]) <= set(off_origin_before_themes), (
                "Theme assets attempted new off-origin requests",
                report["blocked_external_requests"],
            )

            # The same full SPA renders the provider's approval-required event
            # through ChatApprovalCard; exercise its real submit fill, not tokens.
            current_goal = None
            approval_fixture = True
            await page.reload(wait_until="domcontentloaded")
            choice = page.get_by_role("button", name="Keep scope bounded", exact=True)
            await expect(choice).to_be_enabled()
            await choice.click()
            submit = page.get_by_role("button", name="Submit answer", exact=True)
            await expect(submit).to_be_enabled()
            sends_before_theme_check = len(send_payloads)
            report["approval_button_theme_contrast"] = await filled_button_themes(page, submit)
            assert len(send_payloads) == sends_before_theme_check, "hover must not submit an answer"
            await page.screenshot(
                path=str(output / "approval-card-theme-hover.png"), full_page=True
            )
            report["screenshots"].append("approval-card-theme-hover.png")

            assert not report["unknown_requests"], report["unknown_requests"]
            assert not report["page_errors"], report["page_errors"]
            report["result"] = "PASS"
            (output / "appearance-report.json").write_text(json.dumps(report, indent=2))
            return report

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
        await page.screenshot(path=str(output / "goal-queued-image-controls.png"), full_page=True)
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

        # The lightweight Cross-Review & Slim dialog opens lazily and uses the shared subagent priority settings.
        current_goal = None
        idle_fixture = True
        delay_settings_save = False
        await page.set_viewport_size({"width": 1440, "height": 900})
        await page.reload(wait_until="domcontentloaded")
        assert not any(
            path in {"/api/goals/cross-review-settings", "/api/subagents/settings"}
            for _, path in report["api_requests"]
        )
        await expect(composer).to_be_enabled()
        await select_appearance(page, "dark", "Classic")
        await page.get_by_role("button", name="Add to chat", exact=True).click()
        await page.get_by_role("menuitem", name="Cross-Review & Slim").click()
        review = page.get_by_role("dialog", name="Cross-Review & Slim")
        await expect(review).to_be_visible()
        await asyncio.wait_for(settings_get_started.wait(), timeout=5)
        # Saved defaults must load before the review can start.
        await expect(review.get_by_role("button", name="Start Cross-Review")).to_be_disabled()
        await expect(review.get_by_label("Push feature branch on PASS")).to_be_disabled()
        await review.get_by_label("Scope / focus notes").fill("Preserve these notes while loading")
        release_settings_get.set()
        await expect(review.get_by_role("button", name="Start Cross-Review")).to_be_enabled()
        await expect(review.get_by_label("Scope / focus notes")).to_have_value(
            "Preserve these notes while loading"
        )
        for group_name in ("Review focus", "Wrap-up option"):
            await expect(review.get_by_role("group", name=group_name, exact=True)).to_be_visible()
        for pillar in (
            "Bugs & correctness",
            "Elegant design",
            "Restraint & slim",
            "Performance",
        ):
            await expect(review.get_by_text(pillar, exact=True)).to_be_visible()
        # Desktop dialog opens at scrollTop=0 with no vertical overflow hiding content or footer.
        desktop_layout = await review.evaluate("""el => {
            const body = el.querySelector('.cr-body')
            const pillars = [...el.querySelectorAll('.cr-pillar')].map(c => c.getBoundingClientRect())
            const footer = el.querySelector('.cr-footer').getBoundingClientRect()
            return {
              scrollTop: body.scrollTop,
              scrollHeight: body.scrollHeight,
              clientHeight: body.clientHeight,
              pillarCount: pillars.length,
              footerBottom: footer.bottom,
            }
        }""")
        assert desktop_layout["scrollTop"] == 0, desktop_layout
        assert (
            desktop_layout["scrollHeight"] <= desktop_layout["clientHeight"] + 1
        ), f"default desktop dialog overflowed vertically: {desktop_layout}"
        assert desktop_layout["pillarCount"] == 4, desktop_layout
        assert desktop_layout["footerBottom"] <= 900, desktop_layout
        await expect(review.locator(".cr-run-summary")).to_contain_text(
            "Cross-Review & Slim · Auto heterogeneous routing"
        )
        cross_contrast = (
            CONTRAST_SCRIPT.replace("#goal-dialog-title", "#cross-review-title")
            .replace(".goal-dialog-field-label", ".dialog legend")
            .replace(".goal-dialog-plan-note", ".goal-dialog-count")
            .replace(".goal-dialog-tips", ".goal-dialog-button--primary")
        )
        # Clearing scope notes surfaces actionable validation feedback and a 1-click default reset.
        await review.get_by_label("Scope / focus notes").fill("   ")
        await expect(review.get_by_role("button", name="Start Cross-Review")).to_be_disabled()
        await expect(
            review.get_by_text("Scope / focus notes are required to start a review.", exact=False)
        ).to_be_visible()
        dark_warning_contrast = await page.evaluate(cross_contrast)
        assert dark_warning_contrast and all(
            value >= 4.5 for value in dark_warning_contrast.values()
        ), dark_warning_contrast
        await review.get_by_role("button", name="Reset to default").click()
        await expect(review.get_by_label("Scope / focus notes")).to_have_value(
            "Cross-review the current change for bugs and correctness, elegant and reasonable design, restraint against overengineering or excessive defensive code, and performance; simplify where justified and verify."
        )
        await expect(review.get_by_role("button", name="Start Cross-Review")).to_be_enabled()
        await review.get_by_label("Scope / focus notes").fill("x" * 3700)
        await expect(review.locator(".goal-dialog-count")).to_have_attribute(
            "data-near-limit", "true"
        )
        dark_near_limit_contrast = await page.evaluate(cross_contrast)
        assert dark_near_limit_contrast and all(
            value >= 4.5 for value in dark_near_limit_contrast.values()
        ), dark_near_limit_contrast
        await review.get_by_label("Scope / focus notes").fill("Preserve these notes while loading")
        await review.get_by_role("button", name="Start Cross-Review").focus()
        await page.keyboard.press("Tab")
        await expect(review.get_by_role("button", name="Configure in Settings")).to_be_focused()
        await page.keyboard.press("Shift+Tab")
        await expect(review.get_by_role("button", name="Start Cross-Review")).to_be_focused()
        measured = await page.evaluate(cross_contrast)
        assert measured and all(value >= 4.5 for value in measured.values()), measured
        await page.screenshot(path=str(output / "cross-review-dark.png"), full_page=True)
        report["screenshots"].append("cross-review-dark.png")

        # F6: GET 503 on Subagent Priority settings surfaces alert, disables Save priority, and allows Retry load.
        fail_next_settings_get = True
        await review.get_by_role("button", name="Configure in Settings").click()
        subagent_dialog = page.get_by_role("dialog", name="Subagent Priority")
        await expect(subagent_dialog).to_be_visible()
        await expect(
            subagent_dialog.get_by_text(
                "Could not load saved subagent priority. Retry before saving changes."
            )
        ).to_be_visible()
        await expect(subagent_dialog.get_by_role("button", name="Save priority")).to_be_disabled()
        await expect(subagent_dialog.get_by_role("button", name="Add priority")).to_be_disabled()
        await page.wait_for_timeout(250)
        await page.screenshot(
            path=str(output / "subagent-priority-settings-error.png"), full_page=True
        )
        report["screenshots"].append("subagent-priority-settings-error.png")
        await subagent_dialog.get_by_role("button", name="Retry load").click()
        await expect(subagent_dialog.get_by_role("button", name="Save priority")).to_be_enabled()

        # Open global Subagent Priority settings from Cross-Review & Slim and configure shared priority.
        await subagent_dialog.get_by_label("Provider", exact=True).select_option("claude")
        await expect(subagent_dialog.get_by_label("Effort", exact=True)).to_be_disabled()
        await subagent_dialog.get_by_label("Provider", exact=True).select_option("codex")
        await expect(subagent_dialog.get_by_label("Effort", exact=True)).to_be_enabled()
        await subagent_dialog.get_by_label("Model", exact=True).select_option("reviewer-alpha")
        await subagent_dialog.get_by_label("Effort", exact=True).select_option("ultra")
        await subagent_dialog.get_by_role("button", name="Add priority").click()
        await expect(subagent_dialog.locator(".candidate")).to_have_count(1)
        await subagent_dialog.get_by_role("button", name="Save priority").click()
        await expect(subagent_dialog.get_by_text("Subagent priority saved.")).to_be_visible()
        assert cross_settings["preferred_candidates"] == [
            {"provider": "codex", "model": "reviewer-alpha", "effort": "ultra"}
        ]
        await page.screenshot(path=str(output / "subagent-priority-settings.png"), full_page=True)
        report["screenshots"].append("subagent-priority-settings.png")

        # F5: Adding an unsaved second candidate and closing Settings must NOT leak into Cross-Review & Slim or persist on push toggle.
        await subagent_dialog.get_by_label("Provider", exact=True).select_option("claude")
        await subagent_dialog.get_by_role("button", name="Add priority").click()
        await expect(subagent_dialog.locator(".candidate")).to_have_count(2)
        await subagent_dialog.get_by_role("button", name="Close").click()
        await expect(subagent_dialog).not_to_be_visible()

        # Cross-Review & Slim reflects ONLY the committed 1 candidate, not the unsaved 2nd candidate.
        await expect(review.locator(".cr-priority-summary")).to_have_text(
            "#1 Codex/reviewer-alpha (ultra)"
        )
        await expect(review.locator(".cr-run-summary")).to_contain_text(
            "Cross-Review & Slim · 1 shared priority subagent"
        )
        await review.get_by_label("Push feature branch on PASS").check()
        await expect(review.locator(".cr-run-summary")).to_contain_text(
            "Cross-Review & Slim · 1 shared priority subagent · Push on PASS"
        )
        assert cross_settings["preferred_candidates"] == [
            {"provider": "codex", "model": "reviewer-alpha", "effort": "ultra"}
        ]
        await review.get_by_label("Push feature branch on PASS").uncheck()
        assert cross_settings["push_remote_branch"] is False

        # F7: 120-character custom model ID wraps cleanly at 390x844 mobile viewport without horizontal overflow.
        long_model_id = "custom-model-" + ("x" * 107)
        assert len(long_model_id) == 120
        await review.get_by_role("button", name="Configure in Settings").click()
        await expect(subagent_dialog).to_be_visible()
        await subagent_dialog.get_by_label("Model", exact=True).select_option("__custom__")
        await subagent_dialog.get_by_label("Custom model ID", exact=True).fill(long_model_id)
        await subagent_dialog.get_by_role("button", name="Add priority").click()
        await subagent_dialog.get_by_role("button", name="Save priority").click()
        await expect(subagent_dialog.get_by_text("Subagent priority saved.")).to_be_visible()
        await page.set_viewport_size({"width": 390, "height": 844})
        settings_mobile_overflow = await subagent_dialog.evaluate("""el => {
            const body = el.querySelector('.sa-body')
            return { clientWidth: body.clientWidth, scrollWidth: body.scrollWidth }
        }""")
        assert (
            settings_mobile_overflow["scrollWidth"] <= settings_mobile_overflow["clientWidth"] + 1
        ), f"SubagentSettingsDialog overflowed at 390px with 120-char model: {settings_mobile_overflow}"
        await subagent_dialog.get_by_role("button", name="Close").click()
        await expect(subagent_dialog).not_to_be_visible()

        long_model_cr_overflow = await review.evaluate("""el => {
            const body = el.querySelector('.cr-body')
            const summary = el.querySelector('.cr-priority-summary').getBoundingClientRect()
            return {
              clientWidth: body.clientWidth,
              scrollWidth: body.scrollWidth,
              summaryRight: summary.right,
            }
        }""")
        assert (
            long_model_cr_overflow["scrollWidth"] <= long_model_cr_overflow["clientWidth"] + 1
            and long_model_cr_overflow["summaryRight"] <= 391
        ), f"CrossReviewDialog overflowed at 390px with 120-char model: {long_model_cr_overflow}"

        # Restore single reviewer-alpha candidate for clean light/dark/mobile screenshots.
        await page.set_viewport_size({"width": 1440, "height": 900})
        await review.get_by_role("button", name="Configure in Settings").click()
        await expect(subagent_dialog).to_be_visible()
        await subagent_dialog.get_by_role("button", name="Remove subagent 2").click()
        await subagent_dialog.get_by_role("button", name="Save priority").click()
        await expect(subagent_dialog.get_by_text("Subagent priority saved.")).to_be_visible()
        await page.set_viewport_size({"width": 390, "height": 844})
        await subagent_dialog.evaluate("""el => {
            const body = el.querySelector('.sa-body')
            if (body) body.scrollTop = 0
            if (document.activeElement instanceof HTMLElement) document.activeElement.blur()
        }""")
        await page.wait_for_timeout(200)
        await page.screenshot(
            path=str(output / "subagent-priority-settings-mobile.png"), full_page=True
        )
        report["screenshots"].append("subagent-priority-settings-mobile.png")
        await page.set_viewport_size({"width": 1440, "height": 900})
        await subagent_dialog.get_by_role("button", name="Close").click()
        await expect(subagent_dialog).not_to_be_visible()

        # R2: Two-page conflict-safe regression — Page A opens Cross-Review & Slim with 1 candidate,
        # Page B saves 2 candidates in Subagent Priority Settings, then Page A checks Push on PASS.
        # Narrow PATCH from Page A must preserve Page B's 2 candidates.
        page_b = await context.new_page()
        await page_b.goto(base + f"/?tab={TAB}", wait_until="domcontentloaded")
        await expect(page_b.get_by_role("button", name="Add to chat", exact=True)).to_be_visible()
        await page_b.get_by_role("button", name="Add to chat", exact=True).click()
        await page_b.get_by_role("menuitem", name="Cross-Review & Slim").click()
        review_b = page_b.get_by_role("dialog", name="Cross-Review & Slim")
        await expect(review_b).to_be_visible()
        await review_b.get_by_role("button", name="Configure in Settings").click()
        subagent_b = page_b.get_by_role("dialog", name="Subagent Priority")
        await expect(subagent_b).to_be_visible()
        await subagent_b.get_by_label("Provider", exact=True).select_option("claude")
        await subagent_b.get_by_role("button", name="Add priority").click()
        await expect(subagent_b.locator(".candidate")).to_have_count(2)
        await subagent_b.get_by_role("button", name="Save priority").click()
        await expect(subagent_b.get_by_text("Subagent priority saved.")).to_be_visible()
        assert len(cross_settings["preferred_candidates"]) == 2
        await page_b.close()

        # Now Page A (which loaded when there was only 1 candidate) toggles Push feature branch on PASS:
        await review.get_by_label("Push feature branch on PASS").check()
        await expect(review.locator(".cr-run-summary")).to_contain_text(
            "Cross-Review & Slim · 2 shared priority subagents · Push on PASS"
        )
        assert len(cross_settings["preferred_candidates"]) == 2
        assert cross_settings["push_remote_branch"] is True
        await review.get_by_label("Push feature branch on PASS").uncheck()
        assert cross_settings["push_remote_branch"] is False
        assert len(cross_settings["preferred_candidates"]) == 2

        # S1: Stale-mode regression — if another tab/client saves readonly_brief while Page A
        # still has Cross-Review & Slim open, a push-only PATCH from Page A must NOT switch
        # the saved mode to cross_review_slim; Page A reconciles to Read-Only Audit.
        cross_settings["default_preset"] = "readonly_brief"
        cross_settings["push_remote_branch"] = False
        await review.get_by_label("Push feature branch on PASS").check()
        await expect(
            review.get_by_text(
                "Saved read-only audit default active (no file edits or branch push)."
            )
        ).to_be_visible()
        await expect(review.get_by_label("Push feature branch on PASS")).not_to_be_checked()
        await expect(review.get_by_label("Push feature branch on PASS")).to_be_disabled()
        assert cross_settings["default_preset"] == "readonly_brief"
        assert cross_settings["push_remote_branch"] is False
        assert len(cross_settings["preferred_candidates"]) == 2

        # Keep 1 candidate for the remaining single-candidate assertions/screenshots.
        cross_settings = {
            "preferred_candidates": [
                {"provider": "codex", "model": "reviewer-alpha", "effort": "ultra"}
            ],
            "push_remote_branch": False,
            "default_preset": "readonly_brief",
        }
        await page.keyboard.press("Escape")
        await expect(review).not_to_be_visible()

        # R1: Failed (503) and malformed (200 without settings) hydration on Cross-Review & Slim
        # must block Start Cross-Review and direct form submission until Retry load succeeds,
        # preserving saved readonly_brief authorization.
        prior_create_count = len(goal_creates)
        fail_next_settings_get = True
        await page.get_by_role("button", name="Add to chat", exact=True).click()
        await page.get_by_role("menuitem", name="Cross-Review & Slim").click()
        await expect(review).to_be_visible()
        await expect(
            review.get_by_text(
                "Could not load saved subagent settings. Retry before starting a review."
            )
        ).to_be_visible()
        await expect(review.get_by_role("button", name="Start Cross-Review")).to_be_disabled()
        await expect(review.get_by_label("Push feature branch on PASS")).to_be_disabled()
        await expect(review.locator(".cr-run-summary")).to_contain_text(
            "Saved defaults unavailable — retry load to start"
        )
        # Direct form submit event while loadError=True must not emit or create a Goal.
        await review.evaluate(
            "form => form.dispatchEvent(new Event('submit', { cancelable: true }))"
        )
        assert len(goal_creates) == prior_create_count

        # Next Retry load returns malformed 200 (missing settings) -> still blocked.
        malformed_next_settings_get = True
        await review.get_by_role("button", name="Retry load").click()
        await expect(
            review.get_by_text(
                "Could not load saved subagent settings. Retry before starting a review."
            )
        ).to_be_visible()
        await expect(review.get_by_role("button", name="Start Cross-Review")).to_be_disabled()
        await review.evaluate(
            "form => form.dispatchEvent(new Event('submit', { cancelable: true }))"
        )
        assert len(goal_creates) == prior_create_count

        # Retry load now succeeds and restores saved readonly_brief authorization.
        await review.get_by_role("button", name="Retry load").click()
        await expect(
            review.get_by_text(
                "Saved read-only audit default active (no file edits or branch push)."
            )
        ).to_be_visible()
        await expect(review.get_by_label("Push feature branch on PASS")).to_be_disabled()
        await expect(review.locator(".cr-run-summary")).to_contain_text(
            "Read-Only Audit · 1 shared priority subagent"
        )
        await review.get_by_role("button", name="Start Cross-Review").click()
        await expect(review).not_to_be_visible()
        assert goal_creates[-1]["strict_review"] is True
        assert goal_creates[-1]["strict_review_preset"] == "readonly_brief"
        assert goal_creates[-1]["strict_review_push_branch"] is False
        await expect(page.locator(".goal-strict-pill")).to_contain_text("Read-Only Audit")

        # Now reopen, switch from legacy readonly_brief to Cross-Review & Slim, and verify light & mobile dark views.
        current_goal = None
        await page.reload(wait_until="domcontentloaded")
        await expect(composer).to_be_enabled()
        await select_appearance(page, "light", "Classic")
        await page.get_by_role("button", name="Add to chat", exact=True).click()
        await page.get_by_role("menuitem", name="Cross-Review & Slim").click()
        await expect(review.get_by_role("button", name="Start Cross-Review")).to_be_enabled()
        await review.get_by_role("button", name="Switch to Cross-Review & Slim").click()
        await expect(
            review.get_by_text(
                "Saved read-only audit default active (no file edits or branch push)."
            )
        ).not_to_be_visible()
        assert cross_settings["default_preset"] == "cross_review_slim"
        await review.get_by_role("button", name="Configure in Settings").click()
        await expect(subagent_dialog).to_be_visible()
        await page.wait_for_timeout(250)
        await page.screenshot(
            path=str(output / "subagent-priority-settings-light.png"), full_page=True
        )
        report["screenshots"].append("subagent-priority-settings-light.png")
        await subagent_dialog.get_by_role("button", name="Close").click()
        await expect(subagent_dialog).not_to_be_visible()
        light_contrast = await page.evaluate(cross_contrast)
        assert light_contrast and all(
            value >= 4.5 for value in light_contrast.values()
        ), light_contrast
        await review.get_by_label("Scope / focus notes").fill("   ")
        light_warning_contrast = await page.evaluate(cross_contrast)
        assert light_warning_contrast and all(
            value >= 4.5 for value in light_warning_contrast.values()
        ), light_warning_contrast
        await review.get_by_label("Scope / focus notes").fill("x" * 3700)
        light_near_limit_contrast = await page.evaluate(cross_contrast)
        assert light_near_limit_contrast and all(
            value >= 4.5 for value in light_near_limit_contrast.values()
        ), light_near_limit_contrast
        await review.get_by_role("button", name="Reset to default").click()
        await page.screenshot(path=str(output / "cross-review-light.png"), full_page=True)
        report["screenshots"].append("cross-review-light.png")

        # Switch back to dark mode before taking the mobile dark screenshot and wait for settled geometry (F8).
        await page.keyboard.press("Escape")
        await expect(review).not_to_be_visible()
        await select_appearance(page, "dark", "Classic")
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.get_by_role("button", name="Add to chat", exact=True).click()
        await page.get_by_role("menuitem", name="Cross-Review & Slim").click()
        await expect(review.get_by_role("button", name="Start Cross-Review")).to_be_enabled()
        await review.get_by_label("Scope / focus notes").fill("Mobile custom scope notes")
        # Reset scrollTop to 0 and blur textarea so the mobile screenshot shows a clean, settled default state.
        await review.evaluate("""el => {
            const body = el.querySelector('.cr-body')
            if (body) body.scrollTop = 0
            if (document.activeElement instanceof HTMLElement) document.activeElement.blur()
        }""")
        await page.wait_for_timeout(150)
        mobile_probe = await review.evaluate("""el => {
            const body = el.querySelector('.cr-body')
            const footer = el.querySelector('.cr-footer').getBoundingClientRect()
            const dialogRect = el.getBoundingClientRect()
            const selectors = [
              '.goal-dialog-button',
              '.cr-btn',
              '.cr-chip',
              '.cr-reset-btn',
            ]
            const controls = selectors.flatMap(sel =>
              [...el.querySelectorAll(sel)].map(node => {
                const r = node.getBoundingClientRect()
                return {
                  sel,
                  text: (node.textContent || '').trim().slice(0, 32),
                  width: r.width,
                  height: r.height,
                  left: r.left,
                  right: r.right,
                }
              })
            )
            return {
              clientWidth: body.clientWidth,
              scrollWidth: body.scrollWidth,
              scrollTop: body.scrollTop,
              dialogTop: dialogRect.top,
              dialogBottom: dialogRect.bottom,
              footerBottom: footer.bottom,
              controls,
            }
        }""")
        assert (
            mobile_probe["scrollWidth"] <= mobile_probe["clientWidth"] + 1
        ), f"mobile body overflowed horizontally: {mobile_probe}"
        assert mobile_probe["scrollTop"] == 0, f"mobile body remained scrolled: {mobile_probe}"
        assert (
            mobile_probe["dialogBottom"] <= 845 and mobile_probe["footerBottom"] <= 845
        ), f"mobile dialog not settled inside 844px viewport: {mobile_probe}"
        for ctrl in mobile_probe["controls"]:
            assert (
                ctrl["height"] >= 44 and ctrl["width"] >= 44
            ), f"mobile control below 44px target: {ctrl}"
            assert (
                ctrl["left"] >= -1 and ctrl["right"] <= 391
            ), f"mobile control outside 390px viewport: {ctrl}"
        await page.screenshot(path=str(output / "cross-review-mobile.png"), full_page=True)
        report["screenshots"].append("cross-review-mobile.png")
        await page.set_viewport_size({"width": 1440, "height": 900})
        await review.get_by_label("Push feature branch on PASS").check()
        await expect(review.locator(".cr-run-summary")).to_contain_text(
            "Cross-Review & Slim · 1 shared priority subagent · Push on PASS"
        )
        await review.get_by_role("button", name="Start Cross-Review").click()
        await expect(review).not_to_be_visible()
        assert goal_creates[-1]["strict_review"] is True
        assert goal_creates[-1]["strict_review_preset"] == "cross_review_slim"
        assert goal_creates[-1]["strict_review_push_branch"] is True
        assert "strict_review_candidates" not in goal_creates[-1]
        assert "strict_review_mode" not in goal_creates[-1]
        await expect(page.locator(".goal-strict-pill")).to_contain_text("Cross-Review & Slim")

        # Goal creation includes composer images in its first request.
        current_goal = None
        idle_fixture = True
        await page.reload(wait_until="domcontentloaded")
        await expect(composer).to_be_enabled()
        await page.get_by_role("button", name="Add to chat", exact=True).click()
        await page.get_by_role("menuitem", name="Set a Goal").click()
        dialog = page.get_by_role("dialog", name="Set a Goal", exact=True)
        described_by = await dialog.get_by_role("textbox").get_attribute("aria-describedby")
        assert described_by, "the objective field lost its hint association"
        assert await dialog.locator(f"#{described_by}").count() == 1, described_by
        await dialog.get_by_role("textbox").fill("Build from the attached reference")
        await page.locator('input[type="file"]').set_input_files(
            {
                "name": "goal-reference.png",
                "mimeType": "image/png",
                "buffer": PNG_IMAGE,
            }
        )
        await expect(dialog.get_by_role("button", name="View goal-reference.png")).to_be_visible()
        # The attachment row is the parent's; the dialog may frame it but
        # must not restyle the chip preview or its remove badge.
        chip = await dialog.locator(".attachment-chip").first.bounding_box()
        remove = await dialog.locator(".attachment-remove").first.bounding_box()
        trigger = await dialog.get_by_role("button", name="Add images").bounding_box()
        assert chip and chip["width"] <= 48.5 and chip["height"] <= 48.5, chip
        assert remove and remove["height"] <= 19, f"remove badge was restyled: {remove}"
        assert remove["width"] <= 19, remove
        assert trigger and trigger["height"] >= 30, f"the picker trigger shrank: {trigger}"
        # The row's composition is the picker's own: caption above, trigger
        # and chips side by side. Reverting that layout must fail here.
        caption = await dialog.locator(".goal-image-picker > p").first.bounding_box()
        assert caption, "the picker caption is missing"
        assert caption["y"] + caption["height"] <= trigger["y"] + 2, (caption, trigger)
        assert (
            abs((chip["y"] + chip["height"] / 2) - (trigger["y"] + trigger["height"] / 2)) <= 6
        ), (
            chip,
            trigger,
        )
        await page.screenshot(path=str(output / "goal-setup-attachment.png"), full_page=True)
        report["screenshots"].append("goal-setup-attachment.png")
        await expect(dialog.get_by_role("button", name="Start Goal", exact=True)).to_be_enabled()
        await dialog.get_by_role("button", name="Start Goal", exact=True).click()
        await expect(dialog).not_to_be_visible()
        assert len(goal_creates[-1]["attachments"]) == 1
        assert goal_creates[-1]["objective"] == "Build from the attached reference"
        # Nothing asked for approval, so nothing sends the gate either;
        # Strict cross-review opens checked by default.
        assert goal_creates[-1]["plan_first"] is False, goal_creates[-1]
        assert goal_creates[-1]["strict_review"] is True, goal_creates[-1]
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
        strict_toggle = dialog.get_by_label("Strict cross-review", exact=True)
        # No approval gate unless asked for: a new Goal starts working, so
        # the option opens unchecked and says so. Strict cross-review opens checked.
        await expect(plan_toggle).not_to_be_checked()
        await expect(strict_toggle).to_be_checked()
        await expect(dialog.get_by_text("Consensus gate", exact=True)).to_be_visible()
        await expect(dialog.get_by_text("starts working immediately")).to_be_visible()
        await dialog.get_by_role("textbox").fill("Plan the cache before building it")
        # Opting in is what turns the review gate (and its card) on.
        await plan_toggle.check()
        await expect(plan_toggle).to_be_checked()
        await expect(dialog.get_by_text("Nothing starts until you approve")).to_be_visible()
        # The dialog fade-in is a 220ms entrance animation; shoot after it.
        await page.wait_for_timeout(400)
        await page.screenshot(path=str(output / "goal-setup-plan-first.png"), full_page=True)
        report["screenshots"].append("goal-setup-plan-first.png")
        # Both modes must stay readable. Close the Goal modal before using
        # the real Appearance dialog; changing mode must retain the palette.
        # Contrast walks the real computed colours for the effective backdrop.
        setup_palette = await page.locator("html").get_attribute("data-palette")
        assert setup_palette is not None
        dark_ratio = await dialog.evaluate(CONTRAST_SCRIPT)
        assert dark_ratio["title"] >= 4.5, dark_ratio
        assert dark_ratio["label"] >= 4.5, dark_ratio
        assert dark_ratio["hint"] >= 4.5, dark_ratio
        assert dark_ratio["note"] >= 4.5, dark_ratio
        assert dark_ratio["tips"] >= 4.5, dark_ratio
        await dialog.get_by_role("button", name="Cancel", exact=True).click()
        await expect(dialog).not_to_be_visible()
        await select_appearance(page, "light", setup_palette)
        await page.get_by_role("button", name="Add to chat", exact=True).click()
        await page.get_by_role("menuitem", name="Set a Goal").click()
        dialog = page.get_by_role("dialog", name="Set a Goal", exact=True)
        plan_toggle = dialog.get_by_label("Plan first", exact=True)
        # A cancelled dialog remembered nothing, so the light pass starts
        # from the same resting state.
        await expect(plan_toggle).not_to_be_checked()
        await dialog.get_by_role("textbox").fill("Plan the cache before building it")
        await plan_toggle.check()
        await page.wait_for_timeout(300)
        light_ratio = await dialog.evaluate(CONTRAST_SCRIPT)
        assert light_ratio["title"] >= 4.5, light_ratio
        assert light_ratio["label"] >= 4.5, light_ratio
        assert light_ratio["hint"] >= 4.5, light_ratio
        assert light_ratio["note"] >= 4.5, light_ratio
        assert light_ratio["tips"] >= 4.5, light_ratio
        await page.screenshot(path=str(output / "goal-setup-plan-first-light.png"), full_page=True)
        report["screenshots"].append("goal-setup-plan-first-light.png")
        await dialog.get_by_role("button", name="Cancel", exact=True).click()
        await expect(dialog).not_to_be_visible()
        await select_appearance(page, "dark", setup_palette)
        await page.get_by_role("button", name="Add to chat", exact=True).click()
        await page.get_by_role("menuitem", name="Set a Goal").click()
        dialog = page.get_by_role("dialog", name="Set a Goal", exact=True)
        plan_toggle = dialog.get_by_label("Plan first", exact=True)
        await expect(plan_toggle).not_to_be_checked()
        await dialog.get_by_role("textbox").fill("Plan the cache before building it")
        await expect(dialog.locator(".goal-dialog-count")).to_have_attribute(
            "data-near-limit", "false"
        )
        # The counter warns before the hard limit, not only at it.
        await dialog.get_by_role("textbox").fill("x" * 3700)
        await expect(dialog.locator(".goal-dialog-count")).to_have_attribute(
            "data-near-limit", "true"
        )
        await expect(dialog.locator(".goal-dialog-count")).to_have_text("3700/4000")
        await dialog.get_by_role("textbox").fill("Plan the cache before building it")
        await plan_toggle.check()
        await page.wait_for_timeout(300)
        # The same open dialog reflows to a phone width.
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.wait_for_timeout(200)
        await expect(plan_toggle).to_be_checked()
        await page.screenshot(path=str(output / "goal-setup-plan-first-mobile.png"), full_page=True)
        # The phone sheet is bounded by the shared overlay rule, which reads
        # the visual viewport so an on-screen keyboard shrinks it instead of
        # hiding the footer. A desktop-only `100dvh` must fail this.
        sheet_hint = await dialog.evaluate("""el => {
                const root = document.documentElement
                root.style.setProperty('--visual-viewport-height', '480px')
                root.style.setProperty('--keyboard-height', '364px')
                const box = el.getBoundingClientRect()
                return { height: box.height, bottom: box.bottom, top: box.top }
            }""")
        assert sheet_hint["top"] >= -1, sheet_hint
        assert sheet_hint["height"] <= 480 - 24 + 1, sheet_hint
        assert sheet_hint["bottom"] <= 480 + 1, sheet_hint
        start_button = await dialog.get_by_role(
            "button", name="Start Goal", exact=True
        ).bounding_box()
        assert start_button, "the sheet footer lost its actions"
        assert start_button["y"] + start_button["height"] <= 480 + 1, start_button
        # Also verify with a panned visualViewport (offsetTop: 60px, height: 420px)
        # that the GoalSetupDialog stays within [60, 480] and focusing the objective
        # textarea keeps it scrolled into view inside .goal-dialog-body.
        panned_goal_sheet = await dialog.evaluate("""async el => {
                const root = document.documentElement
                root.style.setProperty('--visual-viewport-offset-top', '60px')
                root.style.setProperty('--visual-viewport-height', '420px')
                root.style.setProperty('--keyboard-height', '364px')
                const box = el.getBoundingClientRect()
                const textarea = el.querySelector('#goal-dialog-objective')
                const body = el.querySelector('.goal-dialog-body')
                body.scrollTop = body.scrollHeight
                textarea.focus({ preventScroll: true })
                textarea.dispatchEvent(new FocusEvent('focusin', { bubbles: true }))
                await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))
                const taBox = textarea.getBoundingClientRect()
                const bodyBox = body.getBoundingClientRect()
                const headBox = el.querySelector('.goal-dialog-head').getBoundingClientRect()
                const footBox = el.querySelector('.goal-dialog-actions').getBoundingClientRect()
                return {
                  top: box.top,
                  bottom: box.bottom,
                  taTop: taBox.top,
                  taBottom: taBox.bottom,
                  bodyTop: bodyBox.top,
                  bodyBottom: bodyBox.bottom,
                  headBottom: headBox.bottom,
                  footTop: footBox.top,
                }
            }""")
        assert panned_goal_sheet["top"] >= 60 - 1, panned_goal_sheet
        assert abs(panned_goal_sheet["bottom"] - 480) <= 1, panned_goal_sheet
        assert panned_goal_sheet["taTop"] >= panned_goal_sheet["bodyTop"] - 1, panned_goal_sheet
        assert panned_goal_sheet["taBottom"] <= panned_goal_sheet["footTop"] + 1, panned_goal_sheet
        await page.screenshot(path=str(output / "goal-setup-keyboard-sheet.png"))
        report["screenshots"].append("goal-setup-keyboard-sheet.png")
        await page.evaluate("""() => {
                const root = document.documentElement
                root.style.removeProperty('--visual-viewport-offset-top')
                root.style.removeProperty('--visual-viewport-height')
                root.style.removeProperty('--keyboard-height')
            }""")
        report["screenshots"].append("goal-setup-plan-first-mobile.png")
        await page.set_viewport_size({"width": 1440, "height": 900})
        strict_toggle = dialog.get_by_label("Strict cross-review", exact=True)
        await strict_toggle.uncheck()
        await expect(strict_toggle).not_to_be_checked()
        await dialog.get_by_role("button", name="Start Goal", exact=True).click()
        await expect(dialog).not_to_be_visible()
        assert goal_creates[-1]["plan_first"] is True, goal_creates[-1]
        assert goal_creates[-1]["strict_review"] is False, goal_creates[-1]
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
        # The dispatched turn is still running, so the card must not claim
        # the Goal continued yet.
        await expect(card).to_have_attribute("data-mode", "continuing")
        await expect(card.locator(".goal-plan-note")).to_have_text(
            "The Goal is continuing with this plan during its current turn."
        )
        await expect(
            card.get_by_role("button", name="Approve & start", exact=True)
        ).not_to_be_visible()
        assert send_payloads[-1]["text"] == "Approve the plan. Start implementing it now."
        assert send_payloads[-1]["delivery"] == "steer"
        await page.screenshot(path=str(output / "goal-plan-card-continuing.png"), full_page=True)
        report["screenshots"].append("goal-plan-card-continuing.png")

        # Once that turn settles the same card reports the Goal continued.
        current_goal = {
            **PLAN_GOAL,
            "status": "active",
            "dispatch_state": "idle",
            "plan_answered": True,
        }
        await page.reload(wait_until="domcontentloaded")
        card = page.locator(".goal-plan-card")
        await expect(card).to_have_attribute("data-mode", "answered")
        await expect(card.locator(".goal-plan-note")).to_have_text(
            "The Goal continued after this plan."
        )
        await page.screenshot(path=str(output / "goal-plan-card-answered.png"), full_page=True)
        report["screenshots"].append("goal-plan-card-answered.png")
        assert not any(
            path.endswith("/resume") or path.endswith("/pause")
            for _, path in report["api_requests"]
        )

        # Review-time input is acknowledged without a provider turn. Multiple
        # receipts must release optimistic bubbles, while the Goal details show
        # the retained messages/images rather than a false recovery warning.
        plan_fixture = False
        idle_fixture = True
        current_goal = {
            **GOAL,
            "strict_review": True,
            "strict_review_in_progress": True,
            "current_turn_id": None,
            "completed_turn_ids": [DONE_TURN, LIVE_TURN],
        }
        await page.reload(wait_until="domcontentloaded")
        composer = page.locator(".composer-textarea").first
        await composer.fill("Review question one")
        await composer.press("Meta+Enter")
        await expect(composer).to_have_value("")
        # Empty text is optimistic. Wait for the receipt before attaching:
        # attachment preparation deliberately rejects input during a send.
        await expect(composer).to_be_enabled()
        await page.locator('input[type="file"]').set_input_files(
            {"name": "review.png", "mimeType": "image/png", "buffer": PNG_IMAGE}
        )
        await expect(page.locator(".composer-attachments .attachment-thumb")).to_have_count(1)
        await composer.fill("Review question two")
        await composer.press("Meta+Enter")
        await expect(composer).to_have_value("")
        await page.locator(".goal-status-summary").click()
        details = page.locator(".goal-status-details-body")
        await expect(details).to_contain_text("Review question one")
        await expect(details).to_contain_text("Review question two")
        await expect(details).to_contain_text("(1 images)")
        await expect(details).not_to_contain_text("Image delivery was interrupted")
        assert len(current_goal["review_steer_turn_ids"]) == 2
        assert current_goal["strict_review_in_progress"]
        await page.screenshot(path=str(output / "goal-review-queued-input.png"), full_page=True)
        report["screenshots"].append("goal-review-queued-input.png")
        await page.locator(".goal-status-summary").click()
        report["review_queue"] = "PASS"

        assert not report["unknown_requests"], report["unknown_requests"]
        assert not report["page_errors"], report["page_errors"]
        report["result"] = "PASS"
    except BaseException as exc:
        failure = exc
        report["error_type"] = type(exc).__name__
        if page is not None:
            try:
                await page.screenshot(
                    path=str(output / ("appearance-failure.png" if appearance else "failure.png")),
                    full_page=True,
                )
            except Exception:
                pass

    (output / ("appearance-report.json" if appearance else "behavior-report.json")).write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    if failure is not None:
        raise failure
    return report


async def main(browser_run: BrowserRun) -> None:
    # Keep the original runner deadline and every assertion/screenshot. Separate
    # localStorage and mock state let independent behavior and theme checks run
    # concurrently instead of exhausting the deadline serially on a slow runner.
    async with browser_run.context(
        viewport={"width": 1280, "height": 900},
        launch_args=["--disable-background-networking", "--disable-component-update"],
    ) as context:
        assert context.browser is not None
        appearance_context = await context.browser.new_context(
            viewport={"width": 1440, "height": 900}, service_workers="block"
        )
        try:
            async with asyncio.TaskGroup() as tasks:
                behavior = tasks.create_task(_scenario(context, appearance=False))
                appearance = tasks.create_task(_scenario(appearance_context, appearance=True))
        finally:
            # The BrowserRun owner also closes the browser if cancellation
            # interrupts this close; no context or driver has a separate owner.
            await appearance_context.close()
    report = behavior.result()
    for key, value in appearance.result().items():
        if isinstance(value, list) and isinstance(report.get(key), list):
            report[key].extend(value)
        else:
            report[key] = value
    report["phases"] = {"behavior": "PASS", "appearance": "PASS"}
    (Path(sys.argv[2]) / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    run_browser_main(main)
