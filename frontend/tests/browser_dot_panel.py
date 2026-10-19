"""Exercise the Dot launcher and panel on a phone, then on desktop, in the real SPA.

Run only with a task-owned Vite server, never 5173/8173:
  python tests/browser_dot_panel.py <owned-loopback-url> <new-output-dir> <browser>

All APIs are mocked. No Hub backend or provider is contacted.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_lifecycle import BrowserRun, run_browser_main  # noqa: E402

TAB = "dot-tab"
NOW = "2026-10-15T08:00:00Z"
REVISION_1 = "a" * 64
REVISION_2 = "b" * 64


def origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlparse(url)
    return parsed.scheme, parsed.hostname, parsed.port


async def main(browser_run: BrowserRun) -> None:
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    base = sys.argv[1].rstrip("/")
    parsed_base = urlparse(base)
    if (
        parsed_base.scheme != "http"
        or parsed_base.hostname not in {"127.0.0.1", "::1"}
        or parsed_base.port in {None, 5173, 8173, 10025, 18183}
        or parsed_base.username is not None
        or parsed_base.password is not None
        or parsed_base.path not in {"", "/"}
        or parsed_base.query
        or parsed_base.fragment
    ):
        raise SystemExit("Use an owned numeric-loopback review server")
    output = Path(sys.argv[2]).resolve()

    identity = {"name": "Dot", "avatar": "", "avatar_image": None}
    tab = {
        "id": TAB,
        "name": "Dot",
        "cwd": "/fixture/dot",
        "agent_type": "claude",
        "session_kind": "chat",
        "port": 0,
        "is_active": True,
        "created_at": NOW,
    }
    execution = {
        "tab_id": TAB,
        "agent_type": "legacy-provider",
        "env": {
            "LEGACY_PROVIDER_TOKEN": "old-provider-secret",
            "LEGACY_PROVIDER_MODEL": "legacy-old",
        },
        "revision": REVISION_1,
        "supported_agent_types": ["claude", "codex", "cursor", "traex", "maso"],
    }
    execution_put_attempts = 0
    fail_next_tabs_refresh = False
    agent_status = "working"
    work: list[dict] = []
    inbox = [
        {
            "id": "e-1",
            "kind": "task_stalled",
            "text": "Train run — no progress for 2.5h (status working)",
            "at": NOW,
        }
    ]
    lessons = [
        {
            "id": "l-1",
            "rule": "Paper tasks: name the paper version and the output path",
            "votes": 2,
            "tasks": ["task-1", "task-2"],
            "created": NOW,
            "seen": NOW,
            "score": 2.0,
            "in_session": True,
            "reach": "always",
        }
    ]
    review_stats = {
        "reviewed": 4,
        "first_pass": 3,
        "rate": 0.75,
        "causes": {"brief": 1},
        "pending": 1,
        "judged": 4,
        "returned": 5,
        "window": 20,
    }
    writes: list[list] = []
    report = {
        "result": "FAIL",
        "api_requests": [],
        "blocked_external_requests": [],
        "blocked_websockets": [],
        "unknown_requests": [],
        "page_errors": [],
        "writes": writes,
        "screenshots": [],
        "boundary": "Real SPA rendering with mocked APIs; no backend or provider traffic.",
    }
    failure: BaseException | None = None
    page = None

    def status() -> dict:
        return {
            "set_up": True,
            "identity": identity,
            "home": "/fixture/dot",
            "tab_id": tab["id"],
            "tab_exists": True,
            "session": {
                "conversation_id": None,
                "turns": 1,
                "started_at": NOW,
                "max_turns": 24,
            },
            "cards": {"knowledge": 2},
            "focus": 0,
            "open_work": len(work),
            "unread_events": len(inbox),
            "pending_reviews": 1,
            "last_tidy_at": None,
        }

    async with browser_run.context(
        viewport={"width": 390, "height": 844},
        launch_args=["--disable-background-networking", "--disable-component-update"],
    ) as context:
        try:

            async def block_websocket(socket) -> None:
                report["blocked_websockets"].append(socket.url)
                await socket.close(code=1000, reason="mocked dot smoke")

            async def route_all(route) -> None:
                nonlocal execution_put_attempts, fail_next_tabs_refresh
                request = route.request
                parsed = urlparse(request.url)
                if origin(request.url) != origin(base):
                    report["blocked_external_requests"].append(request.url)
                    await route.abort("blockedbyclient")
                    return
                path = parsed.path
                if not path.startswith("/api/"):
                    await route.continue_()
                    return
                method = request.method
                report["api_requests"].append([method, path])
                data = json.loads(request.post_data or "null")
                if method != "GET":
                    writes.append([method, path, data])

                code = 200
                body: object
                if path == "/api/auth/check":
                    body = {"auth_required": False, "user": None}
                elif path == "/api/tabs":
                    if fail_next_tabs_refresh:
                        fail_next_tabs_refresh = False
                        code, body = 503, {"detail": "temporary tab refresh failure"}
                    else:
                        body = [tab]
                elif path == "/api/tabs/status":
                    body = [
                        {
                            "tab_id": tab["id"],
                            "tab_name": tab["name"],
                            "agent_type": tab["agent_type"],
                            "status": agent_status,
                            "status_text": agent_status.title(),
                            "tmux_session": "fixture-dot-session",
                            "sampled_at": NOW,
                        }
                    ]
                elif path == "/api/tabs/archived":
                    body = []
                elif path.startswith("/api/tabs/sidebar-preferences"):
                    body = {"pinned_tab_ids": []}
                elif path == "/api/env-presets":
                    body = {"custom_presets": [], "hidden_builtin_ids": []}
                elif path == "/api/feishu/bot/bots":
                    body = {
                        "pool_revision": 0,
                        "bots": [],
                        "pool_editable": True,
                        "deprecated_env": [],
                        "focus_bot_id": None,
                    }
                elif path.endswith("/capabilities"):
                    body = {
                        "structured": True,
                        "send_message": True,
                        "supports_goals": False,
                        "supports_images": True,
                        "agent_type": "claude",
                        "supports_mode_switch": False,
                    }
                elif path.endswith("/events") or path.endswith("/wait"):
                    if path.endswith("/wait"):
                        await asyncio.sleep(0.2)
                    body = {"events": [], "next_sequence": -1, "has_more": False}
                elif path.endswith("/live"):
                    await route.fulfill(
                        status=200, content_type="text/event-stream", body=": mock\n\n"
                    )
                    return
                elif path.endswith("/goal/current"):
                    body = None
                elif path.endswith("/view"):
                    body = {"ok": True}
                elif "network" in path:
                    body = {"hostname": "fixture", "addresses": []}
                elif path == "/api/dot/execution" and method == "GET":
                    body = execution
                elif path == "/api/dot/execution" and method == "PUT":
                    assert data["expected_revision"] == execution["revision"]
                    execution_put_attempts += 1
                    if execution_put_attempts == 1:
                        code, body = 409, {
                            "detail": (
                                "Unbind the Feishu Bot before switching Dot's backend, "
                                "then bind it to the new Dot chat."
                            )
                        }
                    else:
                        execution.update(
                            {
                                "tab_id": "dot-tab-next",
                                "agent_type": data["agent_type"],
                                "env": data["env"],
                                "revision": REVISION_2,
                            }
                        )
                        tab.update(
                            {
                                "id": execution["tab_id"],
                                "agent_type": execution["agent_type"],
                                "env": execution["env"],
                            }
                        )
                        fail_next_tabs_refresh = True
                        body = execution
                elif path == "/api/dot/status":
                    body = status()
                elif method == "POST" and path == "/api/dot/setup":
                    body = status()
                elif path == "/api/dot/identity" and method == "GET":
                    body = identity
                elif path == "/api/dot/identity" and method == "PATCH":
                    identity.update(
                        {key: value for key, value in data.items() if value is not None}
                    )
                    tab["name"] = identity["name"]
                    body = identity
                elif path == "/api/dot/focus":
                    body = []
                elif path == "/api/dot/work":
                    body = work
                elif path == "/api/dot/inbox":
                    body = inbox
                elif path == "/api/dot/inbox/ack":
                    inbox.clear()
                    body = {"acked": 1}
                elif path == "/api/dot/work/no-such-task/watch":
                    code, body = 404, {"detail": "Task not found"}
                elif path == "/api/dot/work/task-7/watch":
                    work.append(
                        {
                            "task_id": "task-7",
                            "title": "Train run",
                            "status": "working",
                            "watched": True,
                            "stall_hours": data["stall_hours"],
                            "created": NOW,
                            "updated": NOW,
                        }
                    )
                    body = work[-1]
                elif path == "/api/dot/lessons" and method == "GET":
                    live = [lesson for lesson in lessons if not lesson.get("dropped")]
                    body = {"lessons": live, "stats": review_stats}
                elif path == "/api/dot/lessons" and method == "POST":
                    lessons.append(
                        {
                            "id": "l-2",
                            "rule": data["rule"],
                            "votes": 1,
                            "tasks": [],
                            "created": NOW,
                            "seen": NOW,
                            "score": 1.0,
                            "in_session": True,
                            "reach": "always",
                        }
                    )
                    body = lessons[-1]
                elif path.startswith("/api/dot/lessons/"):
                    lesson = next(item for item in lessons if item["id"] == path.rsplit("/", 1)[1])
                    if method == "PATCH":
                        lesson["rule"] = data["rule"]
                    else:
                        lesson["dropped"] = True
                    body = lesson
                elif path == "/api/dot/work/task-7/unwatch":
                    work.clear()
                    body = {"task_id": "task-7", "closed": True}
                else:
                    report["unknown_requests"].append([method, path])
                    await route.fulfill(
                        status=501,
                        content_type="application/json",
                        body=json.dumps({"detail": f"unmocked API: {method} {path}"}),
                    )
                    return
                await route.fulfill(
                    status=code, content_type="application/json", body=json.dumps(body)
                )

            def wrote(method: str, path: str) -> list:
                return [item[2] for item in writes if item[0] == method and item[1] == path]

            async def shot(name: str) -> None:
                await page.screenshot(path=str(output / name))
                report["screenshots"].append(name)

            await context.route_web_socket("**/*", block_websocket)
            await context.route("**/*", route_all)
            page = await context.new_page()
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            await page.goto(base + f"/?tab={TAB}", wait_until="domcontentloaded")

            # Phone: the app bar is hidden, so the compact launcher sits in the tab bar.
            launchers = page.get_by_test_id("dot-launcher")
            await expect(launchers).to_have_count(1)
            launcher = launchers.first
            await expect(launcher).to_be_visible()
            assert "dot-launcher--compact" in (await launcher.get_attribute("class") or "")
            # The compact launcher shares a row with the other toolbar controls.
            # Check physical boxes across phone widths and the mobile breakpoint.
            report["toolbar_geometry"] = []
            for width in (360, 390, 768):
                await page.set_viewport_size({"width": width, "height": 844})
                await expect(launcher).to_be_visible()
                await expect(page.locator(".tab-bar .tab-indicator")).to_be_visible()
                launcher_box = await launcher.bounding_box()
                add_box = await page.locator(".add-tab").bounding_box()
                menu_box = await page.locator(".mobile-app-menu-trigger").bounding_box()
                assert launcher_box is not None and add_box is not None and menu_box is not None
                report["toolbar_geometry"].append(
                    {
                        "width": width,
                        "dot": launcher_box,
                        "add": add_box,
                        "menu": menu_box,
                    }
                )
                for neighbor in (add_box, menu_box):
                    assert abs(launcher_box["height"] - neighbor["height"]) <= 1, report[
                        "toolbar_geometry"
                    ][-1]
                    assert abs(launcher_box["y"] - neighbor["y"]) <= 1, report["toolbar_geometry"][
                        -1
                    ]
                for button in (".dot-launcher__open", ".dot-launcher__memory"):
                    box = await launcher.locator(button).bounding_box()
                    assert box is not None and box["height"] >= 24 and box["width"] >= 36, box
                    assert 0 <= box["x"] and box["x"] + box["width"] <= width, box
                await expect(page.locator(".tab-bar .agent-status")).to_have_count(0)
                await shot(f"dot-mobile-toolbar-{width}.png")
            await page.set_viewport_size({"width": 390, "height": 844})
            # Hiding summaries must not stop the shared status polling lifecycle.
            indicator = page.locator(".tab-bar .tab-indicator")
            await expect(indicator).to_have_attribute("data-status", "working")
            status_reads = report["api_requests"].count(["GET", "/api/tabs/status"])
            assert status_reads > 0
            agent_status = "attention"
            await expect(indicator).to_have_attribute("data-status", "attention", timeout=12000)
            assert report["api_requests"].count(["GET", "/api/tabs/status"]) > status_reads
            await expect(page.locator(".tab-bar .agent-status")).to_have_count(0)
            await launcher.get_by_role("button", name="Talk to Dot", exact=True).click()
            await expect(
                launcher.get_by_role("button", name="Talk to Dot", exact=True)
            ).to_be_enabled()
            assert len(wrote("POST", "/api/dot/setup")) == 1
            await shot("dot-mobile-home.png")

            await launcher.get_by_role("button", name="Open Dot memory").click()
            panel = page.get_by_test_id("dot-panel")
            await expect(panel).to_be_visible()
            panel_box = await panel.bounding_box()
            assert panel_box is not None and panel_box["width"] == 390, panel_box
            close_box = await panel.get_by_role("button", name="Close Dot memory").bounding_box()
            assert close_box is not None and close_box["height"] >= 44, close_box

            # Rename and set an emoji; the tab list is refreshed for the new tab name.
            await panel.get_by_role("button", name="Edit").click()
            editor = page.get_by_test_id("dot-identity-editor")
            execution_editor = page.get_by_test_id("dot-execution-editor")
            await expect(execution_editor.get_by_label("Execution backend")).to_have_value("claude")
            initial_preset = await execution_editor.get_by_label(
                "Dot environment preset"
            ).input_value()
            assert initial_preset != "custom", initial_preset
            await expect(execution_editor).to_contain_text("Currently legacy-provider")
            await execution_editor.get_by_label("Execution backend").select_option("codex")
            await expect(execution_editor.get_by_label("Dot environment preset")).to_have_value(
                "none"
            )
            await expect(execution_editor).to_contain_text(
                "Applying this creates a fresh Dot chat and archives the current chat."
            )
            await expect(execution_editor).to_contain_text("Feishu Bot must be unbound")
            await execution_editor.get_by_role("button", name="Switch backend", exact=True).click()
            await expect(execution_editor).to_contain_text(
                "Unbind the Feishu Bot before switching Dot's backend"
            )
            await expect(execution_editor.get_by_label("Execution backend")).to_have_value("codex")
            await expect(execution_editor.get_by_label("Dot environment preset")).to_have_value(
                "none"
            )
            await execution_editor.get_by_role("button", name="Retry", exact=True).click()
            await expect(execution_editor).to_contain_text("Dot now uses Codex")
            await expect(execution_editor).to_contain_text(
                "Dot was updated, but the Chat list could not refresh."
            )
            switch_payloads = wrote("PUT", "/api/dot/execution")
            expected_switch = {
                "expected_revision": REVISION_1,
                "agent_type": "codex",
                "env": {},
            }
            assert switch_payloads == [expected_switch, expected_switch]
            assert "old-provider-secret" not in json.dumps(switch_payloads)
            await execution_editor.get_by_role("button", name="Retry tab refresh").click()
            await expect(
                execution_editor.get_by_role("button", name="Retry tab refresh")
            ).to_be_hidden()
            assert wrote("PUT", "/api/dot/execution") == [
                expected_switch,
                expected_switch,
            ]
            await expect(page.locator('.tab[data-tab-id="dot-tab-next"]')).to_have_class(
                re.compile(r"\bactive\b")
            )
            name_input = editor.get_by_label("Name")
            font = await name_input.evaluate("el => getComputedStyle(el).fontSize")
            assert font == "16px", f"phone inputs must be 16px to avoid iOS zoom, got {font}"
            tab_reads = report["api_requests"].count(["GET", "/api/tabs"])
            await name_input.fill("Jarvis")
            await editor.get_by_label("Emoji").fill("🦊")
            await shot("dot-mobile-edit.png")
            await editor.get_by_role("button", name="Save").click()
            await expect(editor).to_be_hidden()
            await expect(panel.locator(".dot-panel__title")).to_have_text("Jarvis")
            await expect(panel.locator(".dot-avatar__text").first).to_have_text("🦊")
            assert wrote("PATCH", "/api/dot/identity") == [{"name": "Jarvis", "avatar": "🦊"}]
            deadline = asyncio.get_running_loop().time() + 5
            while report["api_requests"].count(["GET", "/api/tabs"]) == tab_reads:
                assert asyncio.get_running_loop().time() < deadline, "tabs were not refreshed"
                await asyncio.sleep(0.05)

            # Work: the stuck alarm is flagged, a bad id shows the server error,
            # a good one is watched with the chosen alarm and can be stopped.
            await panel.get_by_role("tab", name="Work").click()
            await expect(panel.locator(".dot-item__badge--warn")).to_have_text("stalled")
            watch_form = page.get_by_test_id("dot-watch-form")
            await watch_form.get_by_label("Task id to watch").fill("no-such-task")
            await watch_form.get_by_role("button", name="Watch").click()
            await expect(panel.locator(".dot-panel__error")).to_have_text("Task not found")
            await watch_form.get_by_label("Task id to watch").fill("task-7")
            await watch_form.get_by_label("Stuck alarm after").select_option("4")
            await watch_form.get_by_role("button", name="Watch").click()
            await expect(panel.get_by_text("task-7 · watched")).to_be_visible()
            await expect(panel.get_by_text("stuck alarm 4h")).to_be_visible()
            assert wrote("POST", "/api/dot/work/task-7/watch") == [{"stall_hours": 4}]
            await shot("dot-mobile-work.png")
            await panel.get_by_role("button", name="Stop").click()
            await expect(panel.get_by_text("task-7 · watched")).to_be_hidden()
            assert wrote("POST", "/api/dot/work/task-7/unwatch") == [None]
            await panel.get_by_role("button", name="Mark read").click()
            await expect(panel.locator(".dot-item__badge--warn")).to_have_count(0)
            # Only the events on screen; one that arrived since stays unread.
            assert wrote("POST", "/api/dot/inbox/ack") == [{"ids": ["e-1"]}]

            # Lessons: first-pass rate, add, edit and drop a rule for briefs.
            await panel.get_by_role("tab", name="Lessons").click()
            await expect(page.get_by_test_id("dot-lesson-stats")).to_contain_text(
                "First pass 3/4 (75%)"
            )
            await expect(page.get_by_test_id("dot-lesson-stats")).to_contain_text("brief 1")
            await expect(page.get_by_test_id("dot-lesson-stats")).to_contain_text(
                "judged 4/5 returned"
            )
            rows = page.get_by_test_id("dot-lesson")
            await expect(rows).to_have_count(1)
            await expect(rows.first).to_contain_text("always")
            lesson_form = page.get_by_test_id("dot-lesson-form")
            await lesson_form.get_by_label("New lesson").fill("Split goals over 7 checks")
            await lesson_form.get_by_role("button", name="Add").click()
            await expect(rows).to_have_count(2)
            assert wrote("POST", "/api/dot/lessons") == [{"rule": "Split goals over 7 checks"}]
            await shot("dot-mobile-lessons.png")
            await rows.first.get_by_role("button", name="Edit").click()
            await rows.first.get_by_label("Lesson rule").fill("Name the paper version")
            await rows.first.get_by_role("button", name="Save").click()
            await expect(rows.first).to_contain_text("Name the paper version")
            assert wrote("PATCH", "/api/dot/lessons/l-1") == [{"rule": "Name the paper version"}]
            await rows.nth(1).get_by_role("button", name="Drop").click()
            await expect(rows).to_have_count(1)
            assert wrote("DELETE", "/api/dot/lessons/l-2") == [None]

            await panel.get_by_role("button", name="Close Jarvis memory").click()
            await expect(panel).to_be_hidden()

            # The desktop status entry returns immediately past the breakpoint.
            for width in (769, 1280):
                await page.set_viewport_size({"width": width, "height": 900})
                await expect(launchers).to_have_count(1)
                await expect(
                    page.locator(".app-mode-tools [data-testid=dot-launcher]")
                ).to_be_visible()
                await expect(page.locator(".tab-bar .dot-launcher")).to_have_count(0)
                await expect(page.locator(".tab-bar .status-trigger")).to_be_visible()
                await expect(page.locator(".dot-launcher__name")).to_have_text("Jarvis")
            await shot("dot-desktop-home.png")

            assert not report["unknown_requests"], report["unknown_requests"]
            assert not report["page_errors"], report["page_errors"]
            report["result"] = "PASS"
        except BaseException as exc:
            failure = exc
            report["error_type"] = type(exc).__name__
            if page is not None:
                try:
                    await page.screenshot(path=str(output / "failure.png"), full_page=True)
                except Exception:
                    pass

    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    if failure is not None:
        raise failure


if __name__ == "__main__":
    run_browser_main(main)
