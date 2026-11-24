"""Real Feishu Bot settings and binding components with fully mocked APIs.

Usage:
  python tests/browser_feishu_bot_components.py \
    http://127.0.0.1:5295/tests/feishu_bot_components_harness.html \
    /tmp/feishu-bot-components EXISTING_BROWSER
"""

import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from browser_lifecycle import BrowserRun, run_browser_main
from playwright.async_api import expect

CLOCK = datetime.now(timezone.utc)
NOW = CLOCK.isoformat()
CODE_EXPIRES = (CLOCK + timedelta(seconds=600)).isoformat()
CLAIM_EXPIRES = (CLOCK + timedelta(seconds=300)).isoformat()
CONFIRM_WORD = "ABC234"
SHARED_AVATAR_URL = "https://avatar.example.test/shared-bear.png"
SHARED_AVATAR_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415408d763f8cfc0f01f00050001ff89993d1d0000000049454e44"
    "ae426082"
)


def bot_summary(
    bot_id: str,
    name: str,
    app_id: str,
    *,
    source: str,
    credentials_editable: bool,
    deletable: bool,
    mode: str = "single",
    binding: dict | None = None,
    bindings: list[dict] | None = None,
    feishu_name: str | None = None,
    feishu_avatar_url: str | None = None,
) -> dict:
    active_bindings = (
        list(bindings) if bindings is not None else ([binding] if binding is not None else [])
    )
    primary_binding = active_bindings[0] if active_bindings else binding
    return {
        "bot_id": bot_id,
        "name": name,
        "feishu_name": feishu_name,
        "feishu_avatar_url": feishu_avatar_url,
        "app_id": app_id,
        "source": source,
        "enabled": True,
        "credentials_editable": credentials_editable,
        "deletable": deletable,
        "revision": 1,
        "generation": 1,
        "configured": True,
        "app_secret_configured": True,
        "connection_status": "connected",
        "updated_at": NOW,
        "mode": mode,
        "binding": primary_binding,
        "bindings": active_bindings,
        "my_claims": [],
    }


async def main(browser_run: BrowserRun) -> None:
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    url, output_arg = sys.argv[1:3]
    parsed_base = urlparse(url)
    if (
        parsed_base.scheme != "http"
        or parsed_base.hostname not in ("127.0.0.1", "::1")
        or parsed_base.port in (None, 5173, 8173, 10025, 18183)
        or parsed_base.username is not None
        or parsed_base.password is not None
        or parsed_base.path != "/tests/feishu_bot_components_harness.html"
    ):
        raise SystemExit("Use an owned numeric-loopback Vite server and the Bot harness path")

    output = Path(output_arg)
    output.mkdir(parents=True, exist_ok=True)
    page_errors: list[str] = []
    blocked_requests: list[tuple[str, str]] = []
    unknown_requests: list[tuple[str, str]] = []
    api_calls: list[dict] = []
    response_bodies: list[str] = []
    pool_revision = 1
    shared_avatar_requests = 0
    ai_get_calls = 0
    ai_get_gate = asyncio.Event()
    ai_config: dict = {
        "enabled": False,
        "prefix": "/ai",
        "owner_open_id": "",
        "app_id": "",
        "provider": "maso",
        "model": "gemini-example-model-1dd583607df6_reasoning_high",
        "reasoning_effort": "high",
        "cwd": "/srv/persisted-ai",
        "watch_chats": ["oc_persisted"],
        "poll_interval_seconds": 15,
        "max_active_chats": 10,
        "max_context_messages": 20,
        "ack_emoji": "OnIt",
        "done_emoji": "DONE",
        "send_interim_reply": False,
        "footer": "> 🤖 AI 代答，仅供参考",
        "reply_in_thread": True,
    }
    ai_token_status: dict = {
        "has_token": True,
        "open_id": "ou_unconfigured",
        "user_name": "Pending Owner",
        "app_id": "cli_unconfigured",
        "source": "lark_cli",
        "expires_at": 9999999999,
        "token_expired": False,
        "granted_scopes": ["im:message"],
        "required_send_scope": "im:message.send_as_user",
        "can_send": False,
        "can_send_as_user": False,
        "has_send_as_user_scope": False,
        "can_send_via_bot": False,
        "fallback_bot_app_id": None,
        "send_mode": "none",
        "can_react_as_user": True,
        "can_read_messages": True,
        "missing_required_scopes": ["im:message.send_as_user"],
        "missing_recommended_scopes": [],
        "auth_login_command": 'lark-cli auth login --scope "im:message im:message.reactions:write_only"',
        "remediation_steps": [
            "1. Configure app secret for `cli_unconfigured` (or enable `im:message.send_as_user` in Feishu Developer Console for `cli_unconfigured`)."
        ],
    }

    def ai_command_payload(*, imported: bool | None = None) -> dict:
        payload: dict = {
            "config": json.loads(json.dumps(ai_config)),
            "token_status": json.loads(json.dumps(ai_token_status)),
            "poller_status": {
                "running": bool(ai_config["enabled"]),
                "poll_count": 1,
                "last_poll_at": 1700000000.0,
                "last_error": None,
                "commands_detected": 0,
                "commands_succeeded": 0,
                "commands_failed": 0,
                "skipped_count": 0,
                "watermarks_count": 1,
            },
            "capabilities": {
                "providers": [
                    "claude",
                    "codex",
                    "traex",
                    "maso",
                    "cursor",
                    "antigravity",
                ],
                "reasoning_efforts": [
                    "default",
                    "none",
                    "minimal",
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                    "max",
                    "ultra",
                ],
                "provider_reasoning_efforts": {
                    "claude": [],
                    "codex": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
                    "traex": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
                    "maso": ["default", "none", "low", "medium", "high", "xhigh", "max"],
                    "cursor": [
                        "none",
                        "minimal",
                        "low",
                        "medium",
                        "high",
                        "xhigh",
                        "max",
                    ],
                    "antigravity": [],
                },
                "provider_models": {
                    "claude": ["claude-opus-4-8", "example-code-model"],
                    "codex": ["gpt-example-model-c886ad270fea", "gpt-5.4", "o3"],
                    "maso": [
                        "example-model-dedfefd51120_reasoning_high",
                        "gemini-example-model-1dd583607df6_reasoning_high",
                    ],
                    "cursor": ["gpt-5.3-codex-high", "claude-4.5-sonnet"],
                    "antigravity": ["gemini-example-model-1d11bad5f3fe-high"],
                },
                "provider_model_options": {
                    "claude": [
                        {
                            "id": "claude-opus-4-8",
                            "label": "claude-opus-4-8",
                            "provider_model_id": "claude-opus-4-8",
                        },
                        {
                            "id": "example-code-model",
                            "label": "example-code-model",
                            "provider_model_id": "example-code-model",
                        },
                    ],
                    "codex": [
                        {
                            "id": "gpt-5.4",
                            "label": "gpt-5.4",
                            "provider_model_id": "gpt-5.4",
                            "default_reasoning_effort": "high",
                            "supported_reasoning_efforts": [
                                {
                                    "id": "low",
                                    "label": "Low",
                                    "provider_model_id": "gpt-5.4",
                                },
                                {
                                    "id": "high",
                                    "label": "High",
                                    "provider_model_id": "gpt-5.4",
                                },
                                {
                                    "id": "xhigh",
                                    "label": "Extra High",
                                    "provider_model_id": "gpt-5.4",
                                },
                            ],
                        },
                        {
                            "id": "gpt-example-model-c886ad270fea",
                            "label": "gpt-example-model-c886ad270fea",
                            "provider_model_id": "gpt-example-model-c886ad270fea",
                            "default_reasoning_effort": "high",
                            "supported_reasoning_efforts": [
                                {
                                    "id": "high",
                                    "label": "High",
                                    "provider_model_id": "gpt-example-model-c886ad270fea",
                                },
                                {
                                    "id": "xhigh",
                                    "label": "Extra High",
                                    "provider_model_id": "gpt-example-model-c886ad270fea",
                                },
                                {
                                    "id": "max",
                                    "label": "Max",
                                    "provider_model_id": "gpt-example-model-c886ad270fea",
                                },
                            ],
                        },
                    ],
                    "maso": [
                        {
                            "id": "example-model-dedfefd51120",
                            "label": "example-model-dedfefd51120",
                            "provider_model_id": "example-model-dedfefd51120",
                            "default_reasoning_effort": "default",
                            "supported_reasoning_efforts": [
                                {
                                    "id": "default",
                                    "label": "Default",
                                    "provider_model_id": "example-model-dedfefd51120",
                                },
                                {
                                    "id": "high",
                                    "label": "High",
                                    "provider_model_id": "example-model-dedfefd51120_reasoning_high",
                                },
                                {
                                    "id": "xhigh",
                                    "label": "Extra High",
                                    "provider_model_id": "example-model-dedfefd51120_reasoning_xhigh",
                                },
                                {
                                    "id": "max",
                                    "label": "Max",
                                    "provider_model_id": "example-model-dedfefd51120_reasoning_max",
                                },
                            ],
                        },
                        {
                            "id": "gemini-example-model-1dd583607df6",
                            "label": "gemini-example-model-1dd583607df6",
                            "provider_model_id": "gemini-example-model-1dd583607df6",
                            "default_reasoning_effort": "low",
                            "supported_reasoning_efforts": [
                                {
                                    "id": "low",
                                    "label": "Low",
                                    "provider_model_id": "gemini-example-model-1dd583607df6",
                                },
                                {
                                    "id": "medium",
                                    "label": "Medium",
                                    "provider_model_id": "gemini-example-model-1dd583607df6_reasoning_medium",
                                },
                                {
                                    "id": "high",
                                    "label": "High",
                                    "provider_model_id": "gemini-example-model-1dd583607df6_reasoning_high",
                                },
                            ],
                        },
                    ],
                    "cursor": [
                        {
                            "id": "gpt-5.3-codex",
                            "label": "gpt-5.3-codex",
                            "provider_model_id": "gpt-5.3-codex-high",
                            "default_reasoning_effort": "high",
                            "supported_reasoning_efforts": [
                                {
                                    "id": "low",
                                    "label": "Low",
                                    "provider_model_id": "gpt-5.3-codex-low",
                                },
                                {
                                    "id": "high",
                                    "label": "High",
                                    "provider_model_id": "gpt-5.3-codex-high",
                                },
                            ],
                        },
                        {
                            "id": "claude-4.5-sonnet",
                            "label": "claude-4.5-sonnet",
                            "provider_model_id": "claude-4.5-sonnet",
                            "supported_reasoning_efforts": [],
                        },
                    ],
                    "antigravity": [
                        {
                            "id": "gemini-example-model-1d11bad5f3fe-high",
                            "label": "gemini-example-model-1d11bad5f3fe-high",
                            "provider_model_id": "gemini-example-model-1d11bad5f3fe-high",
                        }
                    ],
                },
            },
        }
        if imported is not None:
            payload["imported"] = imported
        return payload

    bots = [
        bot_summary(
            "bot-env",
            "Environment Bot",
            "app-env",
            source="environment",
            credentials_editable=False,
            deletable=False,
        ),
        bot_summary(
            "bot-occupied",
            "Occupied Bot",
            "app-occupied",
            source="stored",
            credentials_editable=True,
            deletable=True,
            binding={
                "pairing_id": "pair-occupied",
                "state": "active",
                "tab_id": "tab-other",
                "workspace_id": None,
                "created_at": NOW,
                "is_mine": False,
                "owner_kind": "oauth",
                "chat_id": None,
                "bot_id": "bot-occupied",
                "app_id": "app-occupied",
                "expires_at": None,
            },
        ),
    ]

    def bot_by_id(bot_id: str) -> dict:
        return next(bot for bot in bots if bot["bot_id"] == bot_id)

    def pool(focus_bot_id: str | None = None) -> dict:
        return {
            "pool_revision": pool_revision,
            "bots": json.loads(json.dumps(bots)),
            "pool_editable": True,
            "deprecated_env": [],
            "focus_bot_id": focus_bot_id,
        }

    async with browser_run.context(viewport={"width": 1280, "height": 900}) as context:
        await context.add_init_script("""
                window.__feishuPollTimers = new Map()
                let nextTimerId = 200000
                const originalSetTimeout = window.setTimeout.bind(window)
                const originalClearTimeout = window.clearTimeout.bind(window)
                window.setTimeout = function(callback, delay, ...args) {
                  if (delay === 3000) {
                    nextTimerId += 1
                    window.__feishuPollTimers.set(nextTimerId, () => callback(...args))
                    return nextTimerId
                  }
                  return originalSetTimeout(callback, delay, ...args)
                }
                window.clearTimeout = function(timerId) {
                  if (window.__feishuPollTimers.delete(timerId)) return
                  originalClearTimeout(timerId)
                }
                window.__runFeishuPollTimers = function() {
                  const callbacks = [...window.__feishuPollTimers.values()]
                  window.__feishuPollTimers.clear()
                  for (const callback of callbacks) queueMicrotask(callback)
                  return callbacks.length
                }
                """)

        async def block_websocket(socket) -> None:
            await socket.close(code=1000, reason="mocked Feishu Bot component smoke")

        async def fulfill_json(route, value: dict | list, status: int = 200) -> None:
            encoded = json.dumps(value)
            response_bodies.append(encoded)
            await route.fulfill(
                status=status,
                content_type="application/json",
                body=encoded,
            )

        async def route_all(route) -> None:
            nonlocal pool_revision, shared_avatar_requests, ai_get_calls
            request = route.request
            parsed = urlparse(request.url)
            if request.url == SHARED_AVATAR_URL:
                shared_avatar_requests += 1
                if shared_avatar_requests == 1:
                    blocked_requests.append((request.method, request.url))
                    await route.abort("failed")
                else:
                    await route.fulfill(
                        status=200,
                        content_type="image/png",
                        headers={"Cache-Control": "no-store"},
                        body=SHARED_AVATAR_PNG,
                    )
                return
            if request.url == "https://avatar.example.test/primary-bear.png":
                blocked_requests.append((request.method, request.url))
                await route.abort("failed")
                return
            if (parsed.scheme, parsed.hostname, parsed.port) != (
                parsed_base.scheme,
                parsed_base.hostname,
                parsed_base.port,
            ):
                blocked_requests.append((request.method, request.url))
                await route.abort("blockedbyclient")
                return
            method, path = request.method, parsed.path
            if not path.startswith("/api/"):
                if method in {"GET", "HEAD"}:
                    await route.continue_()
                else:
                    unknown_requests.append((method, path))
                    await route.fulfill(status=405, body="blocked static method")
                return

            if parsed.query:
                unknown_requests.append((method, request.url))
                await route.fulfill(status=501, body="unexpected API query")
                return
            raw_body = request.post_data
            submitted = json.loads(raw_body) if raw_body else None
            api_calls.append({"method": method, "path": path, "body": submitted})

            if method == "GET" and path == "/api/feishu/bot/bots":
                await fulfill_json(route, pool())
                return

            if method == "GET" and path == "/api/feishu/bot/bots/bot-created/chats":
                await fulfill_json(
                    route,
                    {
                        "revision": bot_by_id("bot-created")["revision"],
                        "chats": [
                            {
                                "chat_id": "group-test",
                                "name": "Test group",
                                "chat_mode": "topic",
                                "is_topic_mode": True,
                                "user_count": 1,
                                "bot_count": 1,
                            }
                        ],
                    },
                )
                return
            if method == "POST" and path == "/api/feishu/bot/bots/bot-created/bindings/group":
                await fulfill_json(route, {"detail": "chat_not_visible"}, status=422)
                return

            if method == "GET" and path == "/api/feishu/bot/ai-command":
                ai_get_calls += 1
                if ai_get_calls == 1:
                    await ai_get_gate.wait()
                    await fulfill_json(route, ai_command_payload())
                    return
                if ai_get_calls == 2:
                    await fulfill_json(
                        route,
                        {
                            "detail": {
                                "code": "invalid_ai_command_config",
                                "message": "Temporary /ai status error",
                            }
                        },
                        status=500,
                    )
                    return
                await fulfill_json(route, ai_command_payload())
                return

            if method == "POST" and path == "/api/feishu/bot/ai-command/import-token":
                ai_config["owner_open_id"] = ai_config["owner_open_id"] or "ou_imported"
                ai_config["app_id"] = ai_config["app_id"] or "cli_imported"
                ai_token_status.update(
                    {
                        "has_token": True,
                        "open_id": "ou_imported",
                        "user_name": "Imported Owner",
                        "app_id": "cli_imported",
                        "source": "lark_cli",
                        "expires_at": 9999999999,
                        "token_expired": False,
                        "granted_scopes": [
                            "im:message",
                            "im:message.reactions:write_only",
                        ],
                        "can_send": True,
                        "can_send_as_user": False,
                        "has_send_as_user_scope": False,
                        "can_send_via_bot": True,
                        "fallback_bot_app_id": "cli_bot_fallback_1",
                        "send_mode": "bot_fallback",
                        "can_react_as_user": True,
                        "can_read_messages": True,
                        "missing_required_scopes": ["im:message.send_as_user"],
                        "remediation_steps": [
                            "Ready via bot fallback (`cli_bot_fallback_1`): user token reads context and reacts as ou_imported."
                        ],
                    }
                )
                await fulfill_json(route, ai_command_payload(imported=True))
                return

            if method in {"PATCH", "PUT"} and path == "/api/feishu/bot/ai-command":
                assert isinstance(submitted, dict)
                merged = {**ai_config, **submitted}
                prov = str(merged.get("provider") or "codex").strip().lower()
                raw_model = str(merged.get("model") or "").strip()
                raw_effort = str(merged.get("reasoning_effort") or "").strip().lower()
                if prov in {"claude", "antigravity"}:
                    assert (
                        raw_effort == ""
                    ), f"{prov} must not submit reasoning_effort={raw_effort!r}"
                    merged["model"] = raw_model
                    merged["reasoning_effort"] = ""
                elif prov in {"maso", "cursor"}:
                    if not raw_model:
                        merged["model"] = ""
                        merged["reasoning_effort"] = ""
                    else:
                        opts = ai_command_payload()["capabilities"]["provider_model_options"].get(
                            prov, []
                        )
                        opt = next(
                            (
                                item
                                for item in opts
                                if item["id"] == raw_model
                                or item.get("provider_model_id") == raw_model
                                or any(
                                    e.get("provider_model_id") == raw_model
                                    for e in item.get("supported_reasoning_efforts", [])
                                )
                            ),
                            None,
                        )
                        if raw_effort and opt and opt.get("supported_reasoning_efforts"):
                            matched_eff = next(
                                (
                                    e
                                    for e in opt["supported_reasoning_efforts"]
                                    if e["id"] == raw_effort
                                ),
                                None,
                            )
                            assert (
                                matched_eff is not None
                            ), f"{prov} model {opt['id']} does not support {raw_effort}"
                            merged["model"] = matched_eff["provider_model_id"]
                            merged["reasoning_effort"] = raw_effort
                        else:
                            merged["model"] = raw_model
                            merged["reasoning_effort"] = raw_effort
                else:
                    merged["model"] = raw_model
                    merged["reasoning_effort"] = raw_effort
                ai_config.clear()
                ai_config.update(merged)
                await fulfill_json(route, ai_command_payload())
                return

            if method == "POST" and path == "/api/feishu/bot/bots":
                assert isinstance(submitted, dict)
                assert set(submitted) == {
                    "name",
                    "app_id",
                    "app_secret",
                }
                created = bot_summary(
                    "bot-created",
                    submitted["name"],
                    submitted["app_id"],
                    source="stored",
                    credentials_editable=True,
                    deletable=True,
                    feishu_name="Primary Bear",
                    feishu_avatar_url="https://avatar.example.test/primary-bear.png",
                )
                bots.append(created)
                pool_revision += 1
                await fulfill_json(route, pool("bot-created"), status=201)
                return

            secrets_match = re.fullmatch(r"/api/feishu/bot/bots/(bot-created)/secrets", path)
            if method == "PUT" and secrets_match:
                assert isinstance(submitted, dict)
                bot = bot_by_id(secrets_match.group(1))
                assert submitted["expected_revision"] == bot["revision"]
                assert set(submitted) == {
                    "app_secret",
                    "expected_revision",
                }
                bot["revision"] += 1
                bot["updated_at"] = NOW
                pool_revision += 1
                await fulfill_json(route, pool(bot["bot_id"]))
                return
            start_match = re.fullmatch(r"/api/feishu/bot/bots/(bot-created)/pair/start", path)
            if method == "POST" and start_match:
                assert isinstance(submitted, dict)
                bot = bot_by_id(start_match.group(1))
                assert submitted == {
                    "tab_id": "tab-1",
                    "expected_revision": bot["revision"],
                }
                bot["revision"] += 1
                pool_revision += 1
                await fulfill_json(
                    route,
                    {
                        "bot_id": bot["bot_id"],
                        "revision": bot["revision"],
                        "code": "CH-ABCDEF1234",
                        "expires_at": CODE_EXPIRES,
                    },
                    status=201,
                )
                return

            activate_match = re.fullmatch(r"/api/feishu/bot/bots/(bot-created)/pair/activate", path)
            if method == "POST" and activate_match:
                assert isinstance(submitted, dict)
                bot = bot_by_id(activate_match.group(1))
                assert submitted == {
                    "pairing_id": "pair-created",
                    "confirm_word": CONFIRM_WORD,
                    "expected_revision": bot["revision"],
                }
                bot["revision"] += 1
                active_binding = {
                    "pairing_id": "pair-created",
                    "state": "active",
                    "tab_id": "tab-1",
                    "workspace_id": None,
                    "created_at": NOW,
                    "is_mine": True,
                    "owner_kind": "local",
                    "bot_id": "bot-created",
                    "app_id": "app-created",
                    "expires_at": None,
                    "chat_id": "oc-created",
                }
                bot["binding"] = active_binding
                bot["bindings"] = [active_binding]
                bot["my_claims"] = []
                pool_revision += 1
                await fulfill_json(route, pool(bot["bot_id"]))
                return

            pairing_match = re.fullmatch(r"/api/feishu/bot/bots/(bot-created)/pairing", path)
            if method == "DELETE" and pairing_match:
                assert isinstance(submitted, dict)
                bot = bot_by_id(pairing_match.group(1))
                assert submitted == {"expected_revision": bot["revision"]}
                bot["revision"] += 1
                bot["binding"] = None
                bot["bindings"] = []
                pool_revision += 1
                await fulfill_json(route, pool(bot["bot_id"]))
                return

            unknown_requests.append((method, path))
            await route.fulfill(
                status=501,
                content_type="application/json",
                body=json.dumps({"detail": f"unmocked API: {method} {path}"}),
            )

        await context.route_web_socket("**/*", block_websocket)
        await context.route("**/*", route_all)
        page = await context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        await page.goto(url)

        trigger = page.get_by_test_id("feishu-binding-trigger")
        await expect(trigger).to_be_visible()
        await expect(page.get_by_test_id("feishu-binding-status")).to_have_count(0)
        await expect(trigger).to_have_attribute("title", re.compile(r"^Feishu · "))
        await expect(trigger).to_have_attribute(
            "aria-label",
            re.compile(r"^Feishu connection settings · "),
        )
        trigger_box = await trigger.bounding_box()
        name_box = await page.locator(".pane-session-name").bounding_box()
        assert trigger_box is not None
        assert name_box is not None
        assert trigger_box["y"] == name_box["y"] == 6
        assert trigger_box["height"] == name_box["height"] == 24
        assert name_box["x"] + name_box["width"] < trigger_box["x"]
        assert trigger_box["x"] - name_box["x"] - name_box["width"] == 5
        assert 7 <= 1280 - trigger_box["x"] - trigger_box["width"] <= 9
        trigger_mark = trigger.locator("svg.trigger-icon.feishu-brand-mark")
        await expect(trigger_mark).to_be_visible()
        await expect(trigger_mark).to_have_attribute("data-feishu-brand-mark", "bird")
        await page.screenshot(path=str(output / "feishu-status-entry-desktop.png"))

        # Pool create: response is summary-only; submitted credentials clear.
        await page.get_by_role("button", name="Open Bot settings", exact=True).click()
        settings = page.get_by_test_id("feishu-bot-settings-dialog")
        await expect(settings).to_be_visible()
        await expect(settings.get_by_text("Loading Bot pool…", exact=True)).to_have_count(0)
        await expect(settings.get_by_role("heading", name="Add Bot", exact=True)).to_be_visible()
        await settings.get_by_label("Name", exact=True).fill("Primary Bot")
        await settings.get_by_label("App ID", exact=True).fill("app-created")
        await settings.get_by_label("App Secret", exact=True).fill("create-secret")
        await settings.get_by_role("button", name="Validate and add", exact=True).click()
        await expect(
            settings.get_by_role("heading", name="Primary Bot", exact=True)
        ).to_be_visible()
        settings_box = await settings.bounding_box()
        sidebar_box = await settings.locator(".bot-sidebar").bounding_box()
        detail_box = await settings.locator(".detail").bounding_box()
        assert settings_box is not None and settings_box["width"] >= 900
        assert sidebar_box is not None and detail_box is not None
        assert sidebar_box["x"] < detail_box["x"]
        assert 230 <= sidebar_box["width"] <= 270
        assert detail_box["width"] > sidebar_box["width"]
        await expect(
            settings.locator('[aria-label="Bots"] button').filter(has_text="Primary Bot")
        ).to_have_attribute("aria-pressed", "true")
        await expect(settings.locator(".credential-state")).to_contain_text("Secret set")
        await expect(settings.get_by_role("status").filter(has_text="Bot added.")).to_be_visible()
        await expect(settings.get_by_label("App Secret", exact=True)).to_have_value("")
        await expect(settings.locator(".connection-card")).to_contain_text("Connected")
        for retired_label in ("Verification Token", "Encrypt Key", "Callback URL"):
            await expect(settings.get_by_text(retired_label, exact=True)).to_have_count(0)

        for row in await settings.locator('[aria-label="Bots"] > button').all():
            box = await row.bounding_box()
            assert box is not None and box["height"] <= 80
        await page.screenshot(path=str(output / "feishu-bot-pool-desktop.png"), full_page=True)
        create_call = next(
            call
            for call in api_calls
            if call["method"] == "POST" and call["path"] == "/api/feishu/bot/bots"
        )
        assert create_call["body"] == {
            "name": "Primary Bot",
            "app_id": "app-created",
            "app_secret": "create-secret",
        }

        # Replacing credentials uses the selected revision and clears the
        # secret draft on success.
        await settings.get_by_label("App Secret", exact=True).fill("replace-secret")
        await settings.get_by_role("button", name="Validate and replace", exact=True).click()
        await expect(
            settings.get_by_role("status").filter(has_text="Credentials replaced")
        ).to_be_visible()
        await expect(settings.get_by_label("App Secret", exact=True)).to_have_value("")
        secrets_call = next(
            call for call in api_calls if call["path"].endswith("/bot-created/secrets")
        )
        assert secrets_call["body"] == {
            "app_secret": "replace-secret",
            "expected_revision": 1,
        }

        # Closing with dirty secret drafts unmounts the real dialog. A fresh
        # mount and selection must not recover those secrets.
        await settings.get_by_label("App Secret", exact=True).fill("close-secret")
        await settings.locator("footer").get_by_role("button", name="Close", exact=True).click()
        await expect(settings).to_have_count(0)
        await page.get_by_role("button", name="Open Bot settings", exact=True).click()
        settings = page.get_by_test_id("feishu-bot-settings-dialog")
        await expect(settings).to_be_visible()
        await expect(settings.get_by_text("Loading Bot pool…", exact=True)).to_have_count(0)
        await (
            settings.locator('[aria-label="Bots"]')
            .locator("button")
            .filter(has_text="Primary Bot")
            .click()
        )
        await expect(settings.get_by_label("App Secret", exact=True)).to_have_value("")

        # Environment credentials and deletion are read-only. Occupancy is
        # visible in settings and the occupied Bot cannot be selected later.
        await (
            settings.locator('[aria-label="Bots"]')
            .locator("button")
            .filter(has_text="Environment Bot")
            .click()
        )
        await expect(settings.locator(".detail")).to_contain_text("environment")
        await expect(
            settings.get_by_role("heading", name="Replace credentials", exact=True)
        ).to_have_count(0)
        await expect(settings.get_by_role("button", name="Delete Bot", exact=True)).to_have_count(0)
        await expect(
            settings.locator('[aria-label="Bots"]')
            .locator("button")
            .filter(has_text="Occupied Bot")
        ).to_contain_text("In use by Chat tab-other")
        await settings.locator("footer").get_by_role("button", name="Close", exact=True).click()
        await expect(settings).to_have_count(0)
        # Binding: occupied Bot is disabled; available Bot produces a code.
        await trigger.click()
        binding_panel = page.get_by_role("dialog", name="Feishu connection")
        await expect(binding_panel).to_be_visible()
        panel_mark = binding_panel.locator(".panel-mark.feishu-brand-mark")
        await expect(panel_mark).to_be_visible()
        await expect(panel_mark).to_have_attribute("data-feishu-brand-mark", "bird")
        assert await trigger_mark.locator("path").count() == 3
        assert await panel_mark.locator("path").count() == 3
        assert await trigger_mark.locator("path").evaluate_all(
            "paths => paths.map(path => path.getAttribute('d'))"
        ) == await panel_mark.locator("path").evaluate_all(
            "paths => paths.map(path => path.getAttribute('d'))"
        )
        bot_select = binding_panel.get_by_label("Bot", exact=True)
        await expect(bot_select.locator('option[value="bot-occupied"]')).to_have_js_property(
            "disabled", True
        )
        await bot_select.select_option("bot-created")
        await binding_panel.get_by_role("button", name="Generate pairing code", exact=True).click()
        await expect(page.get_by_test_id("feishu-binding-code")).to_have_text("CH-ABCDEF1234")
        start_call = next(call for call in api_calls if call["path"].endswith("/pair/start"))
        assert start_call["body"] == {"tab_id": "tab-1", "expected_revision": 2}

        # Simulate the callback claim only in mock server state, then run the
        # exact poll callback scheduled by the real composable.
        created = bot_by_id("bot-created")
        created["revision"] += 1
        created["my_claims"] = [
            {
                "pairing_id": "pair-created",
                "state": "claimed",
                "bot_id": "bot-created",
                "app_id": "app-created",
                "tab_id": "tab-1",
                "workspace_id": None,
                "chat_id": "oc-created",
                "created_at": NOW,
                "expires_at": CLAIM_EXPIRES,
            }
        ]
        pool_revision += 1
        timers_run = await page.evaluate("window.__runFeishuPollTimers()")
        assert timers_run >= 1
        await expect(page.get_by_test_id("feishu-binding-status")).to_have_text(
            "Confirmation required"
        )
        confirm_input = binding_panel.get_by_label("Confirmation word", exact=True)
        await expect(confirm_input).to_have_value("")
        assert CONFIRM_WORD not in await binding_panel.inner_text()

        await page.screenshot(path=str(output / "feishu-pairing-desktop.png"), full_page=True)
        # The confirmation word is manually entered. The request uses the
        # revision from the claimed pool snapshot; no response may echo it.
        await confirm_input.fill(CONFIRM_WORD)
        await binding_panel.get_by_role("button", name="Activate pairing", exact=True).click()
        await expect(page.get_by_test_id("feishu-binding-status")).to_have_text(
            "Connected with Primary Bear"
        )
        identity = page.get_by_test_id("feishu-binding-identity")
        await expect(identity).to_be_visible()
        await expect(identity.locator("img")).to_have_count(0)
        await expect(identity.locator("svg.feishu-brand-mark")).to_be_visible()
        await expect(binding_panel.get_by_text("Primary Bear", exact=True)).to_be_visible()
        activate_call = next(call for call in api_calls if call["path"].endswith("/pair/activate"))
        assert activate_call["body"] == {
            "pairing_id": "pair-created",
            "confirm_word": CONFIRM_WORD,
            "expected_revision": 4,
        }

        await binding_panel.get_by_role("button", name="Disconnect Feishu", exact=True).click()
        await binding_panel.get_by_role("button", name="Confirm disconnect", exact=True).click()
        await expect(page.get_by_test_id("feishu-binding-status")).to_have_text("Not connected")
        await expect(page.get_by_test_id("feishu-binding-identity")).to_have_count(0)
        disconnect_call = next(
            call
            for call in api_calls
            if call["method"] == "DELETE" and call["path"].endswith("/pairing")
        )
        assert disconnect_call["body"] == {"expected_revision": 5}
        await binding_panel.get_by_role("button", name="Close", exact=True).click()
        await expect(binding_panel).to_have_count(0)

        # A rejected group bind must not hide the group reload control.
        bot_by_id("bot-created")["mode"] = "per_channel"
        bot_by_id("bot-created")["revision"] += 1
        pool_revision += 1
        await page.evaluate("window.__FEISHU_BOT_TEST__.setTabId('tab-group')")
        await trigger.click()
        await binding_panel.get_by_label("Bot", exact=True).select_option("bot-created")
        await binding_panel.get_by_test_id("feishu-binding-group-select").select_option(
            "group-test"
        )
        await binding_panel.get_by_test_id("feishu-binding-bind-group").click()
        await expect(binding_panel.get_by_text(re.compile("cannot see that group"))).to_be_visible()
        await binding_panel.get_by_test_id("feishu-binding-reload-groups").click()
        await expect(binding_panel.get_by_text(re.compile("cannot see that group"))).to_have_count(
            0
        )
        await binding_panel.get_by_role("button", name="Close", exact=True).click()
        bot_by_id("bot-created")["mode"] = "single"
        bot_by_id("bot-created")["revision"] += 1
        pool_revision += 1

        # Avatar failures belong to one Chat/Bot identity, not to the
        # reused panel instance or URL. Bot A's failed request must not
        # suppress Bot B's retry when both official profiles share a URL.
        for bot_id, tab_id, name in (
            ("bot-avatar-a", "tab-avatar-a", "Shared Bear A"),
            ("bot-avatar-b", "tab-avatar-b", "Shared Bear B"),
        ):
            bots.append(
                bot_summary(
                    bot_id,
                    name,
                    f"app-{bot_id}",
                    source="stored",
                    credentials_editable=True,
                    deletable=True,
                    feishu_name=name,
                    feishu_avatar_url=SHARED_AVATAR_URL,
                    binding={
                        "pairing_id": f"pair-{bot_id}",
                        "state": "active",
                        "tab_id": tab_id,
                        "workspace_id": None,
                        "created_at": NOW,
                        "is_mine": True,
                        "owner_kind": "local",
                        "chat_id": f"oc-{bot_id}",
                        "bot_id": bot_id,
                        "app_id": f"app-{bot_id}",
                        "expires_at": None,
                    },
                )
            )
        pool_revision += 1
        await page.evaluate("window.__FEISHU_BOT_TEST__.setTabId('tab-avatar-a')")
        await expect(trigger).to_have_attribute(
            "aria-label", re.compile(r"Connected with Shared Bear A$")
        )
        await trigger.click()
        binding_panel = page.get_by_role("dialog", name="Feishu connection")
        await expect(binding_panel).to_be_visible()
        for _ in range(100):
            if shared_avatar_requests >= 1:
                break
            await asyncio.sleep(0.01)
        assert shared_avatar_requests == 1
        identity = page.get_by_test_id("feishu-binding-identity")
        await expect(identity.locator("img")).to_have_count(0)
        await expect(identity.locator("svg.feishu-brand-mark")).to_be_visible()

        await page.evaluate("window.__FEISHU_BOT_TEST__.setTabId('tab-avatar-b')")
        await expect(trigger).to_have_attribute(
            "aria-label", re.compile(r"Connected with Shared Bear B$")
        )
        # Changing Chats closes the previous Chat's panel. Reopen the new
        # Chat's panel so its independently keyed Bot identity is mounted.
        await expect(binding_panel).to_have_count(0)
        await trigger.click()
        binding_panel = page.get_by_role("dialog", name="Feishu connection")
        await expect(binding_panel).to_be_visible()
        identity = page.get_by_test_id("feishu-binding-identity")
        for _ in range(100):
            if shared_avatar_requests >= 2:
                break
            await asyncio.sleep(0.01)
        assert shared_avatar_requests == 2
        await expect(identity.locator("img")).to_have_attribute("src", SHARED_AVATAR_URL)
        await expect(identity.locator("img")).to_have_attribute("crossorigin", "anonymous")
        await page.wait_for_function(
            "() => { const image = document.querySelector('[data-testid=feishu-binding-identity] img'); return image?.complete && image.naturalWidth > 0 }"
        )
        await page.screenshot(
            path=str(output / "feishu-bound-bot-avatar-desktop.png"), full_page=True
        )

        await page.evaluate("window.__FEISHU_BOT_TEST__.setTabId('tab-1')")
        await expect(trigger).to_have_attribute("aria-label", re.compile(r"Not connected$"))
        await expect(page.get_by_test_id("feishu-binding-identity")).to_have_count(0)

        # Exercise Personal /ai Assistant panel: delayed/error/retry hydration,
        # draft preservation across ~/.lark-cli token import, and save/readback.
        await page.get_by_role("button", name="Open Bot settings", exact=True).click()
        settings = page.get_by_test_id("feishu-bot-settings-dialog")
        await expect(settings).to_be_visible()
        await expect(settings.get_by_text("Loading Bot pool…", exact=True)).to_have_count(0)
        await settings.get_by_test_id("feishu-ai-command-nav").click()
        await expect(settings.get_by_test_id("feishu-ai-loading")).to_be_visible()
        await expect(settings.get_by_test_id("feishu-ai-settings-form")).to_have_count(0)
        await expect(
            settings.locator("footer").get_by_role("button", name="Reload", exact=True)
        ).to_be_disabled()

        # Navigating away to a Bot during initial /ai GET immediately restores Bot Reload
        await (
            settings.locator('[aria-label="Bots"]')
            .locator("button")
            .filter(has_text="Occupied Bot")
            .click()
        )
        await expect(
            settings.locator("footer").get_by_role("button", name="Reload", exact=True)
        ).to_be_enabled()
        ai_get_gate.set()
        await expect(
            settings.locator("footer").get_by_role("button", name="Reload", exact=True)
        ).to_be_enabled()

        # Re-open Personal /ai: GET #2 fails with 500 -> error state -> Retry (GET #3) hydrates form
        await settings.get_by_test_id("feishu-ai-command-nav").click()
        await expect(settings.get_by_test_id("feishu-ai-load-error")).to_be_visible()
        await expect(settings.get_by_test_id("feishu-ai-settings-form")).to_have_count(0)
        assert not any(
            call["method"] in {"PATCH", "PUT"} and call["path"] == "/api/feishu/bot/ai-command"
            for call in api_calls
        )

        await settings.get_by_test_id("feishu-ai-retry-btn").click()
        ai_form = settings.get_by_test_id("feishu-ai-settings-form")
        await expect(ai_form).to_be_visible()
        token_card = settings.get_by_test_id("feishu-ai-token-card")
        await expect(token_card).to_have_attribute("data-state", "failed")
        await expect(settings.locator(".detail-title-row .source-chip")).to_have_text(
            "Missing send_as_user"
        )
        await expect(token_card.locator("strong")).to_have_text(
            "Pending Owner (ou_unconfigured) · Missing required scope: im:message.send_as_user (and no bot fallback configured)"
        )
        await expect(settings.get_by_test_id("feishu-ai-token-guidance")).to_have_text(
            "1. Configure app secret for `cli_unconfigured` (or enable `im:message.send_as_user` in Feishu Developer Console for `cli_unconfigured`)."
        )
        await expect(settings.get_by_test_id("feishu-ai-token-login-cmd")).to_be_visible()
        model_select = settings.get_by_test_id("feishu-ai-model-input")
        effort_select = settings.get_by_test_id("feishu-ai-effort-select")
        # Verify Model control is a real HTML <select> (not a plain text <input>) with logical labels
        assert await model_select.evaluate("el => el.tagName") == "SELECT"
        assert await model_select.locator("option").all_text_contents() == [
            "Provider default",
            "example-model-dedfefd51120",
            "gemini-example-model-1dd583607df6",
        ]
        await expect(settings.get_by_test_id("feishu-ai-provider-select")).to_have_value("maso")
        await expect(model_select).to_have_value("gemini-example-model-1dd583607df6_reasoning_high")
        await expect(effort_select).to_have_value("high")
        # Switching MASO effort updates the resolved variant value on the logical Model <select>
        await effort_select.select_option("medium")
        await expect(model_select).to_have_value(
            "gemini-example-model-1dd583607df6_reasoning_medium"
        )
        # Selecting live-shaped MASO example-model-dedfefd51120 adopts mode='default' ('example-model-dedfefd51120'), saves ('maso', 'example-model-dedfefd51120', 'default'),
        # and reloads cleanly; then switching to xhigh and choosing Provider default ('') clears model-bound effort and reloads ('maso', '', '')
        await model_select.select_option(label="example-model-dedfefd51120")
        await expect(model_select).to_have_value("example-model-dedfefd51120")
        await expect(effort_select).to_have_value("default")
        await settings.get_by_test_id("feishu-ai-save-btn").click()
        await expect(
            settings.get_by_text("/ai command settings saved.", exact=True)
        ).to_be_visible()
        await settings.locator("footer").get_by_role("button", name="Close", exact=True).click()
        await expect(settings).to_have_count(0)
        await page.get_by_role("button", name="Open Bot settings", exact=True).click()
        settings = page.get_by_test_id("feishu-bot-settings-dialog")
        await expect(settings).to_be_visible()
        await settings.get_by_test_id("feishu-ai-command-nav").click()
        model_select = settings.get_by_test_id("feishu-ai-model-input")
        effort_select = settings.get_by_test_id("feishu-ai-effort-select")
        await expect(settings.get_by_test_id("feishu-ai-provider-select")).to_have_value("maso")
        await expect(model_select).to_have_value("example-model-dedfefd51120")
        await expect(effort_select).to_have_value("default")

        await effort_select.select_option("xhigh")
        await expect(model_select).to_have_value("example-model-dedfefd51120_reasoning_xhigh")
        await model_select.select_option("")
        await expect(model_select).to_have_value("")
        await expect(effort_select).to_be_disabled()
        await expect(effort_select).to_have_value("")
        await settings.get_by_test_id("feishu-ai-save-btn").click()
        await expect(
            settings.get_by_text("/ai command settings saved.", exact=True)
        ).to_be_visible()
        await settings.locator("footer").get_by_role("button", name="Close", exact=True).click()
        await expect(settings).to_have_count(0)
        await page.get_by_role("button", name="Open Bot settings", exact=True).click()
        settings = page.get_by_test_id("feishu-bot-settings-dialog")
        await expect(settings).to_be_visible()
        await settings.get_by_test_id("feishu-ai-command-nav").click()
        model_select = settings.get_by_test_id("feishu-ai-model-input")
        effort_select = settings.get_by_test_id("feishu-ai-effort-select")
        token_card = settings.get_by_test_id("feishu-ai-token-card")
        await expect(settings.get_by_test_id("feishu-ai-provider-select")).to_have_value("maso")
        await expect(model_select).to_have_value("")
        await expect(effort_select).to_be_disabled()
        await expect(effort_select).to_have_value("")
        # Switching Provider to claude disables effort selection and lists Claude models
        await settings.get_by_test_id("feishu-ai-provider-select").select_option("claude")
        await expect(model_select).to_have_value("claude-opus-4-8")
        await expect(effort_select).to_be_disabled()
        await expect(effort_select).to_have_value("")
        # Switching Provider to cursor and selecting a non-effort model disables effort select
        await settings.get_by_test_id("feishu-ai-provider-select").select_option("cursor")
        await expect(model_select).to_have_value("gpt-5.3-codex-high")
        await expect(effort_select).to_be_enabled()
        await model_select.select_option("claude-4.5-sonnet")
        await expect(effort_select).to_be_disabled()
        await expect(effort_select).to_have_value("")
        await expect(settings.get_by_test_id("feishu-ai-cwd-input")).to_have_value(
            "/srv/persisted-ai"
        )
        await expect(settings.get_by_test_id("feishu-ai-watch-chats-input")).to_have_value(
            "oc_persisted"
        )

        # Edit draft fields, then import ~/.lark-cli token before saving: draft edits must survive
        await settings.get_by_test_id("feishu-ai-enabled-toggle").check()
        await settings.get_by_test_id("feishu-ai-provider-select").select_option("codex")
        await expect(model_select).to_have_value("gpt-5.4")
        await model_select.select_option("gpt-example-model-c886ad270fea")
        await expect(model_select).to_have_value("gpt-example-model-c886ad270fea")
        await effort_select.select_option("xhigh")
        await settings.get_by_test_id("feishu-ai-cwd-input").fill("/srv/draft-ai")
        await settings.get_by_test_id("feishu-ai-watch-chats-input").fill("oc_chat_1, oc_chat_2")

        await settings.get_by_test_id("feishu-ai-import-token-btn").click()
        await expect(
            settings.get_by_text("Imported user token from ~/.lark-cli.", exact=True)
        ).to_be_visible()
        await expect(token_card).to_have_attribute("data-state", "connected")
        await expect(settings.locator(".detail-title-row .source-chip")).to_have_text(
            "Bot fallback (cli_bot_fallback_1)"
        )
        await expect(token_card.locator("strong")).to_have_text(
            "Ready (bot fallback: cli_bot_fallback_1): Imported Owner (ou_imported) · im:message.send_as_user not granted"
        )
        await expect(settings.get_by_test_id("feishu-ai-token-guidance")).to_have_count(0)
        await expect(settings.get_by_test_id("feishu-ai-token-login-cmd")).to_have_count(0)
        await expect(settings.get_by_test_id("feishu-ai-provider-select")).to_have_value("codex")
        await expect(model_select).to_have_value("gpt-example-model-c886ad270fea")
        await expect(effort_select).to_have_value("xhigh")
        await expect(settings.get_by_test_id("feishu-ai-cwd-input")).to_have_value("/srv/draft-ai")
        await expect(settings.get_by_test_id("feishu-ai-watch-chats-input")).to_have_value(
            "oc_chat_1, oc_chat_2"
        )
        await expect(settings.get_by_test_id("feishu-ai-owner-input")).to_have_value("ou_imported")
        await expect(settings.get_by_test_id("feishu-ai-app-id-input")).to_have_value(
            "cli_imported"
        )

        done_reaction = settings.get_by_test_id("feishu-ai-done-emoji-input")
        progress_reply = settings.get_by_test_id("feishu-ai-send-interim-input")
        await expect(done_reaction).to_have_value("DONE")
        await expect(progress_reply).not_to_be_checked()
        await done_reaction.fill("")
        await progress_reply.check()
        await settings.get_by_test_id("feishu-ai-save-btn").click()
        await expect(
            settings.get_by_text("/ai command settings saved.", exact=True)
        ).to_be_visible()
        ai_patches = [
            call["body"]
            for call in api_calls
            if call["method"] == "PATCH" and call["path"] == "/api/feishu/bot/ai-command"
        ]
        assert len(ai_patches) == 3
        assert ai_patches[0]["provider"] == "maso"
        assert ai_patches[0]["model"] == "example-model-dedfefd51120"
        assert ai_patches[0]["reasoning_effort"] == "default"
        assert ai_patches[1]["provider"] == "maso"
        assert ai_patches[1]["model"] == ""
        assert ai_patches[1]["reasoning_effort"] == ""
        assert ai_patches[2]["enabled"] is True
        assert ai_patches[2]["provider"] == "codex"
        assert ai_patches[2]["model"] == "gpt-example-model-c886ad270fea"
        assert ai_patches[2]["reasoning_effort"] == "xhigh"
        assert ai_patches[2]["cwd"] == "/srv/draft-ai"
        assert ai_patches[2]["watch_chats"] == ["oc_chat_1", "oc_chat_2"]
        assert ai_patches[2]["owner_open_id"] == "ou_imported"
        assert ai_patches[2]["app_id"] == "cli_imported"
        assert ai_patches[2]["done_emoji"] == ""
        assert ai_patches[2]["send_interim_reply"] is True
        await expect(done_reaction).to_have_value("")
        await expect(progress_reply).to_be_checked()
        await page.screenshot(path=str(output / "feishu-ai-command-desktop.png"), full_page=True)
        await settings.locator("footer").get_by_role("button", name="Close", exact=True).click()
        await expect(settings).to_have_count(0)

        for forbidden in (
            CONFIRM_WORD,
            "create-secret",
            "replace-secret",
            "close-secret",
        ):
            assert all(forbidden not in body for body in response_bodies)

        # Check narrow viewport (720x900) layout for Personal /ai Assistant panel (bot_fallback state)
        await page.set_viewport_size({"width": 720, "height": 900})
        await page.get_by_role("button", name="Open Bot settings", exact=True).click()
        settings = page.get_by_test_id("feishu-bot-settings-dialog")
        await expect(settings).to_be_visible()
        await settings.get_by_test_id("feishu-ai-command-nav").click()
        await expect(settings.get_by_test_id("feishu-ai-settings-form")).to_be_visible()
        await expect(settings.get_by_test_id("feishu-ai-token-card")).to_have_attribute(
            "data-state", "connected"
        )
        await expect(settings.locator(".detail-title-row .source-chip")).to_have_text(
            "Bot fallback (cli_bot_fallback_1)"
        )
        narrow_settings_box = await settings.bounding_box()
        narrow_ai_box = await settings.get_by_test_id("feishu-ai-settings-form").bounding_box()
        assert narrow_settings_box is not None and narrow_ai_box is not None
        assert narrow_settings_box["x"] >= 0
        assert narrow_settings_box["x"] + narrow_settings_box["width"] <= 721
        assert (
            narrow_ai_box["x"] + narrow_ai_box["width"]
            <= narrow_settings_box["x"] + narrow_settings_box["width"] + 1
        )
        narrow_model_box = await settings.get_by_test_id("feishu-ai-model-input").bounding_box()
        assert narrow_model_box is not None
        assert (
            narrow_model_box["x"] + narrow_model_box["width"]
            <= narrow_settings_box["x"] + narrow_settings_box["width"] + 1
        )
        await page.screenshot(path=str(output / "feishu-ai-command-narrow.png"), full_page=True)
        await settings.locator("footer").get_by_role("button", name="Close", exact=True).click()
        await expect(settings).to_have_count(0)

        # Switch mock token_status to direct user scope before mobile check so all 3 states are verified in browser
        ai_token_status.update(
            {
                "granted_scopes": ["im:message", "im:message.send_as_user"],
                "can_send": True,
                "can_send_as_user": True,
                "has_send_as_user_scope": True,
                "can_send_via_bot": True,
                "send_mode": "user",
                "missing_required_scopes": [],
            }
        )

        # Both real components remain operable at mobile width.
        await page.set_viewport_size({"width": 390, "height": 844})
        trigger_box = await trigger.bounding_box()
        name_box = await page.locator(".pane-session-name").bounding_box()
        assert trigger_box is not None
        assert name_box is not None
        assert trigger_box["y"] == name_box["y"] == 6
        assert trigger_box["height"] == name_box["height"] == 24
        assert name_box["x"] + name_box["width"] < trigger_box["x"]
        assert 7 <= 390 - trigger_box["x"] - trigger_box["width"] <= 9
        await page.get_by_role("button", name="Open Bot settings", exact=True).click()
        settings = page.get_by_test_id("feishu-bot-settings-dialog")
        await expect(settings).to_be_visible()
        await expect(settings.get_by_text("Loading Bot pool…", exact=True)).to_have_count(0)
        settings_box = await settings.bounding_box()
        assert settings_box is not None
        assert settings_box["x"] >= 0
        assert settings_box["x"] + settings_box["width"] <= 391
        assert settings_box["y"] >= 0
        assert settings_box["y"] + settings_box["height"] <= 845
        mobile_sidebar_box = await settings.locator(".bot-sidebar").bounding_box()
        mobile_detail_box = await settings.locator(".detail").bounding_box()
        assert mobile_sidebar_box is not None and mobile_detail_box is not None
        await page.screenshot(path=str(output / "feishu-bot-pool-mobile.png"), full_page=True)
        assert mobile_sidebar_box["y"] < mobile_detail_box["y"]
        for region_box in (mobile_sidebar_box, mobile_detail_box):
            assert region_box["x"] >= settings_box["x"]
            assert (
                region_box["x"] + region_box["width"]
                <= settings_box["x"] + settings_box["width"] + 1
            ), {"dialog": settings_box, "region": region_box}
        assert await settings.locator(".layout").evaluate(
            "element => getComputedStyle(element).overflowY === 'auto'"
        )
        await settings.get_by_test_id("feishu-ai-command-nav").click()
        await expect(settings.get_by_test_id("feishu-ai-settings-form")).to_be_visible()
        await expect(settings.get_by_test_id("feishu-ai-token-card")).to_have_attribute(
            "data-state", "connected"
        )
        await expect(settings.locator(".detail-title-row .source-chip")).to_have_text("Scope ready")
        await expect(
            settings.get_by_test_id("feishu-ai-token-card").locator("strong")
        ).to_have_text("Ready: Imported Owner (ou_imported) · im:message.send_as_user granted")
        mobile_ai_box = await settings.get_by_test_id("feishu-ai-settings-form").bounding_box()
        assert mobile_ai_box is not None
        assert mobile_ai_box["x"] >= settings_box["x"]
        assert (
            mobile_ai_box["x"] + mobile_ai_box["width"]
            <= settings_box["x"] + settings_box["width"] + 1
        )
        mobile_model_select = settings.get_by_test_id("feishu-ai-model-input")
        mobile_model_box = await mobile_model_select.bounding_box()
        assert mobile_model_box is not None
        assert mobile_model_box["x"] >= settings_box["x"]
        assert (
            mobile_model_box["x"] + mobile_model_box["width"]
            <= settings_box["x"] + settings_box["width"] + 1
        )
        await expect(mobile_model_select).to_have_value("gpt-example-model-c886ad270fea")
        mobile_save_btn = settings.get_by_test_id("feishu-ai-save-btn")
        await mobile_save_btn.scroll_into_view_if_needed()
        await expect(mobile_save_btn).to_be_visible()
        await mobile_save_btn.click()
        await expect(
            settings.get_by_role("status").filter(has_text="/ai command settings saved.")
        ).to_be_visible()
        await settings.get_by_test_id("feishu-ai-provider-select").scroll_into_view_if_needed()
        await expect(mobile_model_select).to_be_visible()
        await page.screenshot(path=str(output / "feishu-ai-command-mobile.png"), full_page=True)
        await settings.locator("footer").get_by_role("button", name="Close", exact=True).click()
        await expect(settings).to_have_count(0)

        await trigger.click()
        binding_panel = page.get_by_role("dialog", name="Feishu connection")
        await expect(binding_panel).to_be_visible()
        panel_box = await binding_panel.bounding_box()
        assert panel_box is not None
        assert panel_box["x"] >= 0
        assert panel_box["x"] + panel_box["width"] <= 391
        await page.screenshot(path=str(output / "feishu-pairing-mobile.png"), full_page=True)
        await binding_panel.get_by_label("Bot", exact=True).select_option("bot-created")
        await expect(
            binding_panel.get_by_role("button", name="Generate pairing code", exact=True)
        ).to_be_enabled()
        await binding_panel.get_by_role("button", name="Close", exact=True).click()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

        await page.screenshot(
            path=str(output / "feishu-bot-components-mobile.png"),
            full_page=True,
        )
        assert set(blocked_requests) == {
            ("GET", "https://avatar.example.test/primary-bear.png"),
            ("GET", SHARED_AVATAR_URL),
        }, blocked_requests
        assert not unknown_requests, unknown_requests
        assert not page_errors, page_errors

    report = {
        "result": "PASS",
        "api_calls": [{"method": call["method"], "path": call["path"]} for call in api_calls],
        "blocked_requests": blocked_requests,
        "unknown_requests": unknown_requests,
        "page_errors": page_errors,
        "boundary": "Real SettingsDialog/BindingPanel/Pinia; all HTTP and WebSocket traffic mocked.",
    }
    (output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    run_browser_main(main)
