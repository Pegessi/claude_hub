"""Managed Chat blocking-question acceptance with every API boundary mocked.

Covers the user-visible regression contract that source-level tests cannot prove:
a question card remains usable while its provider turn is busy, a lost HTTP
response does not resolve the card, and the explicit retry preserves both the
Hub client turn id and exact provider ``(turn_id, call_id)`` ownership.

Run only through ``scripts/verify.sh browser-mocks`` or with a dedicated Vite
server, never shared ports 5173/8173.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from browser_lifecycle import BrowserRun, run_browser_main
from playwright.async_api import Route, WebSocketRoute, expect

TAB = "fixture-managed-question-tab"
SESSION = "fixture-managed-question-session"
TURN = "fixture-managed-question-turn"
CALL = "fixture-managed-question-call"
NOW = "2026-10-15T10:00:00Z"
BUSY_TEXT = "Durable busy follow-up"
STREAM_BASE = f"/api/workspaces/sessions/{SESSION}/stream"
HARNESS_PATH = "/managed-chat-question-harness"


def event(sequence: int, event_type: str, payload: dict, *, call_id: str | None = None) -> dict:
    return {
        "stream_sequence": sequence,
        "session_id": SESSION,
        "tab_id": TAB,
        "agent_type": "codex",
        "type": event_type,
        "turn_id": TURN,
        "message_id": None,
        "call_id": call_id,
        "payload": payload,
        "created_at": NOW,
        "redacted": False,
    }


EVENTS = [
    event(0, "turn_started", {"summary": "Need an exact choice"}),
    event(
        1,
        "approval_required",
        {
            "tool_call_id": CALL,
            "kind": "ask_question",
            "title": "Choose a color",
            "questions": [
                {
                    "id": "color",
                    "prompt": "Pick one",
                    "options": [
                        {"id": "red", "label": "Red"},
                        {"id": "blue", "label": "Blue"},
                    ],
                }
            ],
        },
        call_id=CALL,
    ),
]

CAPABILITIES = {
    "structured": True,
    "adapter_id": "codex-app-server",
    "schema_version": 1,
    "sources": [],
    "supports_approval_ui": True,
    "supports_tool_timeline": True,
    "supports_images": True,
    # Managed Task ownership must hide these even if a provider advertises them.
    "supports_goals": True,
    "supports_dynamic_modes": True,
    "current_mode": "default",
    "available_modes": [
        {"id": "default", "label": "Agent"},
        {"id": "plan", "label": "Plan"},
    ],
    "available_models": [],
    "current_model": None,
    "current_reasoning_effort": None,
}


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
    unknown_requests: list[tuple[str, str]] = []
    external_requests: list[str] = []
    sends: list[dict] = []
    busy_sends: list[dict] = []

    async with browser_run.context(viewport={"width": 1280, "height": 900}) as context:
        component_response = await context.request.get(f"{base}/src/components/StructuredPane.vue")
        component = await component_response.text()
        store_response = await context.request.get(f"{base}/src/stores/terminalStore.ts")
        store_source = await store_response.text()
        vue_match = re.search(r'from "([^"]*/vue.js[^"]*)"', component)
        pinia_match = re.search(r'from "([^"]*/pinia.js[^"]*)"', store_source)
        if vue_match is None or pinia_match is None:
            raise RuntimeError("Vite did not expose the expected Vue/Pinia module URLs")
        vue_url = vue_match.group(1)
        pinia_url = pinia_match.group(1)
        harness = f"""<!doctype html><html><body>
        <div id="app" style="height:100dvh"></div>
        <script type="module">
        import {{createApp, h, KeepAlive}} from '{vue_url}';
        import {{createPinia}} from '{pinia_url}';
        import '/src/App.vue';
        import StructuredPane from '/src/components/StructuredPane.vue';
        import {{useTerminalStore}} from '/src/stores/terminalStore.ts';
        const pinia = createPinia();
        const app = createApp({{setup() {{
          useTerminalStore().tabs = [{{
            id:{json.dumps(TAB)}, name:'Managed question', agent_type:'codex', env:{{}},
            session_kind:'chat', target:'local', cwd:'/fixture/workspace',
            workspace_id:'fixture-workspace', workspace_role:'worker',
            managed_session_id:{json.dumps(SESSION)}
          }}];
          return () => h(KeepAlive, null, {{default: () => h(StructuredPane, {{
            tabId:{json.dumps(TAB)}, managedChat:true,
            managedSessionId:{json.dumps(SESSION)}, paneId:'fixture-pane'
          }})}});
        }}}});
        app.use(pinia).mount('#app');
        </script></body></html>"""

        async def block_websocket(socket: WebSocketRoute) -> None:
            await socket.close(code=1000, reason="mocked managed question acceptance")

        async def api(route: Route) -> None:
            request = route.request
            parsed = urlparse(request.url)
            if (parsed.scheme, parsed.hostname, parsed.port) != (
                parsed_base.scheme,
                parsed_base.hostname,
                parsed_base.port,
            ):
                external_requests.append(request.url)
                await route.abort("blockedbyclient")
                return
            path = parsed.path
            method = request.method
            if path == HARNESS_PATH:
                await route.fulfill(status=200, content_type="text/html", body=harness)
                return
            if not path.startswith("/api/"):
                await route.fallback()
                return
            if method == "GET" and path == f"{STREAM_BASE}/capabilities":
                body: object = CAPABILITIES
            elif method == "GET" and path == f"{STREAM_BASE}/events":
                body = {"events": EVENTS, "next_sequence": 1, "has_more": False}
            elif method == "POST" and path == f"{STREAM_BASE}/wait":
                await asyncio.sleep(0.05)
                body = {"events": [], "next_sequence": 1, "has_more": False}
            elif method == "POST" and path == f"{STREAM_BASE}/send":
                payload = json.loads(request.post_data or "{}")
                if payload.get("text") == BUSY_TEXT:
                    busy_sends.append(payload)
                    body = {"ok": True}
                else:
                    sends.append(payload)
                    if len(sends) == 1:
                        # Model the provider accepting the exact answer while the
                        # browser loses the acknowledgement. The card must stay open
                        # and its explicit retry must preserve the idempotency key.
                        await route.fulfill(
                            status=503,
                            content_type="application/json",
                            body=json.dumps({"detail": "response lost after provider accepted"}),
                        )
                        return
                    body = {"ok": True, "answered_question": True}
            elif method == "GET" and path == "/api/tabs/status":
                body = []
            else:
                unknown_requests.append((method, path))
                await route.fulfill(
                    status=599,
                    content_type="application/json",
                    body=json.dumps({"detail": f"unexpected mocked API request: {method} {path}"}),
                )
                return
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(body),
            )

        await context.route("**/*", api)
        await context.route_web_socket("**/*", block_websocket)
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.add_init_script("window.EventSource = undefined")
        await page.goto(f"{base}{HARNESS_PATH}")

        await expect(page.get_by_text("Choose a color", exact=True)).to_be_visible()
        await expect(page.get_by_role("button", name="Stop", exact=True)).to_be_visible()
        await expect(page.get_by_role("button", name=re.compile(r"Chat mode:"))).to_have_count(0)

        await page.get_by_role("button", name="Add to chat", exact=True).click()
        await expect(page.get_by_role("menuitem", name="Set a Goal", exact=True)).to_have_count(0)
        await expect(
            page.get_by_role("menuitem", name="Create scheduled task", exact=True)
        ).to_have_count(0)
        await page.keyboard.press("Escape")

        # Plain Enter during the active managed turn must POST immediately to
        # the managed-session mailbox. A browser-only queue would clear the
        # textarea without issuing this request and lose it on reload.
        composer = page.get_by_placeholder("Send a message…")
        await composer.fill(BUSY_TEXT)
        await composer.press("Enter")
        await expect(composer).to_have_value("")
        deadline = asyncio.get_running_loop().time() + 2
        while not busy_sends and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        if len(busy_sends) != 1:
            raise AssertionError(f"managed busy Enter was not durably posted: {busy_sends!r}")
        await expect(page.locator(".composer-queue")).to_have_count(0)

        choice = page.get_by_role("group", name="Pick one").get_by_role(
            "button", name="Red", exact=True
        )
        submit = page.get_by_role("button", name="提交选择", exact=True)
        await choice.click()
        await submit.click()
        await expect(page.get_by_role("alert")).to_contain_text(
            "response lost after provider accepted"
        )
        await expect(page.get_by_text("已提交", exact=True)).to_have_count(0)
        await expect(submit).to_be_visible()

        await submit.click()
        await expect(page.get_by_text("已提交", exact=True)).to_be_visible()
        await expect(page.get_by_role("alert")).to_have_count(0)
        await page.screenshot(path=output / "managed-question-recovered.png", full_page=True)

    if len(busy_sends) != 1:
        raise AssertionError(f"expected one durable busy Enter send, got {busy_sends!r}")
    busy = busy_sends[0]
    if busy.get("delivery") != "normal" or busy.get("text") != BUSY_TEXT:
        raise AssertionError(f"managed busy Enter used the wrong delivery: {busy!r}")
    if "question_turn_id" in busy or "question_call_id" in busy:
        raise AssertionError(f"ordinary busy send was misclassified as a question answer: {busy!r}")
    if len(sends) != 2:
        raise AssertionError(f"expected lost-response retry, got sends={sends!r}")
    first, second = sends
    if first["client_turn_id"] != second["client_turn_id"]:
        raise AssertionError("lost-response retry changed the stable client turn id")
    for payload in sends:
        if payload.get("delivery") != "normal":
            raise AssertionError(f"managed question was not normal delivery: {payload!r}")
        if payload.get("question_turn_id") != TURN or payload.get("question_call_id") != CALL:
            raise AssertionError(f"managed question lost exact provider ownership: {payload!r}")
    if unknown_requests:
        raise AssertionError(f"unexpected mocked API requests: {unknown_requests!r}")
    if external_requests:
        raise AssertionError(f"browser attempted external requests: {external_requests!r}")
    if errors:
        raise AssertionError(f"browser page errors: {errors!r}")

    report = {
        "result": "PASS",
        "coverage": [
            "managed busy Enter posts immediately to the durable session mailbox",
            "managed question remains actionable while provider turn is busy",
            "lost answer response leaves the exact card unresolved",
            "explicit retry preserves client turn id and provider card identity",
            "managed surface hides Task-owned mode, Goal, and schedule controls",
        ],
        "send_attempts": len(sends),
        "unknown_requests": unknown_requests,
        "external_requests": external_requests,
        "page_errors": errors,
        "boundary": "All API requests mocked; no backend or provider request.",
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    run_browser_main(main)
