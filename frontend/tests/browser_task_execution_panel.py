"""Real TaskExecutionPanel DOM smoke with a fully mocked HTTP boundary.

Usage:
  python tests/browser_task_execution_panel.py \
    http://127.0.0.1:5291/tests/task_execution_panel_harness.html \
    /tmp/task-execution-panel [chromium]
"""

import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, expect


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
        or parsed_base.path != "/tests/task_execution_panel_harness.html"
    ):
        raise SystemExit("Use an owned numeric-loopback Vite server and the test harness path")

    output = Path(output_arg)
    output.mkdir(parents=True, exist_ok=True)
    api_requests: list[dict] = []
    unexpected_mutations: list[tuple[str, str]] = []
    unknown_requests: list[tuple[str, str]] = []
    page_errors: list[str] = []
    blocked_requests: list[tuple[str, str]] = []
    refresh_requests: list[tuple[str, str]] = []
    task_sources = {
        "task-a": {"kind": "agent", "tab_id": None, "agent_id": "agent-a"},
        "task-b": {"kind": "chat", "tab_id": "tab-b", "agent_id": None},
        "task-workspace": {"kind": "human", "tab_id": None, "agent_id": None},
    }
    mutation_plans: dict[str, list[str]] = {"progress": [], "handoff": []}

    def plan(action: str, *outcomes: str) -> None:
        mutation_plans[action].extend(outcomes)

    def mutation_requests(action: str) -> list[dict]:
        return [request for request in api_requests if request.get("action") == action]

    def mutation_response(action: str, workspace_id: str, task_id: str, body: dict) -> dict:
        execution_control = (
            body["execution_control"]
            if action == "handoff"
            else ("workspace" if task_id == "task-workspace" else "initiator")
        )
        execution_epoch = body["expected_execution_epoch"] + (1 if action == "handoff" else 0)
        progress_revision = body["expected_progress_revision"] + 1
        return {
            "task": {
                "id": task_id,
                "workspace_id": workspace_id,
                "source": task_sources[task_id],
                "execution_epoch": execution_epoch,
                "progress_revision": progress_revision,
                "execution_control": execution_control,
            },
            "event": {
                "call_id": f"task-execution:{task_id}:{body['call_id']}",
                "task_id": task_id,
            },
            "replayed": False,
        }

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=executable,
        )
        try:
            context = await browser.new_context(
                viewport={"width": 1100, "height": 900},
                service_workers="block",
            )
            await context.add_init_script("""
                (() => {
                  const original = Storage.prototype.setItem
                  Storage.prototype.setItem = function(key, value) {
                    if (globalThis.__FAIL_TASK_STORAGE__ &&
                        String(key).startsWith('claude-hub:task-')) {
                      throw new DOMException('fixture storage failure', 'QuotaExceededError')
                    }
                    return original.call(this, key, value)
                  }
                })()
                """)

            async def block_websocket(socket) -> None:
                await socket.close(code=1000, reason="mocked Task panel smoke")

            async def api(route) -> None:
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

                method = request.method
                path = parsed.path
                if not path.startswith("/api/"):
                    if method in {"GET", "HEAD"}:
                        await route.continue_()
                    else:
                        unknown_requests.append((method, path))
                        await route.fulfill(
                            status=405,
                            content_type="application/json",
                            body=json.dumps({"detail": f"blocked static method: {method} {path}"}),
                        )
                    return

                progress_match = re.fullmatch(
                    r"/api/workspaces/ws-a/tasks/(task-a|task-b|task-workspace)/progress/manual",
                    path,
                )
                handoff_match = re.fullmatch(
                    r"/api/workspaces/ws-a/tasks/(task-a|task-b|task-workspace)/handoff",
                    path,
                )
                if method == "POST" and progress_match:
                    action = "progress"
                    task_id = progress_match.group(1)
                elif method == "POST" and handoff_match:
                    action = "handoff"
                    task_id = handoff_match.group(1)
                else:
                    action = ""

                if action:
                    workspace_id = "ws-a"
                    body = json.loads(request.post_data or "{}")
                    api_requests.append(
                        {"method": method, "path": path, "action": action, "body": body}
                    )

                    if not mutation_plans[action]:
                        unexpected_mutations.append((method, path))
                        await route.fulfill(
                            status=500,
                            content_type="application/json",
                            body=json.dumps({"detail": "unexpected mutation"}),
                        )
                        return
                    outcome = mutation_plans[action].pop(0)
                    if outcome == "409":
                        await route.fulfill(
                            status=409,
                            content_type="application/json",
                            body=json.dumps({"detail": "secret server conflict detail"}),
                        )
                        return
                    if outcome == "malformed":
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps(
                                {"task": {"id": "wrong-task"}, "event": {}, "replayed": False}
                            ),
                        )
                        return
                    if outcome != "ok":
                        raise AssertionError(f"unknown planned outcome: {outcome}")
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(mutation_response(action, workspace_id, task_id, body)),
                    )
                    return

                if method == "GET" and path == "/api/workspaces/ws-a/board":
                    refresh_requests.append((method, path))
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "workspace": {"id": "ws-a", "name": "Fixture workspace"},
                                "tasks": [],
                                "sessions": [],
                                "reports": [],
                                "tasks_pagination": None,
                            }
                        ),
                    )
                    return
                if method == "GET" and path == "/api/workspaces/ws-a/lessons":
                    refresh_requests.append((method, path))
                    await route.fulfill(status=200, content_type="application/json", body="[]")
                    return
                unknown_requests.append((method, path))
                await route.fulfill(
                    status=501,
                    content_type="application/json",
                    body=json.dumps({"detail": f"unmocked API: {method} {path}"}),
                )

            await context.route_web_socket("**/*", block_websocket)
            await context.route("**/*", api)
            page = await context.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            await page.goto(url)
            panel = page.get_by_role("region", name="Task execution and progress")
            await expect(panel).to_be_visible()

            async def switch_task(task_id: str, source_label: str) -> None:
                await page.evaluate(
                    "taskId => window.__TASK_EXECUTION_TEST__.switchTask(taskId)",
                    task_id,
                )
                await expect(panel).to_contain_text(source_label)

            async def remount() -> None:
                await page.evaluate("window.__TASK_EXECUTION_TEST__.remount()")
                await expect(panel).to_be_visible()

            async def assert_no_automatic_mutation(previous_count: int) -> None:
                await page.wait_for_timeout(100)
                assert len(api_requests) == previous_count

            # Progress: a 409 stages one exact request. Task switches and a real
            # unmount/remount restore it, but never send it without a click.
            summary = panel.get_by_label("Summary", exact=True)
            await summary.fill("checkpoint one")
            plan("progress", "409")
            await panel.get_by_role("button", name="Save progress", exact=True).click()
            await expect(panel.get_by_role("alert")).to_contain_text("Progress was not confirmed")
            await expect(
                panel.get_by_role("button", name="Confirm prior update", exact=True)
            ).to_be_visible()
            assert "secret server conflict detail" not in await panel.inner_text()
            progress_body = mutation_requests("progress")[0]["body"]

            before = len(api_requests)
            await switch_task("task-b", "From Chat")
            await expect(
                panel.get_by_role("button", name="Confirm prior update", exact=True)
            ).to_have_count(0)
            await switch_task("task-a", "From Agent")
            await expect(
                panel.get_by_role("button", name="Confirm prior update", exact=True)
            ).to_be_visible()
            await assert_no_automatic_mutation(before)

            await remount()
            await expect(
                panel.get_by_role("button", name="Confirm prior update", exact=True)
            ).to_be_visible()
            await assert_no_automatic_mutation(before)

            plan("progress", "malformed")
            await panel.get_by_role("button", name="Confirm prior update", exact=True).click()
            await expect(panel.get_by_role("alert")).to_contain_text(
                "prior progress update could not be confirmed"
            )
            assert mutation_requests("progress")[-1]["body"] == progress_body

            before = len(api_requests)
            await remount()
            await expect(
                panel.get_by_role("button", name="Confirm prior update", exact=True)
            ).to_be_visible()
            await assert_no_automatic_mutation(before)

            plan("progress", "ok")
            await panel.get_by_role("button", name="Confirm prior update", exact=True).click()
            await expect(panel.get_by_role("status")).to_contain_text("Progress saved")
            assert [entry["body"] for entry in mutation_requests("progress")] == [
                progress_body,
                progress_body,
                progress_body,
            ]
            await remount()
            await expect(
                panel.get_by_role("button", name="Confirm prior update", exact=True)
            ).to_have_count(0)

            # Handoff follows the same 409 -> malformed -> accepted sequence.
            # The saved attempt changes to confirmation wording after recovery,
            # while its body and call_id remain byte-for-byte stable.
            await switch_task("task-workspace", "Created manually")
            plan("handoff", "409")
            await panel.get_by_role("button", name="Hand off to initiator", exact=True).click()
            await expect(panel.get_by_role("alert")).to_contain_text("handoff was not confirmed")
            handoff_body = mutation_requests("handoff")[0]["body"]

            before = len(api_requests)
            await switch_task("task-b", "From Chat")
            await expect(
                panel.get_by_role("button", name="Confirm prior handoff", exact=True)
            ).to_have_count(0)
            await switch_task("task-workspace", "Created manually")
            await expect(
                panel.get_by_role("button", name="Confirm prior handoff", exact=True)
            ).to_be_visible()
            await assert_no_automatic_mutation(before)

            await remount()
            await expect(
                panel.get_by_role("button", name="Confirm prior handoff", exact=True)
            ).to_be_visible()
            await assert_no_automatic_mutation(before)

            plan("handoff", "malformed")
            await panel.get_by_role("button", name="Confirm prior handoff", exact=True).click()
            await expect(panel.get_by_role("alert")).to_contain_text(
                "prior handoff could not be confirmed"
            )
            assert mutation_requests("handoff")[-1]["body"] == handoff_body

            before = len(api_requests)
            await remount()
            await expect(
                panel.get_by_role("button", name="Confirm prior handoff", exact=True)
            ).to_be_visible()
            await assert_no_automatic_mutation(before)

            plan("handoff", "ok")
            await panel.get_by_role("button", name="Confirm prior handoff", exact=True).click()
            await expect(panel.get_by_role("status")).to_contain_text(
                "Execution responsibility updated"
            )
            assert [entry["body"] for entry in mutation_requests("handoff")] == [
                handoff_body,
                handoff_body,
                handoff_body,
            ]
            await expect(panel).to_contain_text("External reporter credential · epoch 5")
            await page.evaluate(
                "window.__TASK_EXECUTION_TEST__.patchTask({execution_control:'initiator',execution_epoch:5,progress_revision:2})"
            )
            await remount()
            await expect(panel).to_contain_text("External reporter credential · epoch 5")
            await expect(
                panel.get_by_role("button", name="Confirm prior handoff", exact=True)
            ).to_have_count(0)

            # Persistence failure must stop before fetch. Exercise both request
            # kinds on distinct Tasks so no prior volatile attempt can interfere.
            await page.evaluate("globalThis.__FAIL_TASK_STORAGE__ = true")
            await switch_task("task-b", "From Chat")
            await panel.get_by_label("Summary", exact=True).fill("must not send")
            before = len(api_requests)
            await panel.get_by_role("button", name="Save progress", exact=True).click()
            await expect(
                panel.get_by_role("alert").filter(has_text="No request was sent")
            ).to_be_visible()
            assert len(api_requests) == before

            await switch_task("task-a", "From Agent")
            before = len(api_requests)
            await panel.get_by_role("button", name="Hand off to Workspace", exact=True).click()
            await expect(
                panel.get_by_role("alert").filter(has_text="No request was sent")
            ).to_be_visible()
            assert len(api_requests) == before
            await page.evaluate("globalThis.__FAIL_TASK_STORAGE__ = false")

            await page.screenshot(path=str(output / "task-execution-panel.png"), full_page=True)
            assert not mutation_plans["progress"], mutation_plans
            assert not mutation_plans["handoff"], mutation_plans
            assert not unexpected_mutations, unexpected_mutations
            assert not unknown_requests, unknown_requests
            assert not page_errors, page_errors
            assert refresh_requests == [
                ("GET", "/api/workspaces/ws-a/board"),
                ("GET", "/api/workspaces/ws-a/lessons"),
                ("GET", "/api/workspaces/ws-a/board"),
                ("GET", "/api/workspaces/ws-a/lessons"),
            ]
            assert not blocked_requests, blocked_requests
            await context.close()
        finally:
            await browser.close()

    report = {
        "result": "PASS",
        "progress_requests": len(mutation_requests("progress")),
        "handoff_requests": len(mutation_requests("handoff")),
        "unexpected_mutations": unexpected_mutations,
        "unknown_requests": unknown_requests,
        "page_errors": page_errors,
        "blocked_requests": blocked_requests,
        "refresh_requests": refresh_requests,
        "boundary": "Real Vue/Pinia panel; all API and WebSocket traffic mocked.",
    }
    (output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


asyncio.run(main())
