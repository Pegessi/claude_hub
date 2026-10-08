"""Full-SPA DOM smoke for manual Feishu Bot configuration.

Usage: browser_feishu_bot_config.py <owned-loopback-spa-url> <output-dir> <browser>
Every API is mocked; WebSockets, service workers and external requests are blocked.
Only an existing browser and a task-owned static build server may be used.
"""

import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, expect


def origin(url):
    parsed = urlparse(url)
    return (
        parsed.scheme,
        parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )


def safe_status(**overrides):
    return {
        "configured": False,
        "source": "none",
        "can_manage": False,
        "editable": False,
        "event_url": None,
        "revision": 0,
        **overrides,
    }


def admin_view(status, **overrides):
    return {
        **status,
        "app_id": "cli-bot" if status["configured"] else None,
        "app_secret_configured": status["configured"],
        "verification_token_configured": status["configured"],
        "encrypt_key_configured": status["configured"],
        **overrides,
    }


async def main():
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    base = sys.argv[1].rstrip("/")
    output = Path(sys.argv[2]).resolve()
    executable = Path(sys.argv[3]).resolve(strict=True)
    parsed_base = urlparse(base)
    if (
        parsed_base.scheme not in {"http", "https"}
        or parsed_base.hostname not in {"127.0.0.1", "::1"}
        or parsed_base.port in {None, 5173, 8173}
        or parsed_base.username
        or parsed_base.password
        or parsed_base.path not in {"", "/"}
        or parsed_base.query
        or parsed_base.fragment
    ):
        raise AssertionError(
            "Use an explicit owned numeric-loopback static server origin"
        )
    if not executable.is_file():
        raise AssertionError("An existing browser executable is required")
    output.mkdir(parents=True, exist_ok=True)
    tabs = [
        {
            "id": "ui-tab-1",
            "name": "Bot settings review",
            "cwd": "/fixture/project",
            "agent_type": "codex",
            "session_kind": "chat",
            "port": 0,
            "is_active": True,
            "created_at": "2026-10-06T09:00:00Z",
        }
    ]
    current_status = safe_status()
    current_admin = None
    put_outcome = "success"
    delete_outcome = "success"
    status_get_count = config_get_count = put_count = delete_count = 0
    last_put_body = last_delete_body = None
    api_requests, fallback_paths, external, websockets = [], [], [], []
    page_errors, console_errors, screenshots = [], [], []
    browser = context = page = failure = None

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                headless=True,
                executable_path=str(executable),
                args=[
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--no-default-browser-check",
                ],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                service_workers="block",
            )

            async def block_websocket(websocket):
                websockets.append(websocket.url)
                await websocket.close(code=1000, reason="mocked browser smoke")

            await context.route_web_socket("**/*", block_websocket)

            async def route_all(route):
                nonlocal current_status, current_admin
                nonlocal status_get_count, config_get_count, put_count, delete_count
                nonlocal last_put_body, last_delete_body
                request = route.request
                parsed = urlparse(request.url)
                if origin(request.url) != origin(base):
                    external.append(request.url)
                    await route.abort("blockedbyclient")
                    return
                if parsed.path == "/vite.svg":
                    await route.fulfill(
                        content_type="image/svg+xml",
                        body='<svg xmlns="http://www.w3.org/2000/svg"/>',
                    )
                    return
                if not parsed.path.startswith("/api/"):
                    if request.resource_type == "document":
                        response = await route.fetch(max_redirects=0)
                        html = await response.text()
                        # Preconnect can bypass request routing; remove known external fonts first.
                        html = re.sub(
                            r"<link\b[^>]*https://fonts\.(?:googleapis|gstatic)\.com[^>]*>",
                            "",
                            html,
                        )
                        await route.fulfill(response=response, body=html)
                    else:
                        await route.continue_()
                    return
                path = parsed.path
                api_requests.append((request.method, path))
                if path == "/api/auth/check":
                    body = {"auth_required": False, "user": None}
                elif path in {
                    "/api/tabs/status",
                    "/api/env-presets",
                    "/api/tabs/archived",
                }:
                    body = []
                elif path == "/api/tabs":
                    body = tabs
                elif path == "/api/feishu/bot/binding":
                    body = {"binding": None}
                elif path == "/api/feishu/bot/config/status":
                    assert request.method == "GET"
                    status_get_count += 1
                    body = current_status
                elif path == "/api/feishu/bot/config":
                    if request.method == "GET":
                        config_get_count += 1
                        assert (
                            current_status["can_manage"] and current_admin is not None
                        )
                        body = current_admin
                    elif request.method == "PUT":
                        put_count += 1
                        last_put_body = request.post_data_json
                        if put_outcome == "busy":
                            await route.fulfill(
                                status=503,
                                content_type="application/json",
                                body=json.dumps({"detail": "config_operation_busy"}),
                            )
                            return
                        if put_outcome == "validation_failure":
                            await route.fulfill(
                                status=502,
                                content_type="application/json",
                                body=json.dumps(
                                    {"detail": "upstream echoed secret-one"}
                                ),
                            )
                            return
                        if (
                            put_outcome == "app_id_confirmation"
                            and not last_put_body["allow_app_id_change"]
                        ):
                            await route.fulfill(
                                status=409,
                                content_type="application/json",
                                body=json.dumps(
                                    {"detail": "app_id_change_confirmation_required"}
                                ),
                            )
                            return
                        if put_outcome == "revision_conflict":
                            current_status = safe_status(
                                configured=True,
                                source="stored",
                                can_manage=True,
                                editable=True,
                                event_url="https://hub.example.test/api/feishu/bot/events",
                                revision=11,
                            )
                            current_admin = admin_view(current_status)
                            await route.fulfill(
                                status=409,
                                content_type="application/json",
                                body=json.dumps({"detail": "config_revision_conflict"}),
                            )
                            return
                        assert put_outcome in {"success", "app_id_confirmation"}
                        current_status = safe_status(
                            configured=True,
                            source="stored",
                            can_manage=True,
                            editable=True,
                            event_url="https://hub.example.test/api/feishu/bot/events",
                            revision=last_put_body["expected_revision"] + 1,
                        )
                        current_admin = admin_view(
                            current_status, app_id=last_put_body["app_id"]
                        )
                        body = current_admin
                    elif request.method == "DELETE":
                        delete_count += 1
                        last_delete_body = request.post_data_json
                        current_status = safe_status(
                            can_manage=True,
                            editable=True,
                            revision=last_delete_body["expected_revision"] + 1,
                        )
                        current_admin = admin_view(current_status)
                        body = current_admin
                        if delete_outcome == "committed_error":
                            await route.fulfill(
                                status=500,
                                content_type="application/json",
                                body=json.dumps({"detail": "routing_cleanup_failed"}),
                            )
                            return
                    else:
                        raise AssertionError("Unexpected config request method")
                elif path.endswith("/goal/current"):
                    body = None
                elif path.endswith("/capabilities"):
                    body = {
                        "structured": True,
                        "send_message": True,
                        "supports_goals": True,
                        "supports_images": False,
                        "agent_type": "codex",
                        "supports_mode_switch": False,
                    }
                elif path.endswith("/events") or path.endswith("/wait"):
                    if path.endswith("/wait"):
                        await asyncio.sleep(0.25)
                    body = {"events": [], "next_sequence": -1, "has_more": False}
                elif path.endswith("/live"):
                    await route.fulfill(
                        status=200,
                        content_type="text/event-stream",
                        body=": mocked\n\n",
                    )
                    return
                elif path.endswith("/view"):
                    body = {"ok": True}
                elif "network" in path:
                    body = {"hostname": "fixture", "addresses": []}
                else:
                    fallback_paths.append(path)
                    body = []
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(body)
                )

            await context.route("**/*", route_all)
            page = await context.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on(
                "console",
                lambda message: (
                    console_errors.append(message.text)
                    if message.type == "error"
                    else None
                ),
            )

            async def open_settings():
                await page.get_by_label("Extensions", exact=True).click()
                await page.get_by_role("button", name="Feishu Bot settings").click()
                await expect(
                    page.get_by_test_id("feishu-bot-settings-dialog")
                ).to_be_visible()

            async def close_settings():
                await page.get_by_role(
                    "button", name="Close Feishu Bot settings"
                ).click()
                await expect(
                    page.get_by_test_id("feishu-bot-settings-dialog")
                ).to_have_count(0)

            async def fill_credentials(prefix):
                await page.get_by_label("App ID", exact=True).fill("cli-bot")
                await page.get_by_label("App Secret").fill(f"{prefix}-secret")
                await page.get_by_label("Verification Token").fill(
                    f"{prefix}-verification"
                )
                await page.get_by_label("Encrypt Key").fill(f"{prefix}-encrypt")

            async def secrets_empty():
                for label in ("App Secret", "Verification Token", "Encrypt Key"):
                    await expect(page.get_by_label(label)).to_have_value("")

            async def no_overflow():
                metrics = await page.evaluate(
                    "({ scrollWidth: document.documentElement.scrollWidth, width: innerWidth })"
                )
                assert metrics["scrollWidth"] <= metrics["width"] + 1, metrics

            await page.goto(base + "/?tab=ui-tab-1", wait_until="domcontentloaded")
            await open_settings()
            panel = page.get_by_test_id("feishu-bot-settings-dialog")
            await expect(panel).to_contain_text(
                "Feishu Bot needs administrator configuration"
            )
            await expect(panel.locator("input")).to_have_count(0)
            await no_overflow()
            assert status_get_count == 1 and config_get_count == 0
            await close_settings()

            current_status = safe_status(
                configured=True,
                source="stored",
                can_manage=True,
                editable=True,
                event_url="https://hub.example.test/api/feishu/bot/events",
                revision=7,
            )
            current_admin = admin_view(current_status)
            await open_settings()
            await fill_credentials("close-test")
            await close_settings()
            await open_settings()
            await secrets_empty()
            await fill_credentials("save-test")
            await page.get_by_role("button", name="Validate and save").click()
            await expect(panel.get_by_role("status")).to_contain_text(
                "Configure the event callback in Feishu before testing real messages."
            )
            await secrets_empty()
            await no_overflow()
            assert put_count == 1
            assert last_put_body == {
                "app_id": "cli-bot",
                "app_secret": "save-test-secret",
                "verification_token": "save-test-verification",
                "encrypt_key": "save-test-encrypt",
                "expected_revision": 7,
                "allow_app_id_change": False,
            }
            last_put_body = None
            await page.screenshot(
                path=str(output / "desktop-saved.png"), full_page=True
            )
            screenshots.append("desktop-saved.png")

            # Only explicit App ID confirmation may resend the same credential payload.
            await fill_credentials("app-change")
            await page.get_by_label("App ID", exact=True).fill("cli-bot-next")
            put_outcome = "app_id_confirmation"
            before_app_change_puts = put_count
            await page.get_by_role("button", name="Validate and save").click()
            app_change = page.get_by_role("alertdialog", name="Confirm App ID change")
            await expect(app_change).to_be_visible()
            first_app_change_body = dict(last_put_body)
            assert first_app_change_body["allow_app_id_change"] is False
            await app_change.get_by_role("button", name="Confirm App ID change").click()
            await expect(panel.get_by_role("status")).to_contain_text(
                "Configure the event callback in Feishu before testing real messages."
            )
            assert put_count == before_app_change_puts + 2
            assert last_put_body == {
                **first_app_change_body,
                "allow_app_id_change": True,
            }
            await secrets_empty()
            last_put_body = first_app_change_body = None
            await close_settings()

            current_status = safe_status(
                configured=True,
                source="environment",
                can_manage=True,
                editable=False,
                event_url="https://hub.example.test/api/feishu/bot/events",
                revision=None,
            )
            current_admin = admin_view(current_status, app_id="cli-env")
            await open_settings()
            await expect(panel).to_contain_text("Managed by environment variables")
            await expect(panel.locator("input")).to_have_count(0)
            await expect(
                panel.get_by_role("button", name="Deactivate Bot")
            ).to_have_count(0)
            await close_settings()

            current_status = safe_status(
                configured=True,
                source="stored",
                can_manage=True,
                editable=True,
                event_url="https://hub.example.test/api/feishu/bot/events",
                revision=9,
            )
            current_admin = admin_view(current_status)
            await open_settings()
            await fill_credentials("failure-test")
            put_outcome = "validation_failure"
            await page.get_by_role("button", name="Validate and save").click()
            await expect(panel.get_by_role("alert")).to_have_text(
                "Feishu could not validate this configuration. Check the credentials and network, then try again."
            )
            await expect(
                panel.get_by_text("Configuration validated and saved", exact=False)
            ).to_have_count(0)
            await expect(panel.get_by_role("alert")).not_to_contain_text("secret-one")
            put_outcome = "busy"
            before_busy_gets = status_get_count
            await page.get_by_role("button", name="Validate and save").click()
            await expect(panel.get_by_role("alert")).to_have_text(
                "Another Bot configuration operation is in progress. Wait and try again."
            )
            await expect(page.get_by_label("App Secret")).to_have_value(
                "failure-test-secret"
            )
            assert status_get_count == before_busy_gets
            await close_settings()

            current_status["revision"] = 10
            current_admin = admin_view(current_status)
            await open_settings()
            await fill_credentials("conflict-test")
            before_puts, before_gets = put_count, status_get_count
            put_outcome = "revision_conflict"
            await page.get_by_role("button", name="Validate and save").click()
            await expect(panel.get_by_role("alert")).to_have_text(
                "The configuration changed. Review the latest state and enter all secrets again."
            )
            assert put_count == before_puts + 1 and status_get_count == before_gets + 1
            await secrets_empty()
            await close_settings()

            put_outcome = "success"
            await open_settings()
            await page.get_by_role("button", name="Deactivate Bot", exact=True).click()
            confirmation = page.get_by_role(
                "alertdialog", name="Confirm Bot deactivation"
            )
            await expect(confirmation).to_contain_text(
                "Chat history and message source labels remain"
            )
            await expect(confirmation).to_contain_text(
                "Already-sent network requests cannot be recalled"
            )
            await confirmation.get_by_role(
                "button", name="Confirm deactivation"
            ).click()
            await expect(panel.get_by_role("status")).to_contain_text(
                "Existing Chat history was preserved"
            )
            assert delete_count == 1 and last_delete_body == {"expected_revision": 11}
            last_delete_body = None
            await close_settings()

            current_status = safe_status(
                configured=True,
                source="stored",
                can_manage=True,
                editable=True,
                revision=30,
            )
            current_admin = admin_view(current_status)
            delete_outcome = "committed_error"
            await open_settings()
            await page.get_by_role("button", name="Deactivate Bot", exact=True).click()
            before_failure_deletes, before_failure_gets = delete_count, status_get_count
            await page.get_by_role("button", name="Confirm deactivation").click()
            await expect(panel.get_by_role("alert")).to_have_text(
                "The configuration may have changed, but binding cleanup failed. Check the latest configuration before taking another action."
            )
            await expect(panel.locator(".feishu-config-status strong")).to_have_text(
                "Not configured"
            )
            assert delete_count == before_failure_deletes + 1
            assert status_get_count == before_failure_gets + 1
            assert last_delete_body == {"expected_revision": 30}
            last_delete_body = None
            await close_settings()

            current_status, current_admin = safe_status(), None
            before_mobile_gets = config_get_count
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.get_by_label("App menu", exact=True).click()
            await page.get_by_role("button", name="Feishu Bot settings").click()
            await expect(panel).to_be_visible()
            await expect(panel).to_contain_text("Contact an administrator")
            await expect(panel.locator("input")).to_have_count(0)
            assert config_get_count == before_mobile_gets
            await no_overflow()
            await page.screenshot(
                path=str(output / "mobile-ordinary.png"), full_page=True
            )
            screenshots.append("mobile-ordinary.png")
            assert not fallback_paths, fallback_paths
            assert not page_errors, page_errors
        except BaseException as exc:
            failure = exc
            if page is not None:
                try:
                    await page.screenshot(
                        path=str(output / "failure.png"), full_page=True
                    )
                    screenshots.append("failure.png")
                except Exception:
                    pass
        finally:
            try:
                if context is not None:
                    await context.close()
            finally:
                if browser is not None:
                    await browser.close()

    report = {
        "result": "PASS" if failure is None else "FAIL",
        "coverage": [
            "desktop/mobile global entry",
            "ordinary safe status only",
            "complete credential save",
            "explicit App ID change confirmation with unchanged credentials/revision",
            "secret clearing on save/close/conflict",
            "environment read-only",
            "fixed validation error",
            "busy failure preserves input without reload",
            "post-commit DELETE failure reloads state without repeating mutation",
            "revision refresh without PUT retry",
            "deactivation confirmation/history wording",
            "no overflow",
        ],
        "screenshots": screenshots,
        "api_request_count": len(api_requests),
        "fallback_api_paths": sorted(set(fallback_paths)),
        "status_get_count": status_get_count,
        "config_get_count": config_get_count,
        "put_count": put_count,
        "delete_count": delete_count,
        "blocked_websockets": websockets,
        "blocked_external_requests": external,
        "page_errors": page_errors,
        "console_errors": console_errors,
        "browser_executable": str(executable),
        "boundary": "All APIs mocked; no Hub backend, provider, real credentials or Feishu traffic.",
    }
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    if failure is not None:
        raise failure


if __name__ == "__main__":
    asyncio.run(main())
