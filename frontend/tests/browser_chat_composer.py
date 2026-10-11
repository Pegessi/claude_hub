"""Full-app Chat composer acceptance with every API request mocked.

Covers the interactions a user performs on the Chat page itself: typing and
sending a message, queueing a second one while a turn is in flight, stopping
that turn, and recovering from a send that fails. The existing Chat scenarios
check how finished conversations render; none of them drives the composer.

Run only with a dedicated Vite server, never 5173/8173:
  python tests/browser_chat_composer.py http://127.0.0.1:5287 /tmp/chat-composer-ui [browser]
"""

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from browser_lifecycle import BrowserRun, run_browser_main
from playwright.async_api import expect

TAB = "fixture-composer-tab"
SESSION = "fixture-composer-session"
TURN_ONE = "fixture-turn-1"
NOW = "2026-10-09T10:00:00Z"
FAILING_TEXT = "Message that fails"

# Paths are matched exactly against the tab they belong to. A stub that accepts
# any path ending in "/events" would answer a request aimed at the wrong tab,
# or a GET where the app should have sent a POST, and the unknown-request
# assertion at the end would never see it.
BASE = f"/api/workspaces/tabs/{TAB}/stream"


def event(sequence, event_type, payload, *, turn_id=TURN_ONE, message_id=None):
    return {
        "stream_sequence": sequence,
        "session_id": SESSION,
        "tab_id": TAB,
        "agent_type": "codex",
        "type": event_type,
        "turn_id": turn_id,
        "message_id": message_id,
        "call_id": None,
        "payload": payload,
        "created_at": NOW,
        "redacted": False,
    }


# One finished turn, so the page has history but no turn in flight on load.
EVENTS = [
    event(1, "turn_started", {"summary": "First question"}),
    event(2, "text_delta", {"text": "First answer."}, message_id=f"{TURN_ONE}:assistant"),
    event(3, "turn_completed", {"status": "completed"}, message_id=TURN_ONE),
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
    # Mutable stream state: sending appends events the way the backend would.
    # Keyed by text, not by call order: flushing the queue also issues a send,
    # and an order-based flag would be spent on the wrong request.
    state = {"events": list(EVENTS), "fail_texts": {FAILING_TEXT}, "turn_open": False}

    async with browser_run.context(viewport={"width": 1280, "height": 900}) as context:
        async def block_websocket(socket) -> None:
            await socket.close(code=1000, reason="mocked Chat composer smoke")

        async def api(route) -> None:
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
            if not path.startswith("/api/"):
                await route.fallback()
                return

            body: object
            if method == "GET" and path == "/api/tabs":
                body = [
                    {
                        "id": TAB,
                        "name": "Composer fixture",
                        "agent_type": "codex",
                        "session_kind": "chat",
                        "is_active": True,
                        "created_at": NOW,
                        "workspace_id": None,
                        "workspace_name": None,
                        "workspace_role": None,
                        "archived": False,
                    }
                ]
            elif method == "GET" and path == f"{BASE}/capabilities":
                body = {
                    "structured": True,
                    "send_message": True,
                    "supports_goals": False,
                    "supports_images": False,
                    "agent_type": "codex",
                    "supports_mode_switch": False,
                }
            elif method == "GET" and path == f"{BASE}/events":
                body = {
                    "events": state["events"],
                    "next_sequence": len(state["events"]),
                    "has_more": False,
                }
            elif method == "POST" and path == f"{BASE}/wait":
                # Long poll: return whatever the send handlers have appended.
                body = {
                    "events": [],
                    "next_sequence": len(state["events"]),
                    "has_more": False,
                }
            elif method == "POST" and path == f"{BASE}/send":
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
                turn_id = f"fixture-sent-turn-{len(sends)}"
                # Start the turn without completing it: the composer must now
                # offer Stop, and a further send must queue rather than start a
                # new turn.
                state["events"].append(
                    event(
                        sequence + 1,
                        "turn_started",
                        {"summary": payload.get("text", "")},
                        turn_id=turn_id,
                    )
                )
                state["turn_open"] = True
                body = {"client_turn_id": payload.get("client_turn_id"), "turn_id": turn_id}
            elif method == "POST" and path == f"{BASE}/cancel":
                interrupts.append(json.loads(request.post_data or "{}"))
                sequence = len(state["events"])
                interrupted_turn = f"fixture-sent-turn-{len(sends)}"
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
            elif method == "GET" and path == f"{BASE}/live":
                await route.fulfill(
                    status=200,
                    content_type="text/event-stream",
                    body=": fixture\n\n",
                )
                return
            elif method == "GET" and path == f"{BASE}/goal/current":
                body = None
            elif method == "POST" and path == f"/api/tabs/{TAB}/view":
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
            elif method == "GET" and path == "/api/auth/check":
                body = {"auth_required": False, "user": None}
            elif method == "GET" and path == "/api/env-presets":
                body = []
            elif method == "GET" and path == "/api/tabs/archived":
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

        assert not errors, errors
        assert not unexpected_external, unexpected_external
        assert not unknown_requests, unknown_requests

        (output / "report.json").write_text(
            json.dumps(
                {
                    "result": "PASS",
                    "sends": len(sends),
                    "interrupts": len(interrupts),
                    "unknown_requests": unknown_requests,
                    "blocked_external_requests": len(blocked),
                    "unexpected_external_requests": unexpected_external,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    run_browser_main(main)
