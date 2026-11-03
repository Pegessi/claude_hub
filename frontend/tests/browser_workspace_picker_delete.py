"""Delete workspaces from the New-Agent Workspace picker with mocked APIs.

Covers InfoWall 16ef41f6: the picker popover exposes a per-row delete action;
cancel leaves everything intact, accepting removes the row through DELETE
/api/workspaces/{id}, the active workspace deletion falls back cleanly, and the
last deletion leaves the empty state. Selection switching is exercised through
the same real popover. Runs at desktop (1280) and mobile (390) widths.

Usage:
  python tests/browser_workspace_picker_delete.py OWNED_HARNESS_URL NEW_OUTPUT_DIR EXISTING_BROWSER
"""

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from browser_agent_workspace_task_create import CAPABILITIES, WORKSPACES, task_fixture
from browser_lifecycle import BrowserRun, run_browser_main
from playwright.async_api import expect

WS_1_BOARD = {
    "workspace": WORKSPACES[0],
    "tasks": [task_fixture("busy-task", "ws-1", "Running task")],
    # One live orchestrator so the active-workspace confirm names the resource.
    "sessions": [
        {
            "id": "agent-1",
            "workspace_id": "ws-1",
            "title": "Workspace Agent",
            "role": "orchestrator",
            "runtime_status": "working",
            "archived": False,
            "ephemeral": False,
        }
    ],
    "reports": [],
    "markdown_documents": [],
    "snapshot_path": None,
    "tasks_pagination": {
        "next_cursor": None,
        "has_more": False,
        "status_counts": {"working": 1},
    },
}


def empty_board(workspace: dict) -> dict:
    return {
        "workspace": workspace,
        "tasks": [],
        "sessions": [],
        "reports": [],
        "markdown_documents": [],
        "snapshot_path": None,
        "tasks_pagination": {
            "next_cursor": None,
            "has_more": False,
            "status_counts": {},
        },
    }


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

    observations: list[dict] = []

    async with browser_run.context(viewport={"width": 1280, "height": 844}) as context:
        await context.add_init_script(
            "localStorage.setItem('claude_hub_active_workspace_id', 'ws-1');"
        )

        async def block_websocket(socket) -> None:
            await socket.close(code=1000, reason="mocked Workspace picker scenario")

        deleted: set[str] = set()
        deletes: list[str] = []
        dialog_messages: list[str] = []
        policy = {"accept": True}
        page_errors: list[str] = []
        console_errors: list[str] = []
        blocked: list[tuple[str, str]] = []
        unknown: list[tuple[str, str]] = []

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
            if method == "DELETE" and path.startswith("/api/workspaces/"):
                workspace_id = path.rsplit("/", 1)[-1]
                deleted.add(workspace_id)
                deletes.append(workspace_id)
                await route.fulfill(status=204)
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
                body = [item for item in WORKSPACES if item["id"] not in deleted]
            elif method == "GET" and path == "/api/tabs/sidebar-preferences":
                body = {"pinned_chat_ids": []}
            elif method == "POST" and path == "/api/tabs/sidebar-preferences/migrate-browser-pins":
                body = {"pinned_chat_ids": []}
            elif method == "GET" and path.endswith("/task-capabilities"):
                body = CAPABILITIES
            elif method == "GET" and path.startswith("/api/workspaces/") and "/board" in path:
                workspace_id = path.split("/")[3]
                body = WS_1_BOARD if workspace_id == "ws-1" else empty_board(WORKSPACES[1])
            elif method == "GET" and path.startswith("/api/workspaces/") and "/lessons" in path:
                body = []
            else:
                unknown.append((method, path))
                await route.fulfill(status=501, body=f"unmocked API: {method} {path}")
                return
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

        async def handle_dialog(dialog) -> None:
            dialog_messages.append(dialog.message)
            if policy["accept"]:
                await dialog.accept()
            else:
                await dialog.dismiss()

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
        page.on("dialog", handle_dialog)

        for width in (1280, 390):
            deleted.clear()
            deletes.clear()
            dialog_messages.clear()
            await page.set_viewport_size({"width": width, "height": 844})
            await page.goto(url, wait_until="networkidle")

            trigger = page.get_by_role("button", name="Select workspace")
            await expect(trigger).to_contain_text("Workspace One")
            await expect(trigger).to_have_attribute("aria-expanded", "false")

            # Open the popover: both workspaces offer select + delete rows.
            await trigger.click()
            panel = page.get_by_role("menu", name="Workspaces")
            await expect(panel).to_be_visible()
            options = panel.locator(".workspace-picker__option")
            await expect(options.nth(0)).to_contain_text("Workspace One")
            await expect(options.nth(1)).to_contain_text("Workspace Two")
            await expect(options.nth(0)).to_have_attribute("aria-checked", "true")
            delete_two = panel.get_by_role(
                "menuitem", name='Delete workspace "Workspace Two"', exact=True
            )
            await expect(delete_two).to_be_visible()

            # Dismissing the native confirm must not delete anything.
            policy["accept"] = False
            await delete_two.click()
            assert deletes == [], deletes
            assert dialog_messages[-1] == (
                'Delete workspace "Workspace Two"? This cannot be undone.'
            ), dialog_messages[-1]
            await trigger.click()
            await expect(options.nth(1)).to_be_visible()

            # Selecting the other workspace switches the board, then switch back.
            await options.nth(1).click()
            await expect(trigger).to_contain_text("Workspace Two")
            await trigger.click()
            await panel.locator(".workspace-picker__option").nth(0).click()
            await expect(trigger).to_contain_text("Workspace One")

            # Accepting the delete on a non-active workspace removes only that row.
            await trigger.click()
            policy["accept"] = True
            await panel.get_by_role(
                "menuitem", name='Delete workspace "Workspace Two"', exact=True
            ).click()
            await expect(trigger).to_contain_text("Workspace One")
            assert deletes == ["ws-2"], deletes
            await trigger.click()
            remaining = panel.locator(".workspace-picker__option")
            assert await remaining.count() == 1
            await expect(remaining.nth(0)).to_contain_text("Workspace One")

            # Deleting the active (last) workspace names its open resources and
            # leaves the picker in the empty state.
            policy["accept"] = False
            await panel.get_by_role(
                "menuitem", name='Delete workspace "Workspace One"', exact=True
            ).click()
            active_confirm = dialog_messages[-1]
            assert (
                "1 open task" in active_confirm and "1 live session" in active_confirm
            ), active_confirm
            assert deletes == ["ws-2"], deletes

            await trigger.click()
            policy["accept"] = True
            await panel.get_by_role(
                "menuitem", name='Delete workspace "Workspace One"', exact=True
            ).click()
            assert deletes == ["ws-2", "ws-1"], deletes
            await expect(trigger).to_contain_text("Select workspace")
            await trigger.click()
            await expect(
                panel.get_by_text("No workspaces yet. Create one with New Workspace.")
            ).to_be_visible()
            await page.screenshot(path=str(output / f"picker-delete-{width}.png"))
            await page.keyboard.press("Escape")
            observations.append(
                {
                    "width": width,
                    "deletes": list(deletes),
                    "cancel_kept_row": True,
                    "selection_switched": True,
                    "active_confirm": active_confirm,
                    "empty_state": True,
                }
            )

        assert not page_errors, page_errors
        assert not console_errors, console_errors
        assert not blocked, blocked
        assert not unknown, unknown
        (output / "picker-delete.json").write_text(
            json.dumps({"observations": observations}, indent=2) + "\n",
            encoding="utf-8",
        )
        (output / "report.json").write_text(
            json.dumps(
                {
                    "result": "PASS",
                    "observations": observations,
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
