"""Exercise full Workspace task-detail actions with mocked APIs.

Usage:
  python tests/browser_workspace_detail.py OWNED_HARNESS_URL NEW_OUTPUT_DIR EXISTING_BROWSER
"""

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from browser_agent_workspace_task_create import CAPABILITIES, WORKSPACES, task_fixture
from browser_lifecycle import BrowserRun, run_browser_main
from playwright.async_api import expect


async def main(browser_run: BrowserRun) -> None:
    url = sys.argv[1]
    base = urlparse(url)
    if (
        base.scheme != "http"
        or base.hostname not in ("127.0.0.1", "::1")
        or base.port in (None, 5173, 8173, 10025, 18183)
        or base.username is not None
        or base.password is not None
        or base.path != "/tests/agent_workspace_task_create_harness.html"
    ):
        raise SystemExit("Use an owned loopback Vite server and the full Workspace harness")
    output = Path(sys.argv[2])
    output.mkdir(parents=True, exist_ok=True)
    origin = (base.scheme, base.hostname, base.port)
    task = task_fixture("detail-task", "ws-1", "Workspace detail actions")
    starts: list[dict] = []
    page_errors: list[str] = []
    console_errors: list[str] = []
    blocked: list[tuple[str, str]] = []
    unknown: list[tuple[str, str]] = []
    observations: list[dict] = []

    def board() -> dict:
        return {
            "workspace": WORKSPACES[0],
            "tasks": [task],
            "sessions": [],
            "reports": [],
            "markdown_documents": [],
            "snapshot_path": None,
            "tasks_pagination": {
                "next_cursor": None,
                "has_more": False,
                "status_counts": {"todo": 1},
            },
        }

    async with browser_run.context(viewport={"width": 390, "height": 844}) as context:
        await context.add_init_script(
            "localStorage.setItem('claude_hub_active_workspace_id', 'ws-1');"
        )

        async def block_websocket(socket) -> None:
            await socket.close(code=1000, reason="mocked Workspace detail scenario")

        async def route_all(route) -> None:
            request = route.request
            parsed = urlparse(request.url)
            path, method = parsed.path, request.method
            if (parsed.scheme, parsed.hostname, parsed.port) != origin:
                blocked.append((method, request.url))
                await route.abort()
                return
            if not path.startswith("/api/"):
                if method == "GET":
                    await route.continue_()
                else:
                    unknown.append((method, path))
                    await route.fulfill(status=405, body="blocked static mutation")
                return
            if method == "GET" and path == "/api/env-presets":
                body = {"custom_presets": [], "hidden_builtin_ids": []}
            elif method == "GET" and path in (
                "/api/remote/profiles",
                "/api/tabs/status",
                "/api/tabs",
            ):
                body = []
            elif method == "GET" and path == "/api/workspaces":
                body = [WORKSPACES[0]]
            elif method == "GET" and path == "/api/tabs/sidebar-preferences":
                body = {"pinned_chat_ids": []}
            elif method == "POST" and path == "/api/tabs/sidebar-preferences/migrate-browser-pins":
                body = {"pinned_chat_ids": []}
            elif method == "GET" and path == "/api/workspaces/ws-1/task-capabilities":
                body = CAPABILITIES
            elif method == "GET" and path == "/api/workspaces/ws-1/board":
                assert set(parse_qs(parsed.query)) <= {"tasks_limit"}
                body = board()
            elif method == "GET" and path in (
                "/api/workspaces/ws-1/lessons",
                "/api/workspaces/ws-1/tasks/detail-task/reports",
            ):
                body = []
            elif method == "POST" and path == "/api/workspaces/tasks/detail-task/start":
                payload = request.post_data_json
                assert payload == {
                    "target_session_id": None,
                    "related_task_id": None,
                    "clear_context": None,
                }
                starts.append(payload)
                task["status"] = "working"
                body = task
            else:
                unknown.append((method, path))
                await route.fulfill(status=501, body=f"unmocked API: {method} {path}")
                return
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

        await context.route_web_socket("**/*", block_websocket)
        await context.route("**/*", route_all)
        page = await context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on(
            "console",
            lambda message: (
                console_errors.append(message.text) if message.type == "error" else None
            ),
        )
        for width in (390, 720, 1280):
            task["status"] = "todo"
            await page.set_viewport_size({"width": width, "height": 844})
            await page.goto(url, wait_until="networkidle")
            await page.locator(".task-card h3").filter(has_text=task["title"]).click()
            panel = page.locator(".task-detail-panel")
            await expect(panel).to_be_visible()
            drawer = panel.locator(".detail-action-drawer")
            toggle = panel.locator(".detail-footer-toggle")
            if width <= 760:
                await expect(toggle).to_be_visible()
                await expect(drawer).to_be_hidden()
                await toggle.click()
                await expect(toggle).to_have_attribute("aria-expanded", "true")
            else:
                await expect(toggle).to_be_hidden()
            await expect(drawer).to_be_visible()
            assert await drawer.evaluate("element => getComputedStyle(element).display") == "block"
            start = drawer.get_by_role("button", name="Start", exact=True)
            await expect(start).to_be_enabled()
            await page.screenshot(path=str(output / f"detail-actions-{width}.png"), full_page=True)
            await start.click()
            await expect(panel.locator(".detail-eyebrow")).to_have_text("working")
            await expect(start).to_be_hidden()
            if width <= 760:
                await toggle.click()
                await expect(toggle).to_have_attribute("aria-expanded", "false")
                await expect(drawer).to_be_hidden()
            observations.append(
                {"width": width, "drawer_display": "block", "start_submitted": True}
            )
            await panel.get_by_role("button", name="Close task detail", exact=True).click()
            await expect(panel).to_be_hidden()

        assert len(starts) == 3
        assert not page_errors, page_errors
        assert not console_errors, console_errors
        assert not blocked, blocked
        assert not unknown, unknown
        (output / "detail-actions.json").write_text(
            json.dumps({"observations": observations, "starts": starts}, indent=2) + "\n",
            encoding="utf-8",
        )
        (output / "report.json").write_text(
            json.dumps(
                {
                    "result": "PASS",
                    "observations": observations,
                    "start_requests": len(starts),
                    "page_errors": page_errors,
                    "console_errors": console_errors,
                    "blocked_requests": blocked,
                    "unknown_requests": unknown,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    run_browser_main(main)
