"""Full-app UI contract smoke; every API request is mocked before navigation.

Run with a dedicated Vite server (never the primary 5173/8173 instance):
  python tests/browser_chat_work.py http://127.0.0.1:5287 /tmp/chat-work-ui [existing-browser]
Requires the existing Playwright Python environment and Chromium. This checks
the real Chat surface and controls, not provider execution or backend behavior.
"""

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, expect


async def main():
    if len(sys.argv) not in (3, 4):
        raise SystemExit(__doc__)
    base, output = sys.argv[1:3]
    executable = sys.argv[3] if len(sys.argv) == 4 else None
    parsed_base = urlparse(base)
    if (
        parsed_base.scheme != "http"
        or parsed_base.hostname not in ("127.0.0.1", "::1")
        or parsed_base.port in (None, 5173, 8173, 10025, 18183)
        or parsed_base.username is not None
        or parsed_base.password is not None
    ):
        raise SystemExit("Use an owned numeric-loopback review server")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    now = "2026-10-05T03:00:00Z"
    tabs = [
        dict(id=f"ui-tab-{i}", name=name, cwd="/fixture/project", agent_type="codex",
             session_kind="chat", port=0, is_active=True, created_at=now)
        for i, name in [(1, "Evaluation monitor"), (2, "Feature review")]
    ]
    monitor = dict(
        id="ui-work-1", source_tab_id="ui-tab-1", workspace_id="fixture-workspace",
        title="Watch evaluation progress", kind="monitor", status="waiting",
        agent_type="codex", model=None, cwd="/fixture/project", interval_seconds=300,
        next_run_at="2026-10-05T03:05:00Z", run_count=4, active_task_id=None,
        created_at=now, updated_at=now,
        latest_result=dict(kind="progress", summary="Evaluation is running normally.",
                           task_id="check-4", created_at=now, validation="HTTP health probe returned 200.",
                           report_id="report-4"),
        executions=[dict(task_id="check-4", status="done", outcome="progress",
                         created_at=now, updated_at=now, summary="No material change.")],
    )
    review = {**monitor, "id": "ui-work-2", "source_tab_id": "ui-tab-2", "kind": "task",
              "title": "Review feature changes", "status": "review", "interval_seconds": None,
              "next_run_at": None, "latest_result": None, "executions": []}
    actions, requests, errors = [], [], []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=executable)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900}, service_workers="block"
        )

        async def block_websocket(socket):
            await socket.close(code=1000, reason="mocked UI smoke")

        async def api(route):
            request = route.request
            parsed = urlparse(request.url)
            if (parsed.scheme, parsed.hostname, parsed.port) != (
                parsed_base.scheme, parsed_base.hostname, parsed_base.port
            ):
                await route.abort("blockedbyclient")
                return
            path = parsed.path
            if not path.startswith("/api/"):
                await route.continue_()
                return
            requests.append((request.method, path))
            if path == "/api/auth/check":
                body = dict(auth_required=False, user=None)
            elif path == "/api/tabs":
                body = tabs
            elif path == "/api/tabs/status":
                body = []
            elif path.endswith("/goal/current"):
                body = None
            elif path.endswith("/work"):
                body = [monitor if "ui-tab-1" in path else review]
            elif "/work/" in path:
                assert request.method == "PATCH", request.method
                change = request.post_data_json
                actions.append(change)
                current = monitor if "ui-tab-1" in path else review
                if "action" in change:
                    current["status"] = {"pause": "paused", "resume": "waiting", "stop": "stopped"}[change["action"]]
                if "interval_seconds" in change:
                    current["interval_seconds"] = change["interval_seconds"]
                body = current
            elif path.endswith("/capabilities"):
                body = dict(structured=True, send_message=True, supports_goals=True, supports_images=False,
                            agent_type="codex", supports_mode_switch=False)
            elif path.endswith("/events") or path.endswith("/wait"):
                if path.endswith("/wait"):
                    await asyncio.sleep(0.5)
                body = dict(events=[], next_sequence=-1, has_more=False)
            elif path.endswith("/live"):
                await route.fulfill(status=200, content_type="text/event-stream", body=": fixture\n\n")
                return
            elif path.endswith("/view"):
                body = dict(ok=True)
            elif "network" in path:
                body = dict(hostname="fixture", addresses=[])
            else:
                body = []
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

        await context.route_web_socket("**/*", block_websocket)
        await context.route("**/*", api)
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(base + "/?tab=ui-tab-1")
        card = page.locator('[data-work-id="ui-work-1"]')
        await expect(card).to_be_visible()
        await card.get_by_role("button", name="Watch evaluation progress Waiting").click()
        composer = page.locator("textarea.composer-input")
        if await composer.count() == 0:
            composer = page.locator(".structured-composer textarea")
        await expect(composer).to_be_enabled()
        await composer.fill("Continue with a different question")
        await card.get_by_role("button", name="Pause checks").click()
        await expect(card).to_contain_text("Future checks are paused")
        await card.get_by_role("spinbutton", name="Check interval in minutes").fill("60")
        await card.get_by_role("button", name="Save interval").click()
        await expect(card).to_contain_text("Every 1h")
        await card.get_by_role("button", name="Resume", exact=True).click()
        await expect(card).to_contain_text("Waiting")
        monitor["latest_result"]["kind"] = "anomaly"
        monitor["latest_result"]["summary"] = "Evaluation stalled: investigate worker logs."
        await page.get_by_role("button", name="Refresh background work").click()
        await expect(card).to_contain_text("Attention needed")
        await expect(page.get_by_role("log", name="Chat conversation")).not_to_contain_text("Evaluation stalled")
        await expect(composer).to_have_value("Continue with a different question")
        await card.get_by_text("Reported validation and source").click()
        await expect(card).to_contain_text("report-4")
        await page.screenshot(path=str(output / "desktop.png"), full_page=True)

        await page.locator(".chat-sidebar__item-main").filter(has_text="Feature review").click()
        other = page.locator('[data-work-id="ui-work-2"]')
        await expect(other).to_be_visible()
        await expect(card).not_to_be_visible()
        await other.get_by_role("button", name="Review feature changes Needs review").click()
        await expect(other).to_contain_text("Waiting for review or acceptance")
        await expect(other.get_by_role("button", name="Pause checks")).to_have_count(0)
        await page.locator(".chat-sidebar__item-main").filter(has_text="Evaluation monitor").click()
        await expect(card).to_be_visible()
        await expect(composer).to_have_value("Continue with a different question")
        await card.get_by_role("button", name="Stop work").click()
        await expect(card).to_contain_text("Stop requested")
        await expect(card).to_contain_text("external operations may still finish")
        await page.reload()
        await expect(card).to_contain_text("Stop requested")
        await card.get_by_role("button", name="Watch evaluation progress Stop requested").click()
        await expect(card.get_by_role("button", name="Resume", exact=True)).to_have_count(0)

        await page.set_viewport_size({"width": 390, "height": 844})
        # Resizing starts the existing sidebar's drawer transition; wait for it
        # to finish rather than treating an animating close icon as an open drawer.
        await page.wait_for_function("document.querySelector('.chat-sidebar.mobile-drawer')?.getBoundingClientRect().right <= 0")
        await page.screenshot(path=str(output / "mobile.png"), full_page=True)
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        await expect(composer).to_be_visible()
        rect = await composer.bounding_box()
        assert rect["y"] + rect["height"] <= 844
        assert actions == [{"action": "pause"}, {"interval_seconds": 3600},
                           {"action": "resume"}, {"action": "stop"}], actions
        assert not errors, errors
        await context.close()
        await browser.close()
    report = dict(result="PASS", coverage=["full Chat app", "pause/resume/stop", "change interval",
        "validation source", "review is not completed", "tab switch isolation", "draft preserved",
        "reload persistence via mocked API", "mobile layout", "no transcript injection"],
        mutations=actions, api_requests=len(requests), page_errors=errors,
        boundary="All API requests mocked; no backend or provider execution acceptance.")
    (output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


asyncio.run(main())
