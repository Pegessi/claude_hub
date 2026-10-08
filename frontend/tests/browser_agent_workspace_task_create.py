"""Real AgentWorkspaceView Task-create smoke with every API request mocked.

Usage:
  python tests/browser_agent_workspace_task_create.py \
    http://127.0.0.1:5293/tests/agent_workspace_task_create_harness.html \
    /tmp/agent-workspace-create [chromium]
"""

import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, expect

NOW = "2026-10-08T00:00:00Z"
CAPABILITIES = {
    "supported_execution_controls": ["workspace", "initiator"],
    "progress_states": [
        "started",
        "working",
        "blocked",
        "needs_input",
        "completed",
        "failed",
        "released",
    ],
    "record_only_requires_reporter_key": True,
    "handoff_requires_release": True,
}
WORKSPACES = [
    {
        "id": "ws-1",
        "name": "Workspace One",
        "path": "/fixture/one",
        "default_branch": "main",
        "session_prefix": "one",
        "target": "local",
        "created_at": NOW,
        "updated_at": NOW,
    },
    {
        "id": "ws-2",
        "name": "Workspace Two",
        "path": "/fixture/two",
        "default_branch": "main",
        "session_prefix": "two",
        "target": "local",
        "created_at": NOW,
        "updated_at": NOW,
    },
]


def task_fixture(
    task_id: str,
    workspace_id: str,
    title: str,
    *,
    execution_control: str = "workspace",
    source: dict | None = None,
) -> dict:
    return {
        "id": task_id,
        "workspace_id": workspace_id,
        "title": title,
        "prompt": f"Description for {title}",
        "attachments": [],
        "agent_type": "codex",
        "task_mode": "reviewed",
        "execution_complexity": "auto",
        "origin": "human",
        "source": source,
        "execution_control": execution_control,
        "execution_ref": None,
        "execution_epoch": 1,
        "progress_revision": 0,
        "execution_released": False,
        "latest_progress": None,
        "runtime_observation": None,
        "status": "todo",
        "session_id": None,
        "related_task_id": None,
        "clear_context": None,
        "dispatch_pending": False,
        "review_attempts": 0,
        "created_at": NOW,
        "updated_at": NOW,
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
        or parsed_base.path != "/tests/agent_workspace_task_create_harness.html"
    ):
        raise SystemExit("Use an owned numeric-loopback Vite server and the test harness path")

    output = Path(output_arg)
    output.mkdir(parents=True, exist_ok=True)
    page_errors: list[str] = []
    console_errors: list[str] = []
    blocked_requests: list[tuple[str, str]] = []
    unknown_requests: list[tuple[str, str]] = []
    create_calls: list[dict] = []
    created_by_key: dict[tuple[str, str], dict] = {}
    board_tasks = {
        "ws-1": [
            task_fixture(
                "record-1",
                "ws-1",
                "Record-only fixture",
                execution_control="initiator",
                source={"kind": "human", "tab_id": None, "agent_id": None},
            )
        ],
        "ws-2": [],
    }
    fail_next_board: set[str] = set()
    capability_modes = {"ws-1": "supported", "ws-2": "supported"}
    capability_seen = {"ws-1": asyncio.Event(), "ws-2": asyncio.Event()}
    capability_release = {"ws-1": asyncio.Event(), "ws-2": asyncio.Event()}
    deferred_create_seen = asyncio.Event()
    deferred_create_release = asyncio.Event()
    create_mode = "success"
    next_task_id = 1

    def board(workspace_id: str) -> dict:
        workspace = next(item for item in WORKSPACES if item["id"] == workspace_id)
        return {
            "workspace": workspace,
            "tasks": board_tasks[workspace_id],
            "sessions": [],
            "reports": [],
            "snapshot_path": None,
            "tasks_pagination": {
                "next_cursor": None,
                "has_more": False,
                "status_counts": {},
            },
        }

    def task_from_create(workspace_id: str, body: dict) -> dict:
        nonlocal next_task_id
        task = task_fixture(
            f"created-{next_task_id}",
            workspace_id,
            body["title"],
            execution_control=body.get("execution_control", "workspace"),
            source=body.get("source"),
        )
        next_task_id += 1
        task.update(
            prompt=body["prompt"],
            agent_type=body.get("agent_type", "codex"),
            task_mode=body.get("task_mode", "reviewed"),
            execution_complexity=body.get("execution_complexity", "auto"),
            execution_ref=body.get("execution_ref"),
            session_id=body.get("session_id"),
            related_task_id=body.get("related_task_id"),
            clear_context=body.get("clear_context"),
        )
        return task

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=executable,
        )
        try:
            context = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                service_workers="block",
            )
            await context.add_init_script("""
                localStorage.setItem('claude_hub_active_workspace_id', 'ws-1')
                window.__failTaskReporterStorage = false
                window.__fetchAudit = []
                window.__fetchJsonCompleted = []
                const originalFetch = window.fetch.bind(window)
                window.fetch = async function(input, init = {}) {
                  const url = typeof input === 'string' ? input : input.url
                  const method = init.method || 'GET'
                  window.__fetchAudit.push({ method, url })
                  const response = await originalFetch(input, init)
                  const originalJson = response.json.bind(response)
                  Object.defineProperty(response, 'json', {
                    configurable: true,
                    value: async function() {
                      try {
                        return await originalJson()
                      } finally {
                        window.__fetchJsonCompleted.push({ method, url, status: response.status })
                      }
                    },
                  })
                  return response
                }
                const originalSetItem = Storage.prototype.setItem
                Storage.prototype.setItem = function(key, value) {
                  if (window.__failTaskReporterStorage &&
                      String(key).includes(':task-reporter:')) {
                    throw new DOMException('fixture quota', 'QuotaExceededError')
                  }
                  return originalSetItem.call(this, key, value)
                }
                let intervalId = 100000
                window.__disabledIntervals = []
                window.setInterval = function(_callback, milliseconds) {
                  window.__disabledIntervals.push(milliseconds)
                  intervalId += 1
                  return intervalId
                }
                window.clearInterval = function(_id) {}
                """)

            async def block_websocket(socket) -> None:
                await socket.close(code=1000, reason="mocked Agent Workspace smoke")

            async def route_all(route) -> None:
                nonlocal create_mode
                request = route.request
                parsed = urlparse(request.url)
                origin = (parsed.scheme, parsed.hostname, parsed.port)
                expected_origin = (
                    parsed_base.scheme,
                    parsed_base.hostname,
                    parsed_base.port,
                )
                if origin != expected_origin:
                    blocked_requests.append((request.method, request.url))
                    await route.abort("blockedbyclient")
                    return

                method = request.method
                path = parsed.path
                if not path.startswith("/api/"):
                    if method in {"GET", "HEAD"}:
                        await route.continue_()
                    else:
                        unknown_requests.append((method, path))
                        await route.fulfill(status=405, body="blocked static method")
                    return

                body = None
                if method == "GET" and path == "/api/env-presets" and not parsed.query:
                    body = {"custom_presets": [], "hidden_builtin_ids": []}
                elif method == "GET" and path == "/api/remote/profiles" and not parsed.query:
                    body = []
                elif method == "GET" and path == "/api/workspaces" and not parsed.query:
                    body = WORKSPACES
                elif method == "GET" and path == "/api/tabs/status" and not parsed.query:
                    body = []
                else:
                    capabilities_match = re.fullmatch(
                        r"/api/workspaces/(ws-1|ws-2)/task-capabilities",
                        path,
                    )
                    board_match = re.fullmatch(
                        r"/api/workspaces/(ws-1|ws-2)/board",
                        path,
                    )
                    lessons_match = re.fullmatch(
                        r"/api/workspaces/(ws-1|ws-2)/lessons",
                        path,
                    )
                    reports_match = re.fullmatch(
                        r"/api/workspaces/(ws-1|ws-2)/tasks/([A-Za-z0-9._-]+)/reports",
                        path,
                    )
                    create_match = re.fullmatch(
                        r"/api/workspaces/(ws-1|ws-2)/tasks",
                        path,
                    )
                    if method == "GET" and capabilities_match and not parsed.query:
                        workspace_id = capabilities_match.group(1)
                        mode = capability_modes[workspace_id]
                        if mode == "deferred":
                            capability_seen[workspace_id].set()
                            await capability_release[workspace_id].wait()
                            mode = capability_modes[workspace_id]
                        if mode == "error":
                            await route.fulfill(
                                status=500,
                                content_type="application/json",
                                body=json.dumps({"detail": "fixture capabilities failure"}),
                            )
                            return
                        if mode == "404":
                            await route.fulfill(status=404, body="")
                            return
                        if mode != "supported":
                            raise AssertionError(f"unknown capability mode: {mode}")
                        body = CAPABILITIES
                    elif (
                        method == "GET"
                        and board_match
                        and re.fullmatch(r"tasks_limit=\d+", parsed.query)
                    ):
                        workspace_id = board_match.group(1)
                        if workspace_id in fail_next_board:
                            fail_next_board.remove(workspace_id)
                            await route.fulfill(
                                status=500,
                                content_type="application/json",
                                body=json.dumps({"detail": "fixture board failure"}),
                            )
                            return
                        body = board(workspace_id)
                    elif method == "GET" and lessons_match and parsed.query == "limit=50":
                        body = []
                    elif method == "GET" and reports_match and not parsed.query:
                        workspace_id, task_id = reports_match.groups()
                        if task_id not in {task["id"] for task in board_tasks[workspace_id]}:
                            unknown_requests.append((method, path))
                            await route.fulfill(status=501, body="unmocked task report")
                            return
                        body = []
                    elif method == "POST" and create_match and not parsed.query:
                        workspace_id = create_match.group(1)
                        raw_body = request.post_data or "{}"
                        submitted = json.loads(raw_body)
                        call = {
                            "workspace_id": workspace_id,
                            "body": submitted,
                            "raw_body": raw_body,
                        }
                        create_calls.append(call)
                        request_key = submitted.get("request_key")
                        storage_key = (
                            (workspace_id, request_key)
                            if isinstance(request_key, str) and request_key
                            else None
                        )
                        created = created_by_key.get(storage_key) if storage_key else None
                        replayed = created is not None
                        if created is None:
                            created = task_from_create(workspace_id, submitted)
                            if storage_key:
                                created_by_key[storage_key] = created
                            board_tasks[workspace_id].append(created)

                        mode = create_mode
                        if mode == "deferred":
                            deferred_create_seen.set()
                            await deferred_create_release.wait()
                        matching_calls = [
                            item
                            for item in create_calls
                            if item["workspace_id"] == workspace_id
                            and item["body"].get("request_key") == request_key
                        ]
                        if mode == "malformed_once" and len(matching_calls) == 1:
                            await route.fulfill(
                                status=201,
                                content_type="application/json",
                                body="{}",
                            )
                            return
                        if mode == "success_board_failure":
                            fail_next_board.add(workspace_id)
                        if mode not in {
                            "success",
                            "success_board_failure",
                            "malformed_once",
                            "deferred",
                        }:
                            raise AssertionError(f"unknown create mode: {mode}")
                        await route.fulfill(
                            status=200 if replayed else 201,
                            headers={"X-Task-Replayed": "true" if replayed else "false"},
                            content_type="application/json",
                            body=json.dumps(created),
                        )
                        return
                    else:
                        unknown_requests.append((method, path))
                        await route.fulfill(
                            status=501,
                            content_type="application/json",
                            body=json.dumps({"detail": f"unmocked API: {method} {path}"}),
                        )
                        return

                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(body),
                )

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
            await page.goto(url)
            workspace_select = page.locator("select.workspace-select")
            await expect(workspace_select).to_have_value("ws-1")
            await expect(page.get_by_role("button", name="Add Task", exact=True)).to_be_enabled()

            async def open_add_task():
                await page.get_by_role("button", name="Add Task", exact=True).click()
                modal = page.locator(".workspace-modal").filter(
                    has=page.get_by_role("heading", name="Add Task", exact=True)
                )
                await expect(modal).to_be_visible()
                return modal

            async def fill_task(modal, title: str, *, initiator: bool = False) -> None:
                if initiator:
                    await modal.get_by_role(
                        "button", name="Initiator reports progress", exact=True
                    ).click()
                await modal.get_by_label("Title", exact=True).fill(title)
                await modal.get_by_label("Task description", exact=True).fill(
                    f"Description for {title}"
                )

            async def switch_workspace(workspace_id: str) -> None:
                await workspace_select.select_option(workspace_id)
                await expect(workspace_select).to_have_value(workspace_id)
                expected_name = "Workspace One" if workspace_id == "ws-1" else "Workspace Two"
                await expect(page.locator(".workspace-mobile-identity strong")).to_have_text(
                    expected_name
                )

            async def browser_create_count() -> int:
                return await page.evaluate("""
                    () => window.__fetchAudit.filter(item =>
                      item.method === 'POST' &&
                      /^\\/api\\/workspaces\\/ws-[12]\\/tasks$/.test(item.url)
                    ).length
                    """)

            async def request_submit_without_post(modal) -> None:
                before = await browser_create_count()
                await modal.locator("form").evaluate("form => form.requestSubmit()")
                assert await browser_create_count() == before

            # Record-only cards keep their source/progress UI but expose no
            # Workspace-managed dispatch/edit/abort/delete controls.
            record_card = page.locator(".task-card").filter(has_text="Record-only fixture")
            await expect(record_card).to_be_visible()
            await expect(record_card.locator(".advanced-start")).to_have_count(0)
            await expect(record_card.locator(".task-actions")).to_have_count(0)
            await record_card.click()
            record_detail = page.get_by_role("dialog", name="Record-only fixture")
            await expect(record_detail).to_be_visible()
            await expect(record_detail.locator(".task-execution-panel")).to_be_visible()
            await expect(record_detail.locator(".detail-footer")).to_have_count(0)
            await record_detail.get_by_role("button", name="Close task detail").click()

            # A confirmed create is not retried merely because its board refresh
            # fails. The modal closes and the fixed warning remains visible.
            create_mode = "success_board_failure"
            modal = await open_add_task()
            await fill_task(modal, "Created despite refresh failure")
            before = len(create_calls)
            await modal.get_by_role("button", name="Create Task", exact=True).click()
            await expect(page.get_by_role("heading", name="Add Task")).to_have_count(0)
            await expect(
                page.get_by_text(
                    "The Task was created, but the board could not be refreshed.",
                    exact=True,
                )
            ).to_be_visible()
            assert len(create_calls) == before + 1

            # A malformed success response leaves the modern request frozen.
            # Explicit recovery must send the byte-identical body and call key.
            create_mode = "malformed_once"
            modal = await open_add_task()
            await fill_task(modal, "Recover exact create", initiator=True)
            before = len(create_calls)
            await modal.get_by_role("button", name="Create Task", exact=True).click()
            recover_create = modal.get_by_role(
                "button", name="Recover saved create request", exact=True
            )
            await expect(recover_create).to_be_visible()
            first = create_calls[-1]
            assert first["body"]["execution_control"] == "initiator"
            assert first["body"]["source"] == {"kind": "human", "tab_id": None, "agent_id": None}
            assert len(first["body"]["request_key"]) > 0
            assert len(first["body"]["reporter_key"]) >= 32
            await recover_create.click()
            await expect(page.get_by_role("heading", name="Add Task")).to_have_count(0)
            assert len(create_calls) == before + 2
            assert create_calls[-1]["body"] == first["body"]
            assert create_calls[-1]["raw_body"] == first["raw_body"]

            # The Task receipt is final even when only local reporter-key storage
            # fails. Retrying storage never calls POST /tasks again.
            create_mode = "success"
            await page.evaluate("window.__failTaskReporterStorage = true")
            modal = await open_add_task()
            await fill_task(modal, "Credential local recovery", initiator=True)
            before = len(create_calls)
            await modal.get_by_role("button", name="Create Task", exact=True).click()
            await expect(modal.get_by_text("Task created.", exact=True)).to_be_visible()
            retry_storage = modal.get_by_role("button", name="Retry local storage", exact=True)
            await expect(retry_storage).to_be_visible()
            assert len(create_calls) == before + 1
            await page.evaluate("window.__failTaskReporterStorage = false")
            await retry_storage.click()
            await expect(page.get_by_role("heading", name="Add Task")).to_have_count(0)
            assert len(create_calls) == before + 1

            # A late Workspace-A receipt must not close or clear a newer B draft.
            create_mode = "deferred"
            modal_a = await open_add_task()
            await fill_task(modal_a, "Slow Workspace A", initiator=True)
            before = len(create_calls)
            await modal_a.get_by_role("button", name="Create Task", exact=True).click()
            await asyncio.wait_for(deferred_create_seen.wait(), timeout=5)
            assert len(create_calls) == before + 1
            assert await page.evaluate("""
                () => Object.keys(sessionStorage).some(key =>
                  key.includes('task-create') && key.includes('ws-1'))
                """)
            await modal_a.get_by_role("button", name="Cancel", exact=True).click()
            await switch_workspace("ws-2")
            create_mode = "success"
            modal_b = await open_add_task()
            await fill_task(modal_b, "Workspace B draft")
            old_board_json_count = await page.evaluate("""
                () => window.__fetchJsonCompleted.filter(item =>
                  item.method === 'GET' &&
                  item.url.startsWith('/api/workspaces/ws-1/board?')
                ).length
                """)
            deferred_create_release.set()
            await page.wait_for_function(
                """
                previous => window.__fetchJsonCompleted.filter(item =>
                  item.method === 'GET' &&
                  item.url.startsWith('/api/workspaces/ws-1/board?')
                ).length > previous
                """,
                arg=old_board_json_count,
            )
            await page.evaluate("""
                () => new Promise(resolve => {
                  requestAnimationFrame(() => queueMicrotask(resolve))
                })
                """)
            await page.wait_for_function("""
                () => !Object.keys(sessionStorage).some(key =>
                  key.includes('task-create') && key.includes('ws-1'))
                """)
            await expect(page.get_by_role("heading", name="Add Task")).to_be_visible()
            await expect(modal_b.get_by_label("Title", exact=True)).to_have_value(
                "Workspace B draft"
            )
            await expect(modal_b.get_by_label("Task description", exact=True)).to_have_value(
                "Description for Workspace B draft"
            )
            assert create_calls[before]["workspace_id"] == "ws-1"
            keys = await page.evaluate("Object.keys(sessionStorage)")
            assert not any("ws-2" in key and "task-reporter" in key for key in keys)
            await modal_b.get_by_role("button", name="Cancel", exact=True).click()

            # While capabilities are loading, even requestSubmit() reaches no
            # create fetch. Release is proven by the loading UI disappearing.
            capability_modes["ws-1"] = "deferred"
            await switch_workspace("ws-1")
            await asyncio.wait_for(capability_seen["ws-1"].wait(), timeout=5)
            modal = await open_add_task()
            await expect(
                modal.get_by_text("Loading Task execution options…", exact=True)
            ).to_be_visible()
            await fill_task(modal, "Blocked while capabilities load")
            await request_submit_without_post(modal)
            capability_modes["ws-1"] = "supported"
            capability_release["ws-1"].set()
            await expect(
                modal.get_by_text("Loading Task execution options…", exact=True)
            ).to_have_count(0)
            await expect(
                modal.get_by_role("button", name="Initiator reports progress", exact=True)
            ).to_be_enabled()
            await modal.get_by_role("button", name="Cancel", exact=True).click()

            # Capability errors also block direct requestSubmit and never fall
            # through to the old create shape.
            capability_modes["ws-2"] = "error"
            await switch_workspace("ws-2")
            modal = await open_add_task()
            await expect(modal.get_by_role("alert")).to_contain_text(
                "Task execution options could not be loaded"
            )
            await fill_task(modal, "Blocked after capability error")
            await request_submit_without_post(modal)
            await modal.get_by_role("button", name="Cancel", exact=True).click()

            # Only a real 404 enables the compatibility form. Its POST must omit
            # all modern idempotency/reporter/source fields.
            capability_modes["ws-1"] = "404"
            await switch_workspace("ws-1")
            modal = await open_add_task()
            await expect(
                modal.get_by_text(
                    "This Hub supports the legacy Workspace-managed Task form only.",
                    exact=True,
                )
            ).to_be_visible()
            await expect(
                modal.get_by_role("button", name="Initiator reports progress", exact=True)
            ).to_be_disabled()
            await fill_task(modal, "Legacy compatibility create")
            create_mode = "success"
            before = len(create_calls)
            await modal.get_by_role("button", name="Create Task", exact=True).click()
            await expect(page.get_by_role("heading", name="Add Task")).to_have_count(0)
            assert len(create_calls) == before + 1
            legacy_body = create_calls[-1]["body"]
            for field in ("request_key", "execution_control", "source", "reporter_key"):
                assert field not in legacy_body

            # A saved modern request never degrades into the legacy shape after
            # the same workspace later reports 404.
            capability_modes["ws-2"] = "supported"
            await switch_workspace("ws-2")
            create_mode = "malformed_once"
            modal = await open_add_task()
            await fill_task(modal, "Modern pending cannot downgrade", initiator=True)
            before = len(create_calls)
            await modal.get_by_role("button", name="Create Task", exact=True).click()
            await expect(
                modal.get_by_role("button", name="Recover saved create request", exact=True)
            ).to_be_visible()
            assert len(create_calls) == before + 1
            await modal.get_by_role("button", name="Cancel", exact=True).click()

            capability_modes["ws-1"] = "supported"
            await switch_workspace("ws-1")
            capability_modes["ws-2"] = "404"
            await switch_workspace("ws-2")
            modal = await open_add_task()
            await expect(modal.get_by_role("alert")).to_contain_text(
                "saved request uses the newer Task contract"
            )
            recover = modal.get_by_role("button", name="Recover saved create request", exact=True)
            await expect(recover).to_be_disabled()
            await request_submit_without_post(modal)
            assert len(create_calls) == before + 1
            await modal.get_by_role(
                "button", name="discard it and edit a new Task", exact=True
            ).click()
            await modal.get_by_role("button", name="Cancel", exact=True).click()

            await page.screenshot(
                path=str(output / "agent-workspace-task-create.png"),
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
        "create_requests": len(create_calls),
        "blocked_requests": blocked_requests,
        "unknown_requests": unknown_requests,
        "page_errors": page_errors,
        "console_errors": console_errors,
        "boundary": "Real AgentWorkspaceView/Pinia; every API and WebSocket mocked.",
    }
    (output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


asyncio.run(main())
