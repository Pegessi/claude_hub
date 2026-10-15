"""Full-app Chat composer acceptance with every API request mocked.

Covers MASO provider/model selection and the interactions a user performs on the Chat page itself: typing and
sending a message, queueing a second one while a turn is in flight, stopping
that turn, and recovering from a send that fails. The existing Chat scenarios
check how finished conversations render; none of them drives the composer.

Run only with a dedicated Vite server, never 5173/8173:
  python tests/browser_chat_composer.py http://127.0.0.1:5287 /tmp/chat-composer-ui [browser]
"""

import asyncio
import base64
import json
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

from browser_lifecycle import BrowserRun, run_browser_main
from playwright.async_api import expect

TAB = "fixture-composer-tab"
SESSION = "fixture-composer-session"
TURN_ONE = "fixture-turn-1"
NOW = "2026-10-09T10:00:00Z"
FAILING_TEXT = "Message that fails"
PNG_IMAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)

# Paths are matched exactly against the tab they belong to. A stub that accepts
# any path ending in "/events" would answer a request aimed at the wrong tab,
# or a GET where the app should have sent a POST, and the unknown-request
# assertion at the end would never see it.
BASE = f"/api/workspaces/tabs/{TAB}/stream"
MANAGED_SESSION = "fixture-managed-session"
MANAGED_BASE = f"/api/workspaces/sessions/{MANAGED_SESSION}/stream"


def event(sequence, event_type, payload, *, turn_id=TURN_ONE, message_id=None, agent_type="maso"):
    return {
        "stream_sequence": sequence,
        "session_id": SESSION,
        "tab_id": TAB,
        "agent_type": agent_type,
        "type": event_type,
        "turn_id": turn_id,
        "message_id": message_id,
        "call_id": None,
        "payload": payload,
        "created_at": NOW,
        "redacted": False,
    }


# MASO condensation and a completed turn, followed by the stored Codex
# warning shape. Preserve the warning's provider identity: filtering is owned
# by each event, while the active Chat exercises the MASO provider controls.
EVENTS = [
    event(1, "turn_started", {"summary": "First question"}),
    event(2, "text_delta", {"text": "First answer."}, message_id=f"{TURN_ONE}:assistant"),
    event(
        3,
        "status",
        {"text": "Condensing context…", "snapshot": True},
        message_id="condense-1",
    ),
    event(
        4,
        "status",
        {"text": "Context condensed", "snapshot": True},
        message_id="condense-1",
    ),
    event(5, "turn_completed", {"status": "completed"}, message_id=TURN_ONE),
    event(
        6,
        "status",
        {
            "text": "---------------------------------------------------------------",
            "provider_status": "warning",
            "snapshot": True,
        },
        turn_id=None,
        agent_type="codex",
    ),
]


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

    errors: list[str] = []
    blocked: list[tuple[str, str]] = []
    unknown_requests: list[tuple[str, str]] = []
    unexpected_external: list[str] = []
    sends: list[dict] = []
    interrupts: list[dict] = []
    managed_stream_paths: list[tuple[str, str]] = []
    codex_session_requests = 0
    model_changes: list[dict] = []
    preflight_requests: list[dict] = []
    tab = {
        "id": TAB,
        "name": "MASO composer fixture",
        "agent_type": "maso",
        "session_kind": "chat",
        "is_active": True,
        "created_at": NOW,
        "workspace_id": None,
        "workspace_name": None,
        "workspace_role": None,
        "archived": False,
        "env": {},
        "target": "local",
        "cwd": "/fixture/current",
    }
    # Mutable stream state: sending appends events the way the backend would.
    # Keyed by text, not by call order: flushing the queue also issues a send,
    # and an order-based flag would be spent on the wrong request.
    state = {
        "events": list(EVENTS),
        "fail_texts": {FAILING_TEXT},
        "turn_open": False,
        "maso_available": False,
        "existing_maso_available": True,
        "previews": {},
    }

    async with browser_run.context(viewport={"width": 1280, "height": 900}) as context:

        async def block_websocket(socket) -> None:
            await socket.close(code=1000, reason="mocked Chat composer smoke")

        async def api(route) -> None:
            nonlocal codex_session_requests
            request = route.request
            parsed = urlparse(request.url)
            if (parsed.scheme, parsed.hostname, parsed.port) != (
                parsed_base.scheme,
                parsed_base.hostname,
                parsed_base.port,
            ):
                # Every external request is blocked. The app's font
                # stylesheet is a known host that is allowed to be blocked;
                # anything else is a real leak and fails the scenario.
                blocked.append((request.method, request.url))
                if parsed.scheme != "https" or parsed.hostname not in {
                    "fonts.googleapis.com",
                    "fonts.gstatic.com",
                }:
                    unexpected_external.append(request.url)
                await route.abort("blockedbyclient")
                return

            path = parsed.path
            method = request.method
            if path.startswith(MANAGED_BASE):
                managed_stream_paths.append((method, path))
            if not path.startswith("/api/"):
                await route.fallback()
                return

            body: object
            if method == "GET" and path == "/api/tabs":
                body = [
                    tab,
                    {
                        "id": "fixture-managed-tab",
                        "name": "Workspace fixture",
                        "agent_type": "codex",
                        "session_kind": "chat",
                        "target": "local",
                        "cwd": "/fixture/workspace",
                        "is_active": False,
                        "created_at": NOW,
                        "workspace_id": "fixture-workspace",
                        "workspace_name": "Fixture workspace",
                        "workspace_role": "worker",
                        "managed_session_id": MANAGED_SESSION,
                        "agent_session_id": "provider-conversation-is-not-managed-session",
                        "archived": False,
                    },
                ]
            elif method == "POST" and path == "/api/maso/preflight":
                payload = json.loads(request.post_data or "{}")
                preflight_requests.append(payload)
                endpoint = payload.get("base_url") or "http://127.0.0.1:9796"
                available = state["maso_available"] and "missing" not in endpoint
                body = {
                    "available": available,
                    "base_url": endpoint,
                    "model_count": 1 if available else 0,
                    "message": (
                        "MASO service is ready."
                        if available
                        else "MASO service is unavailable and cannot be used. Start MASO, then retry."
                    ),
                }
            elif (
                method == "GET" and path in {f"{BASE}/capabilities", f"{MANAGED_BASE}/capabilities"}
            ) or (method == "POST" and path in {f"{BASE}/retry", f"{MANAGED_BASE}/retry"}):
                body = {
                    "structured": state["existing_maso_available"],
                    "unavailable_reason": (
                        None
                        if state["existing_maso_available"]
                        else "MASO service is unavailable and cannot be used. Start MASO, then retry."
                    ),
                    "send_message": True,
                    "supports_goals": False,
                    "supports_images": True,
                    "agent_type": "maso",
                    "supports_mode_switch": False,
                    "current_model": tab["env"].get("MASO_MODEL", "seed-high"),
                    "available_models": [
                        {
                            "id": "seed",
                            "label": "seed",
                            "provider_model_id": "seed",
                            "default_reasoning_effort": "none",
                            "supported_reasoning_efforts": [
                                {
                                    "id": "none",
                                    "label": "Disabled",
                                    "provider_model_id": "seed",
                                },
                                {
                                    "id": "high",
                                    "label": "High",
                                    "provider_model_id": "seed-high",
                                },
                            ],
                        }
                    ],
                }
            elif method == "POST" and path == f"/api/tabs/{TAB}/switch-env":
                payload = json.loads(request.post_data or "{}")
                model_changes.append(payload)
                tab["env"] = payload["env"]
                body = tab
            elif method == "GET" and path in {f"{BASE}/events", f"{MANAGED_BASE}/events"}:
                body = {
                    "events": state["events"],
                    "next_sequence": len(state["events"]),
                    "has_more": False,
                }
            elif method == "POST" and path in {f"{BASE}/wait", f"{MANAGED_BASE}/wait"}:
                # Return authoritative edges without advancing past events
                # the client has not received, including after a reload.
                since = json.loads(request.post_data or "{}").get("since_sequence", -1)
                await asyncio.sleep(0.05)
                fresh = [item for item in state["events"] if item["stream_sequence"] > since]
                body = {
                    "events": fresh,
                    "next_sequence": fresh[-1]["stream_sequence"] if fresh else since,
                    "has_more": False,
                }
            elif method == "POST" and path in {f"{BASE}/send", f"{MANAGED_BASE}/send"}:
                payload = json.loads(request.post_data or "{}")
                sends.append(payload)
                if payload.get("text") in state["fail_texts"]:
                    state["fail_texts"].discard(payload.get("text"))
                    await route.fulfill(
                        status=503,
                        content_type="application/json",
                        body=json.dumps({"detail": "fixture send failure"}),
                    )
                    return
                sequence = len(state["events"])
                # Hub uses the submitted client turn ID for its durable
                # start/completion edges and acknowledgement.
                turn_id = payload["client_turn_id"]
                attachments = []
                for index, attachment in enumerate(payload.get("attachments", [])):
                    attachment_id = uuid.uuid5(
                        uuid.NAMESPACE_URL, f"fixture-image-{len(sends)}-{index}"
                    ).hex
                    preview = base64.b64decode(
                        attachment.get("preview_data_url", attachment["data_url"]).split(",", 1)[1]
                    )
                    state["previews"][attachment_id] = preview
                    attachments.append(
                        {
                            "id": attachment_id,
                            "mime_type": "image/png",
                            "byte_size": len(preview),
                            "width": 1,
                            "height": 1,
                        }
                    )
                # Start the turn without completing it: the composer must now
                # offer Stop, and a further send must queue rather than start a
                # new turn.
                state["events"].append(
                    event(
                        sequence + 1,
                        "turn_started",
                        {"summary": payload.get("text", ""), "attachments": attachments},
                        turn_id=turn_id,
                    )
                )
                state["turn_open"] = True
                body = {
                    "client_turn_id": payload.get("client_turn_id"),
                    "turn_id": turn_id,
                }
            elif method == "GET" and (
                path.startswith(f"{BASE}/attachments/")
                or path.startswith(f"{MANAGED_BASE}/attachments/")
            ):
                preview = state["previews"].get(path.rsplit("/", 1)[-1])
                if preview is None:
                    unknown_requests.append((method, path))
                    await route.fulfill(status=404)
                else:
                    await route.fulfill(status=200, content_type="image/png", body=preview)
                return
            elif method == "POST" and path in {f"{BASE}/cancel", f"{MANAGED_BASE}/cancel"}:
                interrupts.append(json.loads(request.post_data or "{}"))
                sequence = len(state["events"])
                interrupted_turn = next(
                    item["turn_id"]
                    for item in reversed(state["events"])
                    if item["type"] == "turn_started"
                )
                state["events"].append(
                    event(
                        sequence + 1,
                        "turn_completed",
                        {"status": "interrupted"},
                        turn_id=interrupted_turn,
                        message_id=interrupted_turn,
                    )
                )
                state["turn_open"] = False
                body = {"ok": True}
            elif method == "GET" and path in {f"{BASE}/live", f"{MANAGED_BASE}/live"}:
                await route.fulfill(
                    status=200,
                    content_type="text/event-stream",
                    body=": fixture\n\n",
                )
                return
            elif method == "GET" and path == f"{BASE}/goal/current":
                body = None
            elif method == "POST" and path in {
                f"/api/tabs/{TAB}/view",
                "/api/tabs/fixture-managed-tab/view",
            }:
                body = {"ok": True}
            elif method == "GET" and path == "/api/feishu/bot/bots":
                body = {
                    "pool_revision": 0,
                    "bots": [],
                    "pool_editable": True,
                    "deprecated_env": [],
                    "focus_bot_id": None,
                }
            elif method == "GET" and path == f"/api/tabs/{TAB}/network":
                body = {"hostname": "fixture", "addresses": []}
            elif method == "GET" and path == "/api/remote/profiles":
                body = []
            elif method == "GET" and path == "/api/codex/sessions":
                codex_session_requests += 1
                body = [
                    {
                        "cwd": "/fixture/current",
                        "sessions": [
                            {
                                "session_id": "fixture-codex-session",
                                "cwd": "/fixture/current",
                                "start_time": NOW,
                                "title": "Existing Codex session",
                            }
                        ],
                    }
                ]
            elif method == "GET" and path == "/api/auth/check":
                body = {"auth_required": False, "user": None}
            elif method == "GET" and path == "/api/env-presets":
                body = {
                    "custom_presets": [
                        {
                            "id": "maso-missing",
                            "name": "MASO missing service",
                            "text": "MASO_BASE_URL=http://maso-missing.test:9796",
                        }
                    ],
                    "hidden_builtin_ids": [],
                }
            elif method == "GET" and path == "/api/tabs/archived":
                body = []
            elif path.startswith("/api/tabs/sidebar-preferences"):
                # Read, one-shot pin migration and per-tab pin updates all
                # return the same shape; the composer never depends on pins.
                body = {"pinned_tab_ids": []}
            elif method == "GET" and path == "/api/remote/profiles":
                body = []
            elif method == "GET" and path == "/api/tabs/status":
                # Background poll; the store iterates it, so it must be a list.
                body = []
            elif method == "GET" and path == f"/api/tabs/{TAB}/goal/current":
                body = {"goal": None}
            else:
                unknown_requests.append((method, path))
                await route.fulfill(
                    status=501,
                    content_type="application/json",
                    body=json.dumps({"detail": f"unmocked API: {method} {path}"}),
                )
                return
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

        await context.route_web_socket("**/*", block_websocket)
        await context.route("**/*", api)
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(base + f"/?tab={TAB}")

        composer = page.locator(".composer-textarea")
        send_button = page.locator(".composer-send-btn")
        await expect(composer).to_be_visible()
        await expect(send_button).to_be_visible()
        await expect(page.locator(".structured-turn")).to_have_count(1)
        await expect(page.get_by_text("First answer.", exact=True)).to_have_count(1)
        await expect(page.get_by_text("Context condensed", exact=True)).to_have_count(1)
        await expect(page.get_by_text("Condensing context…", exact=True)).to_have_count(0)
        await expect(page.locator(".edit-resend-hover-btn")).to_have_count(0)

        # MASO can be selected for Chat, and switching to Terminal clears it.
        await page.locator(".add-tab").click()
        await page.locator("#tabName").fill("Keep my draft")
        provider_picker = page.get_by_label("Chat Provider", exact=True)
        await provider_picker.select_option("maso")
        preflight = page.locator(".modal .maso-preflight")
        create_button = page.locator(".modal").get_by_role("button", name="Create", exact=True)
        await expect(preflight).to_contain_text("MASO service is unavailable and cannot be used")
        await expect(preflight).to_have_attribute("role", "alert")
        await expect(create_button).to_be_disabled()
        await page.locator("#tabName").press("Enter")
        await expect(page.locator("#tabName")).to_have_value("Keep my draft")
        state["maso_available"] = True
        await preflight.get_by_role("button", name="Retry check", exact=True).click()
        await expect(preflight).to_contain_text("MASO service is ready.")
        await expect(create_button).to_be_enabled()
        await expect(page.locator("#tabName")).to_have_value("Keep my draft")
        # A selected custom address is checked from Hub, not the browser host.
        preset = page.locator(".modal .env-preset-row select")
        await preset.select_option("maso-missing")
        await expect(preflight).to_contain_text("http://maso-missing.test:9796")
        await expect(create_button).to_be_disabled()
        assert preflight_requests[-1]["base_url"] == "http://maso-missing.test:9796"
        await preset.select_option("none")
        await expect(preflight).to_contain_text("MASO service is ready.")
        await expect(create_button).to_be_enabled()
        await expect(page.locator(".modal").get_by_text("Solo Mode", exact=True)).to_have_count(0)
        await page.locator(".session-kind-control").get_by_role(
            "button", name="Terminal", exact=True
        ).click()
        terminal_picker = page.get_by_label("Terminal Profile", exact=True)
        await expect(terminal_picker).to_have_value("claude")
        await expect(terminal_picker.locator('option[value="maso"]')).to_have_count(0)
        await expect(page.locator(".maso-preflight")).to_have_count(0)
        await expect(create_button).to_be_enabled()
        await page.locator(".modal").get_by_role("button", name="Cancel", exact=True).click()

        # The app remounts its top bar across the desktop/mobile breakpoint.
        # Exercise a fresh mobile dialog, rather than retaining a detached one.
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.locator(".add-tab").click()
        await page.get_by_label("Chat Provider", exact=True).select_option("maso")
        await preset.select_option("maso-missing")
        await expect(preflight).to_contain_text("MASO service is unavailable")
        await expect(create_button).to_be_disabled()
        await preflight.scroll_into_view_if_needed()
        mobile_retry = preflight.get_by_role("button", name="Retry check", exact=True)
        before_retry = len(preflight_requests)
        await mobile_retry.click()
        await expect(preflight).to_contain_text("MASO service is unavailable")
        assert len(preflight_requests) > before_retry
        # The mobile footer is sticky. Scroll the dialog to expose the entire
        # retry control above it, as a user can do with a normal scroll.
        await page.locator(".modal").hover()
        await page.mouse.wheel(0, 320)
        await page.wait_for_function("""() => {
          const retry = document.querySelector('.maso-preflight button');
          const footer = document.querySelector('.modal-actions');
          return retry && footer && retry.getBoundingClientRect().bottom <= footer.getBoundingClientRect().top;
        }""")
        box = await preflight.bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= 390
        await page.screenshot(path=str(output / "maso-unavailable-mobile.png"), full_page=True)
        await page.locator(".modal").get_by_role("button", name="Cancel", exact=True).click()
        await page.set_viewport_size({"width": 1280, "height": 900})

        # Effort selection must send MASO's provider key, not Codex effort env.
        trigger = page.locator(".composer-model-picker .composer-mode-trigger")
        await expect(trigger).to_contain_text("seed · High")
        await trigger.click()
        await page.get_by_role("listbox", name="Thinking effort", exact=True).get_by_role(
            "option", name=re.compile(r"^High")
        ).click()
        await expect(trigger).to_contain_text("High")
        assert model_changes[-1]["env"] == {"MASO_MODEL": "seed-high"}, model_changes
        await page.set_viewport_size({"width": 390, "height": 844})
        await trigger.click()
        menu = page.get_by_role("dialog", name="Model and thinking effort", exact=True)
        box = await menu.bounding_box()
        assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= 390, box
        await page.screenshot(path=str(output / "maso-model-mobile.png"), full_page=True)
        await page.keyboard.press("Escape")
        await page.set_viewport_size({"width": 1280, "height": 900})

        # An empty composer cannot send: the control is disabled rather than
        # accepting a click that silently does nothing.
        await expect(send_button).to_be_disabled()
        await expect(send_button).to_have_text("Send")
        await expect(page.locator(".composer-stop-btn")).to_have_count(0)

        # Whitespace alone is still empty.
        await composer.fill("   ")
        await expect(send_button).to_be_disabled()

        await composer.fill("Second question")
        await expect(send_button).to_be_enabled()

        # Ordinary click, not a forced one: the button must be genuinely reachable.
        await send_button.click()
        await expect(page.locator(".structured-turn")).to_have_count(2)
        assert len(sends) == 1, sends
        assert sends[0]["text"] == "Second question", sends[0]
        assert sends[0].get("client_turn_id"), "send must carry a client turn id"

        # The composer clears on a successful send, so the text cannot be sent twice.
        await expect(composer).to_have_value("")

        # A turn is now in flight: Stop appears and Send becomes Queue.
        stop_button = page.locator(".composer-stop-btn")
        await expect(stop_button).to_be_visible()
        await expect(stop_button).to_have_text("Stop")
        await expect(send_button).to_have_text("Queue")

        # Queueing while a turn is live holds the text locally instead of
        # racing a second turn onto the provider. The queue is only flushed
        # once the live turn terminalizes.
        await composer.fill("Queued follow-up")
        await expect(send_button).to_be_enabled()
        await send_button.click()
        await expect(page.locator(".composer-queue")).to_contain_text("1 message queued")
        assert len(sends) == 1, "queueing must not issue a second send while a turn is live"
        await expect(composer).to_have_value("")

        await page.screenshot(path=str(output / "chat-composer-in-flight.png"), full_page=True)

        # Stop terminalizes the live turn, which releases the queued draft.
        await stop_button.click()
        assert len(interrupts) == 1, interrupts
        await expect(page.locator(".composer-queue")).to_have_count(0)
        assert len(sends) == 2, sends
        assert sends[1]["text"] == "Queued follow-up", sends[1]
        assert sends[0]["client_turn_id"] != sends[1]["client_turn_id"], sends

        # Draining the queue started another turn. Stop it so the composer is
        # idle again: the failure below must be the send failing, not a queue.
        await expect(page.locator(".composer-stop-btn")).to_be_visible()
        await page.locator(".composer-stop-btn").click()
        await expect(page.locator(".composer-stop-btn")).to_have_count(0)
        await expect(send_button).to_have_text("Send")
        sends_before_failure = len(sends)

        # A failing send must surface an error and keep the user's text, so the
        # message is never lost to a transport failure.
        await composer.fill(FAILING_TEXT)
        await send_button.click()
        assert len(sends) == sends_before_failure + 1, "the failing text must be sent, not queued"
        await expect(page.locator(".composer-error")).to_be_visible()
        await expect(composer).to_have_value(FAILING_TEXT)
        await expect(send_button).to_be_enabled()

        # Retrying the same text succeeds and clears the error.
        await send_button.click()
        await expect(page.locator(".composer-error")).to_have_count(0)
        await expect(composer).to_have_value("")
        assert sends[-1]["text"] == FAILING_TEXT, sends[-1]

        # The retry must be a distinct request, not a replay of the failed one.
        assert sends[-1]["client_turn_id"] != sends[-2]["client_turn_id"], sends[-2:]

        # Mobile: the composer and both controls stay usable and on screen.
        await page.set_viewport_size({"width": 390, "height": 844})
        await composer.fill("Mobile message")
        composer_box = await composer.bounding_box()
        send_box = await send_button.bounding_box()
        assert composer_box is not None and send_box is not None
        assert send_box["x"] + send_box["width"] <= 390, send_box
        assert send_box["y"] + send_box["height"] <= 844, send_box
        assert composer_box["width"] > 0 and composer_box["height"] > 0
        await expect(send_button).to_be_enabled()
        await page.screenshot(path=str(output / "chat-composer-mobile.png"), full_page=True)

        # Existing Chats explain service loss and fail closed, then recover on
        # Retry without suggesting an unsupported MASO Terminal session.
        state["existing_maso_available"] = False
        await page.reload()
        banner = page.locator(".structured-banner[role='alert']")
        await expect(banner).to_contain_text("MASO service is unavailable and cannot be used")
        await expect(banner).to_contain_text("Start the MASO HTTP service, then retry.")
        await expect(banner).not_to_contain_text("Create a Terminal session")
        await expect(send_button).to_be_disabled()
        await page.screenshot(path=str(output / "maso-existing-unavailable.png"), full_page=True)
        state["existing_maso_available"] = True
        await banner.get_by_role("button", name="Retry", exact=True).click()
        await expect(banner).to_have_count(0)

        # MASO accepts native image-only turns, retains multiple thumbnails,
        # and queues original image data while another turn is still running.
        await page.locator(".composer-stop-btn").click()
        await expect(page.locator(".composer-stop-btn")).to_have_count(0)
        await composer.fill("")
        image_input = page.locator(".composer-file-input")
        await image_input.set_input_files(
            [
                {"name": "first.png", "mimeType": "image/png", "buffer": PNG_IMAGE},
                {"name": "second.png", "mimeType": "image/png", "buffer": PNG_IMAGE},
            ]
        )
        await expect(page.locator(".attachment-chip")).to_have_count(2)
        await expect(send_button).to_be_enabled()
        await expect(page.locator(".composer-error")).to_have_count(0)
        await send_button.click()
        image_send = sends[-1]
        assert image_send["text"] == "" and len(image_send["attachments"]) == 2
        assert all(
            base64.b64decode(att["data_url"].split(",", 1)[1]) == PNG_IMAGE
            for att in image_send["attachments"]
        )
        await expect(page.locator(".attachment-chip")).to_have_count(0)
        await expect(page.locator(".turn-attachments img")).to_have_count(2)
        await composer.fill("Queued image")
        await image_input.set_input_files(
            {"name": "queued.png", "mimeType": "image/png", "buffer": PNG_IMAGE}
        )
        await expect(page.locator(".attachment-chip")).to_have_count(1)
        before_queue = len(sends)
        await send_button.click()
        await expect(page.locator(".composer-queue")).to_contain_text("1 message queued")
        assert len(sends) == before_queue
        await page.locator(".composer-stop-btn").click()
        await expect(page.locator(".composer-queue")).to_have_count(0)
        assert len(sends) == before_queue + 1
        assert sends[-1]["text"] == "Queued image" and len(sends[-1]["attachments"]) == 1
        assert (
            base64.b64decode(sends[-1]["attachments"][0]["data_url"].split(",", 1)[1]) == PNG_IMAGE
        )
        await page.locator(".composer-stop-btn").click()
        sends_before_reload = len(sends)
        await page.reload()
        await expect(page.locator(".turn-attachments img")).to_have_count(3)
        await page.wait_for_function(
            """() => [...document.querySelectorAll('.turn-attachments img')].every(img => img.complete && img.naturalWidth === 1)"""
        )
        assert (
            len(sends) == sends_before_reload
        ), "image history reload must not resend a provider turn"
        await page.screenshot(path=str(output / "maso-image-history-mobile.png"), full_page=True)

        # Opening the Workspace-managed Chat must preserve one conversation
        # surface while switching every history/send/cancel request to managed
        # session identity. The ordinary MASO Chat above remained tab-scoped.
        await page.set_viewport_size({"width": 1280, "height": 900})
        await page.evaluate("""async () => {
          const { useTerminalStore } = await import('/src/stores/terminalStore.ts')
          useTerminalStore().setActiveTab('fixture-managed-tab')
        }""")
        await expect(composer).to_be_visible()
        managed_send_count = len(sends)
        await composer.fill("Managed mailbox message")
        await composer.press("Control+Enter")
        await expect(page.locator(".composer-stop-btn")).to_be_visible()
        assert len(sends) == managed_send_count + 1
        assert sends[-1]["text"] == "Managed mailbox message"
        assert sends[-1]["delivery"] == "normal"
        await composer.fill("Managed queued shortcut")
        await composer.press("Control+Enter")
        await expect(page.locator(".composer-queue")).to_contain_text("1 message queued")
        assert len(sends) == managed_send_count + 1
        await expect(page.locator(".composer-stop-btn")).to_be_visible()
        await page.locator(".composer-stop-btn").click()
        await expect(page.locator(".composer-queue")).to_have_count(0)
        assert len(sends) == managed_send_count + 2
        assert sends[-1]["text"] == "Managed queued shortcut"
        assert sends[-1]["delivery"] == "normal"
        await page.locator(".composer-stop-btn").click()
        assert ("GET", f"{MANAGED_BASE}/capabilities") in managed_stream_paths
        assert ("GET", f"{MANAGED_BASE}/events") in managed_stream_paths
        assert ("POST", f"{MANAGED_BASE}/send") in managed_stream_paths
        assert ("POST", f"{MANAGED_BASE}/cancel") in managed_stream_paths
        assert all(
            "/workspaces/tabs/fixture-managed-tab/stream" not in path
            for _, path in managed_stream_paths
        )
        await page.screenshot(path=str(output / "managed-chat-session-stream.png"), full_page=True)
        await page.evaluate(f"""async () => {{
          const {{ useTerminalStore }} = await import('/src/stores/terminalStore.ts')
          useTerminalStore().setActiveTab('{TAB}')
        }}""")
        await expect(composer).to_be_visible()

        # The create dialog must show common directories even when the input
        # starts with a different path. Codex history stays unloaded until the
        # user expands its section.
        await page.set_viewport_size({"width": 1280, "height": 900})
        await page.evaluate(
            "localStorage.setItem('claude-hub:cwd-history:local', "
            "JSON.stringify(['/fixture/recent', '/fixture/other', "
            "'/fixture/three', '/fixture/four', '/fixture/five', "
            "'/fixture/six', '/fixture/seven', '/fixture/eight']))"
        )
        await page.locator(".add-tab").click()
        cwd = page.locator("#tabCwd")
        await expect(cwd).to_have_value("/fixture/recent")
        await cwd.click()
        choices = page.locator(".cwd-choice")
        await expect(choices).to_have_count(10)
        await expect(choices.nth(8)).to_have_text("/fixture/current")
        await expect(choices.nth(9)).to_have_text("/fixture/workspace")
        await page.screenshot(path=str(output / "create-session-directory-choices.png"))
        await cwd.fill("CURRENT")
        await expect(choices).to_have_count(1)
        await choices.click()
        await expect(cwd).to_have_value("/fixture/current")
        await cwd.click()
        await cwd.press("ArrowUp")
        await expect(choices.last).to_have_class("cwd-choice is-active")
        assert await page.locator(".cwd-choices").evaluate("el => el.scrollTop") > 0
        await cwd.press("Enter")
        await expect(cwd).to_have_value("/fixture/workspace")
        await cwd.click()
        await cwd.press("ArrowDown")
        await cwd.press("Enter")
        await expect(cwd).to_have_value("/fixture/recent")
        await page.locator(".cwd-choices-btn").click()
        await expect(choices).to_have_count(10)
        await choices.nth(1).click()
        await expect(cwd).to_have_value("/fixture/other")

        await page.get_by_label("Chat Provider").select_option("codex")
        toggle = page.locator(".codex-session-selector__toggle")
        await expect(toggle).to_have_attribute("aria-expanded", "false")
        await expect(page.locator(".codex-session-selector__body")).to_have_count(0)
        assert codex_session_requests == 0, "Codex sessions loaded before expansion"
        await toggle.click()
        await expect(toggle).to_have_attribute("aria-expanded", "true")
        await expect(page.locator(".codex-session-selector__workspace-path")).to_have_text(
            "/fixture/current"
        )
        await page.locator(".codex-session-selector__workspace").last.click()
        await expect(page.get_by_text("Existing Codex session")).to_be_visible()
        assert codex_session_requests == 1
        await toggle.click()
        await expect(toggle).to_have_attribute("aria-expanded", "false")
        await toggle.click()
        await expect(page.get_by_text("Existing Codex session")).to_be_visible()
        assert codex_session_requests == 1, "reopening should reuse loaded sessions"
        await page.screenshot(path=str(output / "create-session-choices.png"), full_page=True)

        await page.set_viewport_size({"width": 390, "height": 844})
        await page.locator(".add-tab").click()
        await expect(cwd).to_have_value("/fixture/recent")
        await page.get_by_label("Chat Provider").select_option("codex")
        toggle = page.locator(".codex-session-selector__toggle")
        await expect(toggle).to_have_attribute("aria-expanded", "false")
        assert codex_session_requests == 1, "mobile Codex picker loaded while collapsed"
        modal_box = await page.locator(".modal").bounding_box()
        assert modal_box is not None
        assert modal_box["x"] >= 0 and modal_box["x"] + modal_box["width"] <= 390
        await page.screenshot(path=str(output / "create-session-mobile.png"))
        await cwd.click()
        await expect(choices).to_have_count(10)
        await choices.last.click()
        await expect(cwd).to_have_value("/fixture/workspace")

        assert not errors, errors
        assert not unexpected_external, unexpected_external
        assert not unknown_requests, unknown_requests

        (output / "report.json").write_text(
            json.dumps(
                {
                    "result": "PASS",
                    "sends": len(sends),
                    "interrupts": len(interrupts),
                    "codex_session_requests": codex_session_requests,
                    "maso_model_changes": model_changes,
                    "maso_preflight_requests": preflight_requests,
                    "image_turns": sum(bool(item.get("attachments")) for item in sends),
                    "unknown_requests": unknown_requests,
                    "blocked_external_requests": len(blocked),
                    "unexpected_external_requests": unexpected_external,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    run_browser_main(main)
