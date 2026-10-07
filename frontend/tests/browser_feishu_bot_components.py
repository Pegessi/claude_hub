"""Real Feishu Bot settings and binding components with fully mocked APIs.

Usage:
  python tests/browser_feishu_bot_components.py \
    http://127.0.0.1:5295/tests/feishu_bot_components_harness.html \
    /tmp/feishu-bot-components [chromium]
"""

import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, expect

CLOCK = datetime.now(timezone.utc)
NOW = CLOCK.isoformat()
CODE_EXPIRES = (CLOCK + timedelta(seconds=600)).isoformat()
CLAIM_EXPIRES = (CLOCK + timedelta(seconds=300)).isoformat()
CONFIRM_WORD = "ABC234"


def bot_summary(
    bot_id: str,
    name: str,
    app_id: str,
    *,
    source: str,
    credentials_editable: bool,
    deletable: bool,
    binding: dict | None = None,
) -> dict:
    return {
        "bot_id": bot_id,
        "name": name,
        "app_id": app_id,
        "source": source,
        "enabled": True,
        "credentials_editable": credentials_editable,
        "deletable": deletable,
        "revision": 1,
        "generation": 1,
        "configured": True,
        "app_secret_configured": True,
        "verification_token_configured": True,
        "encrypt_key_configured": True,
        "event_url": f"https://hub.example.test/api/feishu/bot/events/{bot_id}",
        "updated_at": NOW,
        "binding": binding,
        "my_claims": [],
    }


async def main() -> None:
    if len(sys.argv) not in (3, 4):
        raise SystemExit(__doc__)
    url, output_arg = sys.argv[1:3]
    executable = sys.argv[3] if len(sys.argv) == 4 else None
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

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=executable,
        )
        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                service_workers="block",
            )
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
                nonlocal pool_revision
                request = route.request
                parsed = urlparse(request.url)
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

                if method == "POST" and path == "/api/feishu/bot/bots":
                    assert isinstance(submitted, dict)
                    assert set(submitted) == {
                        "name",
                        "app_id",
                        "app_secret",
                        "verification_token",
                        "encrypt_key",
                    }
                    created = bot_summary(
                        "bot-created",
                        submitted["name"],
                        submitted["app_id"],
                        source="stored",
                        credentials_editable=True,
                        deletable=True,
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
                        "verification_token",
                        "encrypt_key",
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
                            "event_url": bot["event_url"],
                        },
                        status=201,
                    )
                    return

                activate_match = re.fullmatch(
                    r"/api/feishu/bot/bots/(bot-created)/pair/activate", path
                )
                if method == "POST" and activate_match:
                    assert isinstance(submitted, dict)
                    bot = bot_by_id(activate_match.group(1))
                    assert submitted == {
                        "pairing_id": "pair-created",
                        "confirm_word": CONFIRM_WORD,
                        "expected_revision": bot["revision"],
                    }
                    bot["revision"] += 1
                    bot["binding"] = {
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

            # Pool create: response is summary-only; submitted credentials clear.
            await page.get_by_role("button", name="Open Bot settings", exact=True).click()
            settings = page.get_by_test_id("feishu-bot-settings-dialog")
            await expect(settings).to_be_visible()
            await expect(settings.get_by_text("Loading Bot pool…", exact=True)).to_have_count(0)
            await expect(
                settings.get_by_role("heading", name="Add Bot", exact=True)
            ).to_be_visible()
            await settings.get_by_label("Name", exact=True).fill("Primary Bot")
            await settings.get_by_label("App ID", exact=True).fill("app-created")
            await settings.get_by_label("App Secret", exact=True).fill("create-secret")
            await settings.get_by_label("Verification Token", exact=True).fill("create-token")
            await settings.get_by_label("Encrypt Key", exact=True).fill("create-encrypt")
            await settings.get_by_role("button", name="Validate and add", exact=True).click()
            await expect(
                settings.get_by_role("heading", name="Primary Bot", exact=True)
            ).to_be_visible()
            await expect(
                settings.get_by_role("status").filter(has_text="Bot added.")
            ).to_be_visible()
            await expect(settings.get_by_label("App Secret", exact=True)).to_have_value("")
            await expect(settings.get_by_label("Verification Token", exact=True)).to_have_value("")
            await expect(settings.get_by_label("Encrypt Key", exact=True)).to_have_value("")

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
                "verification_token": "create-token",
                "encrypt_key": "create-encrypt",
            }

            # Replacing credentials uses the selected revision and clears all
            # three secret drafts on success.
            await settings.get_by_label("App Secret", exact=True).fill("replace-secret")
            await settings.get_by_label("Verification Token", exact=True).fill("replace-token")
            await settings.get_by_label("Encrypt Key", exact=True).fill("replace-encrypt")
            await settings.get_by_role("button", name="Validate and replace", exact=True).click()
            await expect(
                settings.get_by_role("status").filter(has_text="Credentials replaced")
            ).to_be_visible()
            await expect(settings.get_by_label("App Secret", exact=True)).to_have_value("")
            await expect(settings.get_by_label("Verification Token", exact=True)).to_have_value("")
            await expect(settings.get_by_label("Encrypt Key", exact=True)).to_have_value("")
            secrets_call = next(
                call for call in api_calls if call["path"].endswith("/bot-created/secrets")
            )
            assert secrets_call["body"]["expected_revision"] == 1

            # Closing with dirty secret drafts unmounts the real dialog. A fresh
            # mount and selection must not recover those secrets.
            await settings.get_by_label("App Secret", exact=True).fill("close-secret")
            await settings.get_by_label("Verification Token", exact=True).fill("close-token")
            await settings.get_by_label("Encrypt Key", exact=True).fill("close-encrypt")
            await settings.locator("footer").get_by_role("button", name="Close", exact=True).click()
            await expect(settings).to_have_count(0)
            await page.get_by_role("button", name="Open Bot settings", exact=True).click()
            settings = page.get_by_test_id("feishu-bot-settings-dialog")
            await expect(settings).to_be_visible()
            await expect(settings.get_by_text("Loading Bot pool…", exact=True)).to_have_count(0)
            await settings.locator('[aria-label="Bots"]').locator("button").filter(
                has_text="Primary Bot"
            ).click()
            await expect(settings.get_by_label("App Secret", exact=True)).to_have_value("")
            await expect(settings.get_by_label("Verification Token", exact=True)).to_have_value("")
            await expect(settings.get_by_label("Encrypt Key", exact=True)).to_have_value("")

            # Environment credentials and deletion are read-only. Occupancy is
            # visible in settings and the occupied Bot cannot be selected later.
            await settings.locator('[aria-label="Bots"]').locator("button").filter(
                has_text="Environment Bot"
            ).click()
            await expect(settings.locator(".detail")).to_contain_text("environment")
            await expect(
                settings.get_by_role("heading", name="Replace credentials", exact=True)
            ).to_have_count(0)
            await expect(
                settings.get_by_role("button", name="Delete Bot", exact=True)
            ).to_have_count(0)
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
            bot_select = binding_panel.get_by_label("Bot", exact=True)
            await expect(bot_select.locator('option[value="bot-occupied"]')).to_have_js_property(
                "disabled", True
            )
            await bot_select.select_option("bot-created")
            await binding_panel.get_by_role(
                "button", name="Generate pairing code", exact=True
            ).click()
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
                "Connected with Primary Bot"
            )
            activate_call = next(
                call for call in api_calls if call["path"].endswith("/pair/activate")
            )
            assert activate_call["body"] == {
                "pairing_id": "pair-created",
                "confirm_word": CONFIRM_WORD,
                "expected_revision": 4,
            }

            await binding_panel.get_by_role("button", name="Disconnect Feishu", exact=True).click()
            await binding_panel.get_by_role("button", name="Confirm disconnect", exact=True).click()
            await expect(page.get_by_test_id("feishu-binding-status")).to_have_text("Not connected")
            disconnect_call = next(
                call
                for call in api_calls
                if call["method"] == "DELETE" and call["path"].endswith("/pairing")
            )
            assert disconnect_call["body"] == {"expected_revision": 5}

            for forbidden in (
                CONFIRM_WORD,
                "create-secret",
                "create-token",
                "create-encrypt",
                "replace-secret",
                "replace-token",
                "replace-encrypt",
                "close-secret",
                "close-token",
                "close-encrypt",
            ):
                assert all(forbidden not in body for body in response_bodies)

            # Both real components remain operable at mobile width.
            await binding_panel.get_by_role("button", name="Close", exact=True).click()
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.get_by_role("button", name="Open Bot settings", exact=True).click()
            settings = page.get_by_test_id("feishu-bot-settings-dialog")
            await expect(settings).to_be_visible()
            await expect(settings.get_by_text("Loading Bot pool…", exact=True)).to_have_count(0)
            settings_box = await settings.bounding_box()
            assert settings_box is not None
            assert settings_box["x"] >= 0
            assert settings_box["x"] + settings_box["width"] <= 391
            await page.screenshot(path=str(output / "feishu-bot-pool-mobile.png"), full_page=True)
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
            assert not blocked_requests, blocked_requests
            assert not unknown_requests, unknown_requests
            assert not page_errors, page_errors
            await context.close()
        finally:
            await browser.close()

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


asyncio.run(main())
