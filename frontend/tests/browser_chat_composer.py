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
from urllib.parse import parse_qs, urlparse

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


def event(
    sequence,
    event_type,
    payload,
    *,
    turn_id=TURN_ONE,
    message_id=None,
    agent_type="maso",
):
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
    capability_requests: list[dict[str, str]] = []
    retry_requests: list[str] = []
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
        "managed_available": True,
        "capability_delays": {},
        "capability_status": {},
        "retry_delays": {},
        "retry_status": {},
        "wait_detail": None,
        "previews": {},
    }

    initial_manual_capabilities = asyncio.Event()
    state["capability_delays"][f"{BASE}/capabilities"] = initial_manual_capabilities

    def capability_count(path: str, query: str | None = None) -> int:
        return sum(
            1
            for item in capability_requests
            if item["path"] == path and (query is None or item["query"] == query)
        )

    async with browser_run.context(
        viewport={"width": 1280, "height": 900},
        launch_args=["--no-proxy-server"],
    ) as context:

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
            if (
                request.method == "GET"
                and path.startswith("/api/workspaces/tabs/")
                and path.endswith("/messages")
            ):
                body = {"messages": []}
            elif method == "GET" and path == "/api/tabs":
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
                if method == "POST":
                    retry_requests.append(path)
                    delay = state["retry_delays"].pop(path, None)
                    if delay is not None:
                        await delay.wait()
                    status = state["retry_status"].get(path, 200)
                    if status != 200:
                        await route.fulfill(
                            status=status,
                            content_type="application/json",
                            body=json.dumps({"detail": "fixture retry unavailable"}),
                        )
                        return
                else:
                    capability_requests.append({"path": path, "query": parsed.query})
                    delay = state["capability_delays"].pop(path, None)
                    if delay is not None:
                        await delay.wait()
                    status = state["capability_status"].get(path, 200)
                    if status != 200:
                        await route.fulfill(
                            status=status,
                            content_type="application/json",
                            body=json.dumps({"detail": "fixture capabilities unavailable"}),
                        )
                        return
                is_managed = path.startswith(MANAGED_BASE)
                available = (
                    state["managed_available"] if is_managed else state["existing_maso_available"]
                )
                agent_type = "codex" if is_managed else tab["agent_type"]
                model_key = "CODEX_MODEL" if is_managed else "MASO_MODEL"
                body = {
                    "structured": available,
                    "unavailable_reason": (
                        None
                        if available
                        else "MASO service is unavailable and cannot be used. Start MASO, then retry."
                    ),
                    "adapter_id": "codex-native" if is_managed else "maso_http",
                    "schema_version": 1,
                    "sources": ["native"],
                    "send_message": True,
                    "supports_goals": False,
                    "supports_images": True,
                    "agent_type": agent_type,
                    "supports_mode_switch": False,
                    "current_model": tab["env"].get(model_key, "seed-high"),
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
            elif method == "GET" and path in {
                f"{BASE}/events",
                f"{MANAGED_BASE}/events",
            }:
                since = int(parse_qs(parsed.query).get("since_sequence", ["-1"])[0])
                fresh = [item for item in state["events"] if item["stream_sequence"] > since]
                body = {
                    "events": fresh,
                    "next_sequence": fresh[-1]["stream_sequence"] if fresh else since,
                    "has_more": False,
                }
            elif method == "POST" and path in {f"{BASE}/wait", f"{MANAGED_BASE}/wait"}:
                if state["wait_detail"] is not None:
                    await route.fulfill(
                        status=409,
                        content_type="application/json",
                        body=json.dumps({"detail": state["wait_detail"]}),
                    )
                    return
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
                        {
                            "summary": payload.get("text", ""),
                            "attachments": attachments,
                        },
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
            elif method == "POST" and path in {
                f"{BASE}/cancel",
                f"{MANAGED_BASE}/cancel",
            }:
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
            elif method == "GET" and path == "/api/dot/identity":
                # The Dot launcher in the tab bar reads Dot's name and avatar.
                body = {"name": "Dot", "avatar": "", "avatar_image": None}
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
        model_trigger = page.locator(".composer-model-picker .composer-mode-trigger")
        await expect(composer).to_be_visible()
        await expect(send_button).to_be_visible()
        await expect(page.locator(".structured-turn")).to_have_count(1)
        await expect(page.get_by_text("First answer.", exact=True)).to_have_count(1)
        await expect(page.get_by_text("Context condensed", exact=True)).to_have_count(1)
        await expect(page.get_by_text("Condensing context…", exact=True)).to_have_count(0)
        await expect(page.locator(".edit-resend-hover-btn")).to_have_count(0)
        assert await page.locator(".structured-timeline").evaluate(
            "element => !element.classList.contains('is-timeline-hidden')"
        ), "history must render before delayed capabilities resolve"
        await expect(composer).to_be_disabled()
        await expect(send_button).to_be_disabled()
        await expect(model_trigger).to_be_disabled()
        assert capability_count(f"{BASE}/capabilities") == 1
        initial_manual_capabilities.set()
        await expect(composer).to_be_enabled()
        await expect(model_trigger).to_be_enabled()
        await composer.fill("Capability gate probe")
        await expect(send_button).to_be_enabled()
        await composer.fill("")

        # Cached history renders before managed capabilities too. MASO's live
        # readiness is not cached, while the managed native fixture is reused.
        managed_capabilities = asyncio.Event()
        state["capability_delays"][f"{MANAGED_BASE}/capabilities"] = managed_capabilities
        await page.evaluate("""async () => {
          const { useTerminalStore } = await import('/src/stores/terminalStore.ts')
          useTerminalStore().setActiveTab('fixture-managed-tab')
        }""")
        await expect(page.get_by_text("First answer.", exact=True)).to_have_count(1)
        assert await page.locator(".structured-timeline").evaluate(
            "element => !element.classList.contains('is-timeline-hidden')"
        ), "managed history must render before delayed capabilities resolve"
        await expect(composer).to_be_disabled()
        await expect(send_button).to_be_disabled()
        await expect(model_trigger).to_be_disabled()
        assert capability_count(f"{MANAGED_BASE}/capabilities") == 1
        managed_capabilities.set()
        await expect(composer).to_be_enabled()
        await expect(model_trigger).to_be_enabled()
        await composer.fill("Managed capability gate probe")
        await expect(send_button).to_be_enabled()
        await composer.fill("")
        manual_capability_requests = capability_count(f"{BASE}/capabilities")
        managed_capability_requests = capability_count(f"{MANAGED_BASE}/capabilities")
        await page.evaluate(f"""async () => {{
          const {{ useTerminalStore }} = await import('/src/stores/terminalStore.ts')
          useTerminalStore().setActiveTab('{TAB}')
        }}""")
        await expect(page.locator(".chat-sidebar__item.active")).to_contain_text(
            "MASO composer fixture"
        )
        await expect(composer).to_be_enabled()
        assert capability_count(f"{BASE}/capabilities") > manual_capability_requests
        manual_capability_requests = capability_count(f"{BASE}/capabilities")
        await page.evaluate("""async () => {
          const { useTerminalStore } = await import('/src/stores/terminalStore.ts')
          useTerminalStore().setActiveTab('fixture-managed-tab')
        }""")
        await page.wait_for_function("""async () => {
          const { useTerminalStore } = await import('/src/stores/terminalStore.ts')
          return useTerminalStore().activeTabId === 'fixture-managed-tab'
        }""")
        await page.wait_for_timeout(100)
        assert capability_count(f"{MANAGED_BASE}/capabilities") == managed_capability_requests
        await page.evaluate(f"""async () => {{
          const {{ useTerminalStore }} = await import('/src/stores/terminalStore.ts')
          useTerminalStore().setActiveTab('{TAB}')
        }}""")
        await expect(page.locator(".chat-sidebar__item.active")).to_contain_text(
            "MASO composer fixture"
        )
        await expect(composer).to_be_enabled()
        assert capability_count(f"{BASE}/capabilities") > manual_capability_requests

        # A capabilities HTTP failure must not hide the already fetched history.
        # Pending and failed transport Retry keep that history readable before a
        # later successful Retry refreshes capabilities.
        state["capability_status"][f"{BASE}/capabilities"] = 503
        await page.reload()
        await expect(page.get_by_text("First answer.", exact=True)).to_have_count(1)
        alert = page.locator(".structured-banner[role='alert']")
        await expect(alert).to_contain_text("capabilities HTTP 503")
        await expect(composer).to_be_disabled()
        await expect(send_button).to_be_disabled()
        await expect(model_trigger).to_be_disabled()
        retry_delay = asyncio.Event()
        state["retry_delays"][f"{BASE}/retry"] = retry_delay
        state["retry_status"][f"{BASE}/retry"] = 503
        retries_before = retry_requests.count(f"{BASE}/retry")
        await alert.get_by_role("button", name="Retry", exact=True).click()
        for _ in range(20):
            if retry_requests.count(f"{BASE}/retry") > retries_before:
                break
            await asyncio.sleep(0.05)
        assert retry_requests.count(f"{BASE}/retry") > retries_before
        await expect(page.get_by_text("First answer.", exact=True)).to_have_count(1)
        assert await page.locator(".structured-timeline").evaluate(
            "element => !element.classList.contains('is-timeline-hidden')"
        ), "history must remain visible while Retry is pending"
        retry_delay.set()
        await expect(alert).to_contain_text("fixture retry unavailable")
        await expect(page.get_by_text("First answer.", exact=True)).to_have_count(1)
        assert await page.locator(".structured-timeline").evaluate(
            "element => !element.classList.contains('is-timeline-hidden')"
        ), "history must remain visible after Retry fails"
        await expect(composer).to_be_disabled()
        await expect(send_button).to_be_disabled()
        await expect(model_trigger).to_be_disabled()

        before_retry_caps = capability_count(f"{BASE}/capabilities")
        state["retry_status"][f"{BASE}/retry"] = 200
        state["capability_status"][f"{BASE}/capabilities"] = 200
        await alert.get_by_role("button", name="Retry", exact=True).click()
        await expect(alert).to_have_count(0)
        await expect(composer).to_be_enabled()
        assert capability_count(f"{BASE}/capabilities") > before_retry_caps
        assert f"{BASE}/retry" in retry_requests
        await composer.fill("")

        # A structured=false capabilities response also leaves history readable
        # and keeps input disabled until explicit Retry refreshes runtime caps.
        state["existing_maso_available"] = False
        await page.reload()
        await expect(page.get_by_text("First answer.", exact=True)).to_have_count(1)
        banner = page.locator(".structured-banner[role='alert']")
        await expect(banner).to_contain_text("MASO service is unavailable and cannot be used")
        await expect(banner).to_contain_text("Start the MASO HTTP service, then retry.")
        await expect(banner).not_to_contain_text("Create a Terminal session")
        await expect(composer).to_be_disabled()
        await expect(send_button).to_be_disabled()
        await expect(model_trigger).to_be_disabled()
        before_retry_caps = capability_count(f"{BASE}/capabilities")
        state["existing_maso_available"] = True
        await banner.get_by_role("button", name="Retry", exact=True).click()
        await expect(banner).to_have_count(0)
        await expect(composer).to_be_enabled()
        assert capability_count(f"{BASE}/capabilities") > before_retry_caps
        await composer.fill("")

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
        await (
            page.locator(".session-kind-control")
            .get_by_role("button", name="Terminal", exact=True)
            .click()
        )
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

        # Effort selection must send MASO's provider key, not Codex effort env,
        # and it must force a fresh capabilities read for the new model config.
        trigger = model_trigger
        await expect(trigger).to_contain_text("seed · High")
        refresh_caps_before = capability_count(f"{BASE}/capabilities", "refresh=true")
        await trigger.click()
        await (
            page.get_by_role("listbox", name="Thinking effort", exact=True)
            .get_by_role("option", name=re.compile(r"^High"))
            .click()
        )
        await expect(trigger).to_contain_text("High")
        assert model_changes[-1]["env"] == {"MASO_MODEL": "seed-high"}, model_changes
        for _ in range(20):
            if capability_count(f"{BASE}/capabilities", "refresh=true") == refresh_caps_before + 1:
                break
            await asyncio.sleep(0.1)
        assert (
            capability_count(f"{BASE}/capabilities", "refresh=true") == refresh_caps_before + 1
        ), capability_requests
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
        await expect(page.locator(".draft-queue")).to_contain_text("1 message queued")
        assert len(sends) == 1, "queueing must not issue a second send while a turn is live"
        await expect(composer).to_have_value("")

        await page.screenshot(path=str(output / "chat-composer-in-flight.png"), full_page=True)

        # Stop terminalizes the live turn, which releases the queued draft.
        await stop_button.click()
        assert len(interrupts) == 1, interrupts
        await expect(page.locator(".draft-queue")).to_have_count(0)
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

        # Native delivery has no identity deduplication. An explicit resend is
        # a distinct intent; Goal/Managed Chat retention is tested separately.
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
        await expect(page.locator(".draft-queue")).to_contain_text("1 message queued")
        assert len(sends) == before_queue
        await page.locator(".composer-stop-btn").click()
        await expect(page.locator(".draft-queue")).to_have_count(0)
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
        await expect(page.locator(".draft-queue")).to_have_count(0)
        assert len(sends) == managed_send_count + 2
        assert sends[-1]["text"] == "Managed queued shortcut"
        assert sends[-1]["delivery"] == "normal"
        await expect(page.locator(".composer-stop-btn")).to_be_visible()
        await page.locator(".composer-stop-btn").click()
        await expect(page.locator(".draft-queue")).to_have_count(0)
        assert len(sends) == managed_send_count + 2
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

        # Reasoning presentation is exercised through the same mocked history
        # and long-poll routes as a real Chat. Start from a bounded transcript so
        # every assertion below belongs to one deliberate stream snapshot.
        thinking_turn = "fixture-thinking-opaque"
        plaintext_turn = "fixture-thinking-plaintext"
        interrupted_turn = "fixture-thinking-interrupted"
        error_turn = "fixture-thinking-error"
        sequence = 0
        state["events"] = []
        state["turn_open"] = False

        def append_thinking_event(event_type, payload, *, turn_id, message_id=None):
            nonlocal sequence
            sequence += 1
            state["events"].append(
                event(
                    sequence,
                    event_type,
                    payload,
                    turn_id=turn_id,
                    message_id=message_id,
                )
            )

        # Snapshot one: opaque reasoning is a single compact active row with an
        # animated, visibly labelled indicator.
        append_thinking_event(
            "turn_started", {"summary": "Opaque reasoning"}, turn_id=thinking_turn
        )
        append_thinking_event(
            "status",
            {"text": "Thinking…", "provider_status": "reasoning", "snapshot": True},
            turn_id=thinking_turn,
            message_id="reasoning:opaque-active",
        )
        await page.reload()
        opaque_turn = page.locator(".structured-turn").filter(has_text="Opaque reasoning")
        opaque_status = opaque_turn.locator(".reasoning-status")
        await expect(opaque_status).to_have_count(1)
        await expect(opaque_status).to_contain_text("Thinking…")
        active_indicator = opaque_status.locator(".thinking-indicator--active")
        await expect(active_indicator).to_be_visible()
        assert await active_indicator.evaluate(
            "element => getComputedStyle(element).animationName !== 'none'"
        ), "active thinking indicator must animate when motion is allowed"
        await page.screenshot(path=str(output / "thinking-opaque-active.png"), full_page=True)

        # Snapshot two: providers can emit a burst of separate terminal
        # snapshots. Ten adjacent Done records still represent one reasoning
        # run, so the transcript must keep one compact row.
        for index in range(10):
            append_thinking_event(
                "status",
                {
                    "text": "Done thinking",
                    "provider_status": "reasoning",
                    "snapshot": True,
                },
                turn_id=thinking_turn,
                message_id=f"reasoning:opaque-done-{index}",
            )
        await page.reload()
        opaque_turn = page.locator(".structured-turn").filter(has_text="Opaque reasoning")
        opaque_status = opaque_turn.locator(".reasoning-status")
        await expect(opaque_status).to_have_count(1)
        await expect(opaque_status).to_contain_text("Thought")
        await expect(opaque_status.locator(".thinking-indicator--active")).to_have_count(0)
        append_thinking_event(
            "turn_completed",
            {"status": "completed"},
            turn_id=thinking_turn,
            message_id=thinking_turn,
        )

        # Plaintext reasoning incorporates the live state in its own summary;
        # there is no duplicate opaque status row. A streamed Done update must
        # not close details the reader already opened.
        append_thinking_event(
            "turn_started", {"summary": "Plaintext reasoning"}, turn_id=plaintext_turn
        )
        append_thinking_event(
            "status",
            {"text": "Thinking…", "provider_status": "reasoning", "snapshot": True},
            turn_id=plaintext_turn,
            message_id="reasoning:plaintext",
        )
        append_thinking_event(
            "thinking_delta",
            {"text": "Inspect the fixture without exposing another status row."},
            turn_id=plaintext_turn,
            message_id="plaintext-detail",
        )
        plaintext = page.locator(".structured-turn").filter(has_text="Plaintext reasoning")
        card = plaintext.locator(".thinking-card")
        await expect(card).to_have_count(1)
        await expect(plaintext.locator(".reasoning-status")).to_have_count(0)
        await expect(card.locator("summary")).to_contain_text("Thinking…")
        await card.locator("summary").click()
        assert await card.evaluate("element => element.open")
        await expect(card.locator(".thinking-body")).to_contain_text("Inspect the fixture")
        await page.screenshot(path=str(output / "thinking-plaintext-active.png"), full_page=True)
        append_thinking_event(
            "status",
            {"text": "Done thinking", "provider_status": "reasoning", "snapshot": True},
            turn_id=plaintext_turn,
            message_id="reasoning:plaintext",
        )
        await expect(card.locator("summary")).to_contain_text("Thought")
        assert await card.evaluate(
            "element => element.open"
        ), "stream update closed opened reasoning"
        await page.screenshot(path=str(output / "thinking-plaintext-done.png"), full_page=True)

        # A tool is a hard boundary: later opaque reasoning stays a separate
        # run instead of being folded into the earlier plaintext card.
        append_thinking_event(
            "tool_call_started",
            {
                "name": "Read",
                "args": {"path": "/fixture"},
                "tool_call_id": "thinking-tool",
            },
            turn_id=plaintext_turn,
            message_id="thinking-tool",
        )
        append_thinking_event(
            "tool_call_completed",
            {
                "name": "Read",
                "result": "ok",
                "status": "completed",
                "tool_call_id": "thinking-tool",
            },
            turn_id=plaintext_turn,
            message_id="thinking-tool",
        )
        append_thinking_event(
            "status",
            {"text": "Thinking…", "provider_status": "reasoning", "snapshot": True},
            turn_id=plaintext_turn,
            message_id="reasoning:after-tool",
        )
        append_thinking_event(
            "status",
            {"text": "Done thinking", "provider_status": "reasoning", "snapshot": True},
            turn_id=plaintext_turn,
            message_id="reasoning:after-tool",
        )
        await expect(plaintext.locator(".thinking-card")).to_have_count(1)
        await expect(plaintext.locator(".reasoning-status")).to_have_count(1)
        await expect(plaintext.locator(".tool-card")).to_contain_text("Read")
        await expect(plaintext.locator(".reasoning-status")).to_contain_text("Thought")
        append_thinking_event(
            "text_delta",
            {"text": "Plaintext answer."},
            turn_id=plaintext_turn,
            message_id="plaintext-answer",
        )
        append_thinking_event(
            "turn_completed",
            {"status": "completed"},
            turn_id=plaintext_turn,
            message_id=plaintext_turn,
        )

        # Terminal and error boundaries stop active animation and use the
        # interrupted label. The separate turns also prove that reasoning runs
        # are scoped by turn rather than merged globally.
        append_thinking_event(
            "turn_started", {"summary": "Cancelled reasoning"}, turn_id=interrupted_turn
        )
        append_thinking_event(
            "status",
            {"text": "Thinking…", "provider_status": "reasoning", "snapshot": True},
            turn_id=interrupted_turn,
            message_id="reasoning:cancelled",
        )
        append_thinking_event(
            "status",
            {
                "text": "Thinking…",
                "provider_status": "reasoning",
                "snapshot": True,
                "subagent_thread": "thinking-child-done",
            },
            turn_id=interrupted_turn,
            message_id="reasoning:child-done",
        )
        append_thinking_event(
            "status",
            {
                "text": "Done thinking",
                "provider_status": "reasoning",
                "snapshot": True,
                "subagent_thread": "thinking-child-done",
            },
            turn_id=interrupted_turn,
            message_id="reasoning:child-done",
        )
        append_thinking_event(
            "status",
            {
                "text": "Thinking…",
                "provider_status": "reasoning",
                "snapshot": True,
                "subagent_thread": "thinking-child-cancelled",
            },
            turn_id=interrupted_turn,
            message_id="reasoning:child-cancelled",
        )
        append_thinking_event(
            "status",
            {
                "text": "Sub-agent turn cancelled",
                "provider_status": "subagent_lifecycle",
                "subagent_status": "cancelled",
                "subagent_thread": "thinking-child-cancelled",
            },
            turn_id=interrupted_turn,
            message_id="lifecycle:child-cancelled",
        )
        append_thinking_event(
            "status",
            {
                "text": "Thinking…",
                "provider_status": "reasoning",
                "snapshot": True,
                "subagent_thread": "thinking-child-still-active",
            },
            turn_id=interrupted_turn,
            message_id="reasoning:child-still-active",
        )
        append_thinking_event(
            "turn_completed",
            {"status": "interrupted"},
            turn_id=interrupted_turn,
            message_id=interrupted_turn,
        )
        cancelled = page.locator(".structured-turn").filter(has_text="Cancelled reasoning")
        main_cancelled_status = cancelled.locator(":scope > .reasoning-status")
        await expect(main_cancelled_status).to_contain_text("Thinking interrupted")
        await expect(main_cancelled_status.locator(".thinking-indicator--active")).to_have_count(0)
        child_region = cancelled.get_by_test_id("subagent-region")
        await expect(child_region).to_have_count(1)
        hidden_indicator = child_region.locator(".thinking-indicator--active")
        await expect(hidden_indicator).to_have_count(1)
        await expect(hidden_indicator).to_have_css("animation-name", "none")
        assert await hidden_indicator.evaluate("element => element.getAnimations().length === 0")
        await child_region.locator(":scope > summary").click()
        child_rows = child_region.locator(".subagent-thread-row")
        await expect(child_rows).to_have_count(3)
        done_child = child_region.locator(
            '.subagent-thread-row[data-subagent-thread-id="thinking-child-done"]'
        )
        cancelled_child = child_region.locator(
            '.subagent-thread-row[data-subagent-thread-id="thinking-child-cancelled"]'
        )
        active_child = child_region.locator(
            '.subagent-thread-row[data-subagent-thread-id="thinking-child-still-active"]'
        )
        await expect(done_child.locator(".reasoning-status")).to_contain_text("Thought")
        await expect(cancelled_child.locator(".reasoning-status")).to_contain_text(
            "Thinking interrupted"
        )
        await expect(cancelled_child.locator(".subthread-status")).to_contain_text(
            "Sub-agent turn cancelled"
        )
        await expect(active_child.locator(".reasoning-status")).to_contain_text("Thinking…")
        await expect(active_child.locator(".thinking-indicator--active")).to_have_count(1)
        # Expand each scoped thread with ordinary summary clicks before the
        # narrow-layout visual check. Hidden details content can retain layout
        # boxes under its sticky summary; it is not readable evidence until the
        # user opens the thread.
        for child in (done_child, cancelled_child, active_child):
            await child.locator(":scope > summary").click()
            assert await child.evaluate("element => element.open")

        # Native details keeps hidden CSS timelines alive unless the ancestor
        # explicitly disables them. Test both the thread and whole-region fold.
        active_indicator = active_child.locator(".thinking-indicator--active")
        await expect(active_indicator).not_to_have_css("animation-name", "none")
        assert await active_indicator.evaluate(
            "element => element.getAnimations().some(animation => animation.playState === 'running')"
        )
        for fold in (active_child, child_region):
            await fold.locator(":scope > summary").click()
            await expect(active_indicator).to_have_css("animation-name", "none")
            assert await active_indicator.evaluate(
                "element => element.getAnimations().length === 0"
            )
            await fold.locator(":scope > summary").click()
            await expect(active_indicator).not_to_have_css("animation-name", "none")

        await page.emulate_media(reduced_motion="reduce")
        append_thinking_event("turn_started", {"summary": "Errored reasoning"}, turn_id=error_turn)
        append_thinking_event(
            "status",
            {"text": "Thinking…", "provider_status": "reasoning", "snapshot": True},
            turn_id=error_turn,
            message_id="reasoning:error",
        )
        errored = page.locator(".structured-turn").filter(has_text="Errored reasoning")
        reduced_indicator = errored.locator(".thinking-indicator--active")
        await expect(reduced_indicator).to_be_visible()
        assert await reduced_indicator.evaluate(
            "element => getComputedStyle(element).animationName === 'none'"
        ), "reduced motion must disable the active indicator animation"
        append_thinking_event(
            "error",
            {
                "message": "Fixture provider failed",
                "detail": "bounded acceptance error",
            },
            turn_id=error_turn,
            message_id="thinking-error",
        )
        await expect(errored.locator(".reasoning-status")).to_contain_text("Thinking interrupted")
        await expect(errored.locator(".thinking-indicator--active")).to_have_count(0)
        await expect(errored).to_contain_text("Fixture provider failed")

        # The compact rows remain contained at the narrow breakpoint. This is
        # screenshot evidence for active/done/plaintext/tool/error presentation.
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.wait_for_function(
            "document.querySelector('.chat-sidebar').classList.contains('mobile-drawer')"
        )
        sidebar = page.locator(".chat-sidebar")
        if "mobile-drawer--open" in (await sidebar.get_attribute("class") or ""):
            await page.get_by_role("button", name="Close sessions", exact=True).click()
        await page.wait_for_function(
            "document.querySelector('.chat-sidebar').getBoundingClientRect().right <= 1"
        )
        thinking_rows = page.locator(".reasoning-status, .thinking-card")
        for row in await thinking_rows.all():
            await row.scroll_into_view_if_needed()
            bounds = await row.bounding_box()
            assert bounds is not None and bounds["x"] >= 0
            assert bounds["x"] + bounds["width"] <= 391, bounds
            coverage = await row.evaluate("""element => {
              const bounds = element.getBoundingClientRect()
              const hit = document.elementFromPoint(
                bounds.left + bounds.width / 2,
                bounds.top + bounds.height / 2,
              )
              return {
                visible: hit !== null && (hit === element || element.contains(hit)),
                rowClass: element.className,
                rowText: element.textContent,
                bounds: bounds.toJSON(),
                hitTag: hit?.tagName ?? null,
                hitClass: hit?.className ?? null,
                hitText: hit?.textContent ?? null,
              }
            }""")
            if not coverage["visible"]:
                await page.screenshot(
                    path=str(output / "thinking-mobile-covered-debug.png"),
                    full_page=True,
                )
            assert coverage["visible"], coverage
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        await page.screenshot(
            path=str(output / "thinking-mobile-reduced-motion.png"), full_page=True
        )
        await page.emulate_media(reduced_motion="no-preference")
        await page.set_viewport_size({"width": 1280, "height": 900})

        # Tool details are deliberately expensive and must only exist while the
        # native details card is open. Keep screenshots for this bounded fixture
        # separate from the composer evidence above.
        tool_evidence = output / "chat-tool-ui"
        tool_evidence.mkdir()
        bulk_turn = "fixture-tool-bulk"
        later_turn = "fixture-tool-later"
        live_tool_turn = "fixture-tool-live"
        sequence = 0
        state["events"] = []
        large_input = "INPUT-BODY-" + ("0123456789abcdef" * 1024)
        large_result = "\n".join(f"result-line-{line:04d}" for line in range(1100))

        append_thinking_event("turn_started", {"summary": "Bulk tools"}, turn_id=bulk_turn)
        for index in range(120):
            tool_id = f"bulk-tool-{index:03d}"
            tool_name = f"Tool{index:03d}"
            append_thinking_event(
                "tool_call_started",
                {
                    "name": tool_name,
                    "args": {
                        "marker": f"INPUT-{index:03d}-START",
                        "blob": large_input,
                        "end": f"INPUT-{index:03d}-END",
                    },
                    "tool_call_id": tool_id,
                },
                turn_id=bulk_turn,
                message_id=tool_id,
            )
            status = "failed" if index == 1 else "completed"
            append_thinking_event(
                "tool_call_completed",
                {
                    "name": tool_name,
                    "result": (f"RESULT-{index:03d}-START\n{large_result}\nRESULT-{index:03d}-END"),
                    "status": status,
                    "tool_call_id": tool_id,
                },
                turn_id=bulk_turn,
                message_id=tool_id,
            )
        append_thinking_event(
            "text_delta",
            {"text": "Bulk tool answer."},
            turn_id=bulk_turn,
            message_id="bulk-answer",
        )
        append_thinking_event(
            "turn_completed",
            {"status": "completed"},
            turn_id=bulk_turn,
            message_id=bulk_turn,
        )
        append_thinking_event("turn_started", {"summary": "Later turn"}, turn_id=later_turn)
        append_thinking_event(
            "text_delta",
            {"text": "Later answer."},
            turn_id=later_turn,
            message_id="later-answer",
        )
        append_thinking_event(
            "turn_completed",
            {"status": "completed"},
            turn_id=later_turn,
            message_id=later_turn,
        )
        await page.reload()

        bulk = page.locator(".structured-turn").filter(has_text="Bulk tools")
        process_fold = bulk.locator(".process-fold")
        await expect(process_fold).to_have_attribute("aria-expanded", "false")
        await process_fold.click()
        await expect(process_fold).to_have_attribute("aria-expanded", "true")
        bulk_card = bulk.locator(".tool-card--group")
        await expect(bulk_card).to_have_count(1)
        await expect(bulk_card.locator(".tool-group-item")).to_have_count(0)
        await expect(bulk_card.locator(".tool-block")).to_have_count(0)
        bulk_names = bulk_card.locator(":scope > summary .tool-group-names")
        await expect(bulk_names).to_contain_text("Tool000")
        await expect(bulk_names).to_contain_text("Tool001")
        await expect(bulk_names).to_contain_text("Tool002")
        await expect(bulk_names).to_contain_text("117 more")
        await expect(bulk_names).not_to_contain_text("Tool003")
        await page.screenshot(path=str(tool_evidence / "bulk-collapsed.png"), full_page=True)

        # This historical turn is memoized, but its native details toggle must
        # still mount all complete tool text on an ordinary click.
        await bulk_card.locator(":scope > summary").click()
        assert await bulk_card.evaluate("element => element.open")
        await expect(bulk_card.locator(".tool-group-item")).to_have_count(120)
        await expect(bulk_card.locator(".tool-block")).to_have_count(240)
        completed_item = bulk_card.locator(".tool-group-item").nth(0)
        failed_item = bulk_card.locator(".tool-group-item").nth(1)
        second_completed_item = bulk_card.locator(".tool-group-item").nth(2)
        await expect(completed_item.locator(".tool-status")).to_have_text("Completed")
        await expect(failed_item.locator(".tool-status")).to_have_text("Failed")
        await expect(second_completed_item.locator(".tool-status")).to_have_text("Completed")
        for item in (completed_item, failed_item, second_completed_item):
            assert await item.locator(".tool-status").evaluate(
                "element => getComputedStyle(element).animationName === 'none'"
            ), "tool status must be a static indicator"

        tool_input = completed_item.locator('pre[aria-label="Tool input"]')
        tool_result = completed_item.locator('pre[aria-label="Tool result"]')
        await expect(tool_input).to_have_attribute("tabindex", "0")
        await expect(tool_result).to_have_attribute("tabindex", "0")
        await expect(tool_input).to_contain_text("INPUT-000-START")
        await expect(tool_input).to_contain_text("INPUT-000-END")
        await expect(tool_result).to_contain_text("RESULT-000-START")
        await expect(tool_result).to_contain_text("RESULT-000-END")
        assert await tool_input.evaluate("element => element.scrollWidth > element.clientWidth")
        await tool_input.focus()
        for _ in range(12):
            await tool_input.press("ArrowRight")
        assert await tool_input.evaluate("element => element.scrollLeft > 0")
        assert await tool_result.evaluate("element => element.scrollHeight > element.clientHeight")
        await tool_result.focus()
        await tool_result.press("PageDown")
        # Keyboard scrolling is applied on a later browser frame.
        await page.wait_for_function(
            "element => element.scrollTop > 0", arg=await tool_result.element_handle()
        )
        await page.screenshot(path=str(tool_evidence / "bulk-expanded.png"), full_page=True)

        # Folding the memoized historical process unmounts the card, but its
        # controlled native-open state survives the remount with complete body
        # content. Two rapid native summary clicks must likewise leave the DOM
        # synchronized with the final details.open value.
        await process_fold.click()
        await expect(process_fold).to_have_attribute("aria-expanded", "false")
        await expect(bulk_card).to_have_count(0)
        await process_fold.click()
        await expect(process_fold).to_have_attribute("aria-expanded", "true")
        await expect(bulk_card).to_have_count(1)
        assert await bulk_card.evaluate("element => element.open")
        await expect(bulk_card.locator(".tool-group-item")).to_have_count(120)
        await bulk_card.locator(":scope > summary").dblclick()
        open_after_double_click = await bulk_card.evaluate("element => element.open")
        expected_item_count = 120 if open_after_double_click else 0
        await expect(bulk_card.locator(".tool-group-item")).to_have_count(expected_item_count)

        if open_after_double_click:
            await bulk_card.locator(":scope > summary").click()
        assert not await bulk_card.evaluate("element => element.open")
        await expect(bulk_card.locator(".tool-group-item")).to_have_count(0)
        await expect(bulk_card.locator(".tool-block")).to_have_count(0)

        # A live card stays open while its completion event replaces Running
        # with Completed and mounts the full result.
        live_tool_id = "live-tool"
        append_thinking_event("turn_started", {"summary": "Live tool"}, turn_id=live_tool_turn)
        append_thinking_event(
            "tool_call_started",
            {
                "name": "LiveRead",
                "args": {"marker": "LIVE-INPUT", "blob": large_input},
                "tool_call_id": live_tool_id,
            },
            turn_id=live_tool_turn,
            message_id=live_tool_id,
        )
        live_turn = page.locator(".structured-turn").filter(has_text="Live tool")
        live_card = live_turn.locator(".tool-card--group")
        live_summary_status = live_card.locator(":scope > summary .tool-status")
        await expect(live_summary_status).to_have_text("Running")
        assert await live_summary_status.evaluate(
            "element => getComputedStyle(element).animationName === 'none'"
        )
        await expect(live_card.locator(".tool-group-item")).to_have_count(0)
        await live_card.locator(":scope > summary").click()
        assert await live_card.evaluate("element => element.open")
        await expect(live_card.locator(".tool-group-item")).to_have_count(1)
        await expect(live_card.locator('pre[aria-label="Tool input"]')).to_contain_text(
            "LIVE-INPUT"
        )
        append_thinking_event(
            "tool_call_completed",
            {
                "name": "LiveRead",
                "result": f"LIVE-RESULT-START\n{large_result}\nLIVE-RESULT-END",
                "status": "completed",
                "tool_call_id": live_tool_id,
            },
            turn_id=live_tool_turn,
            message_id=live_tool_id,
        )
        await expect(live_summary_status).to_have_text("Completed")
        assert await live_card.evaluate("element => element.open"), "stream update closed tool"
        await expect(live_card.locator('pre[aria-label="Tool result"]')).to_contain_text(
            "LIVE-RESULT-END"
        )

        # Child-thread groups use the same lazy body contract and compact name
        # summary, independently of the main tool card.
        child_thread = "fixture-tool-child"
        for index in range(4):
            tool_id = f"child-tool-{index}"
            append_thinking_event(
                "tool_call_started",
                {
                    "name": f"ChildTool{index}",
                    "args": {"marker": f"CHILD-INPUT-{index}"},
                    "tool_call_id": tool_id,
                    "subagent_thread": child_thread,
                },
                turn_id=live_tool_turn,
                message_id=tool_id,
            )
            append_thinking_event(
                "tool_call_completed",
                {
                    "name": f"ChildTool{index}",
                    "result": f"CHILD-RESULT-{index}",
                    "status": "completed",
                    "tool_call_id": tool_id,
                    "subagent_thread": child_thread,
                },
                turn_id=live_tool_turn,
                message_id=tool_id,
            )
        child_region = live_turn.get_by_test_id("subagent-region")
        await child_region.locator(":scope > summary").click()
        child_row = child_region.locator(
            f'.subagent-thread-row[data-subagent-thread-id="{child_thread}"]'
        )
        await child_row.locator(":scope > summary").click()
        child_card = child_row.locator(".tool-card--group")
        await expect(child_card).to_have_count(1)
        await expect(child_card.locator(".tool-group-item")).to_have_count(0)
        await expect(child_card.locator(".tool-block")).to_have_count(0)
        await expect(child_card.locator(".tool-group-names")).to_contain_text("ChildTool0")
        await expect(child_card.locator(".tool-group-names")).to_contain_text("ChildTool1")
        await expect(child_card.locator(".tool-group-names")).to_contain_text("ChildTool2")
        await expect(child_card.locator(".tool-group-names")).to_contain_text("1 more")
        await expect(child_card.locator(".tool-group-names")).not_to_contain_text("ChildTool3")
        await child_card.locator(":scope > summary").click()
        assert await child_card.evaluate("element => element.open")
        await expect(child_card.locator(".tool-group-item")).to_have_count(4)
        await expect(child_card.locator('pre[aria-label="Tool result"]')).to_have_count(4)
        await expect(child_card.locator('pre[aria-label="Tool result"]').last).to_contain_text(
            "CHILD-RESULT-3"
        )
        await child_card.locator(":scope > summary").click()
        await expect(child_card.locator(".tool-group-item")).to_have_count(0)

        # Narrow summaries and the one deliberately open live body stay inside
        # the viewport and are not covered by the closed mobile drawer.
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.wait_for_function(
            "document.querySelector('.chat-sidebar').classList.contains('mobile-drawer')"
        )
        sidebar = page.locator(".chat-sidebar")
        if "mobile-drawer--open" in (await sidebar.get_attribute("class") or ""):
            await page.get_by_role("button", name="Close sessions", exact=True).click()
        await page.wait_for_function(
            "document.querySelector('.chat-sidebar').getBoundingClientRect().right <= 1"
        )
        for card in await page.locator(".tool-card--group").all():
            await card.scroll_into_view_if_needed()
            bounds = await card.bounding_box()
            assert bounds is not None and bounds["x"] >= 0
            assert bounds["x"] + bounds["width"] <= 391, bounds
            assert await card.locator(":scope > summary").evaluate("""summary => {
              const bounds = summary.getBoundingClientRect()
              const hit = document.elementFromPoint(
                bounds.left + bounds.width / 2,
                bounds.top + bounds.height / 2,
              )
              return hit !== null && (hit === summary || summary.contains(hit))
            }"""), "tool summary center is covered at the narrow breakpoint"
        for block in await live_card.locator(".tool-block").all():
            bounds = await block.bounding_box()
            assert bounds is not None and bounds["x"] >= 0
            assert bounds["x"] + bounds["width"] <= 391, bounds
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        await page.screenshot(path=str(tool_evidence / "mobile.png"), full_page=True)
        await page.set_viewport_size({"width": 1280, "height": 900})

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

        # A failed provider remains diagnosable without treating its text as HTML
        # or resending the old task when the user explicitly replaces the process.
        await page.locator(".modal").get_by_role("button", name="Cancel", exact=True).click()
        tab["agent_type"] = "antigravity"
        state["events"] = list(EVENTS)
        state["wait_detail"] = (
            "provider exited with code 1: authentication failed or timed out; "
            '<img src="x" onerror="window.errorDetailExecuted=true"> '
            + "connection-diagnostic-" * 12
        )
        sends_before_auth_retry = len(sends)
        await page.goto(base + f"/?tab={TAB}")
        alert = page.locator(".structured-banner[role='alert']")
        await expect(alert).to_contain_text("wait HTTP 409: provider exited with code 1")
        await expect(alert).to_contain_text("authentication failed or timed out")
        await expect(alert).to_contain_text("local agyx network and sign-in")
        await expect(alert).to_contain_text("Retry does not resend your message.")
        await expect(alert.locator("img")).to_have_count(0)
        assert not await page.evaluate("Boolean(window.errorDetailExecuted)")
        await expect(page.get_by_text("First answer.", exact=True)).to_have_count(1)
        await expect(composer).to_be_disabled()
        for width, height in ((1280, 900), (720, 900), (390, 844)):
            await page.set_viewport_size({"width": width, "height": height})
            for selector in (".banner-message", ".banner-guidance", ".banner-retry"):
                bounds = await alert.locator(selector).bounding_box()
                assert bounds and bounds["x"] >= 0
                assert bounds["x"] + bounds["width"] <= width + 1, (selector, bounds)
            for selector in (".banner-message", ".banner-guidance"):
                assert await alert.locator(selector).evaluate("""element => {
                  const chrome = element.closest('.terminal-pane').querySelector('.pane-chrome')
                  const overlay = chrome.getBoundingClientRect()
                  const range = document.createRange()
                  range.selectNodeContents(element)
                  return [...range.getClientRects()].every(rect =>
                    rect.bottom <= overlay.top || rect.top >= overlay.bottom ||
                    rect.right <= overlay.left || rect.left >= overlay.right,
                  )
                }"""), f"{selector} text is covered by the pane header"
            assert await alert.locator(".banner-retry").evaluate("""button => {
              const bounds = button.getBoundingClientRect()
              const hit = document.elementFromPoint(
                bounds.left + bounds.width / 2, bounds.top + bounds.height / 2,
              )
              return hit !== null && (hit === button || button.contains(hit))
            }"""), "provider Retry is covered"
            await page.screenshot(path=str(output / f"antigravity-error-{width}.png"))
        state["wait_detail"] = None
        retries_before = retry_requests.count(f"{BASE}/retry")
        await alert.get_by_role("button", name="Retry", exact=True).click()
        await expect(alert).to_have_count(0)
        await expect(composer).to_be_enabled()
        await expect(page.get_by_text("First answer.", exact=True)).to_have_count(1)
        assert retry_requests.count(f"{BASE}/retry") == retries_before + 1
        assert len(sends) == sends_before_auth_retry, "Retry resent the previous task"

        # Provider stderr can fill the backend's 64 KiB tail. Keep every byte
        # available without letting the diagnostic push Retry and history away.
        detail_start = "provider exited with code 1: authentication failed; DETAILS-START\n"
        detail_end = "\nDETAILS-END-65536"
        long_detail = (
            detail_start + "x" * (65536 - len(detail_start) - len(detail_end)) + detail_end
        )
        assert len(long_detail.encode()) == 65536
        for width, height in ((1280, 900), (390, 844)):
            await page.set_viewport_size({"width": width, "height": height})
            state["wait_detail"] = long_detail
            await page.reload()
            details = alert.get_by_role("region", name="Stream error details", exact=True)
            await expect(details).to_be_visible()
            await expect(details).to_have_attribute("tabindex", "0")
            assert await details.text_content() == f"wait HTTP 409: {long_detail}"
            assert await details.evaluate("element => element.scrollHeight > element.clientHeight")
            details_box = await details.bounding_box()
            retry_button = alert.get_by_role("button", name="Retry", exact=True)
            retry_box = await retry_button.bounding_box()
            assert details_box and retry_box
            assert details_box["height"] <= min(160, height * 0.25) + 1, details_box
            assert retry_box["y"] <= details_box["y"] + 1, (retry_box, details_box)
            await expect(retry_button).to_be_in_viewport(ratio=1)
            await expect(page.get_by_text("First answer.", exact=True)).to_be_in_viewport(ratio=1)
            await expect(composer).to_be_in_viewport(ratio=1)
            await details.focus()
            await details.press("Control+End")
            await page.wait_for_function("""() => {
              const details = document.querySelector('.banner-message')
              return details.scrollTop + details.clientHeight >= details.scrollHeight - 2
            }""")
            assert await details.evaluate("""element => {
              const node = element.lastChild
              const range = document.createRange()
              range.setStart(node, node.textContent.length - 'DETAILS-END-65536'.length)
              range.setEnd(node, node.textContent.length)
              const end = range.getBoundingClientRect()
              const viewport = element.getBoundingClientRect()
              return end.top >= viewport.top && end.bottom <= viewport.bottom + 1
            }"""), "keyboard scrolling did not reveal the end of the full diagnostic"
            await page.screenshot(path=str(output / f"antigravity-large-error-{width}.png"))
            state["wait_detail"] = None
            retries_before = retry_requests.count(f"{BASE}/retry")
            await retry_button.click()
            await expect(alert).to_have_count(0)
            await expect(composer).to_be_enabled()
            await expect(page.get_by_text("First answer.", exact=True)).to_have_count(1)
            assert retry_requests.count(f"{BASE}/retry") == retries_before + 1
            assert len(sends) == sends_before_auth_retry, "Retry resent the previous task"

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
                    "capability_requests": capability_requests,
                    "retry_requests": retry_requests,
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
