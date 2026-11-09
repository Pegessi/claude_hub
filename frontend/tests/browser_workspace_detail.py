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
    held = {
        **task_fixture("held-task", "ws-1", "Held for a reviewer question"),
        "status": "review",
    }
    # Review task whose evidence fills a phone screen: the decision must still
    # be reachable without hunting for it.
    deep = {
        **task_fixture("deep-review-task", "ws-1", "Review the parser rewrite"),
        "status": "review",
        "goal_packet": {
            "objective": "Rewrite the parser without changing behaviour",
            "status": "approved",
            "source": "human",
            "acceptance_criteria": [
                "Existing parser tests still pass",
                "No new runtime dependency",
                "Public parser API unchanged",
                "Behaviour differences documented",
                "Rollback path recorded",
                "Windows line endings still parse identically",
                "Streaming mode keeps the same memory ceiling",
                "Error messages keep their original wording",
                "Benchmarks do not regress by more than 2%",
                "The published changelog names every user-visible change",
                "No dead code or unused imports were introduced",
                "The workspace snapshot path is recorded in the handoff",
            ],
            "validation_plan": ["pytest -q tests/parsers"],
            "assumptions": [],
            "out_of_scope": [],
        },
        "latest_progress": {
            "state": "completed",
            "summary": "Parser rewrite finished; waiting for a human decision.",
            "validation": (
                "pytest -q tests/parsers -> 118 passed, 0 failed.\n\n"
                "Long-form regression matrix on both interpreter versions:\n"
                "- corpus A: 42 fixtures, AST identical\n"
                "- corpus B: 17 fixtures, AST identical\n"
                "- corpus C: 9 fixtures, two deliberate diffs documented\n\n"
                "Benchmarks: 3.1% faster on the hot path, no regressions."
            ),
            "risks": (
                "One helper signature widened; every in-repo caller was updated "
                "in the same commit, but out-of-tree consumers would notice."
            ),
            "artifact_refs": ["docs/parsers.md", "reports/parser-diff.md"],
            "call_id": "call-deep",
            "execution_epoch": 2,
            "reported_at": "2026-10-24T09:30:00",
        },
    }

    def lifecycle(task_id: str, phase: str, reason: str, text: str, attention: bool) -> dict:
        return {
            "task_id": task_id,
            "phase": phase,
            "reason": reason,
            "reason_text": text,
            "owner": {"kind": "session", "id": "agent-1", "state": "idle"},
            "attention": attention,
            "blocked_by_task_id": None,
            "holds_session": attention,
            "since": "2026-10-24T09:00:00",
            "legacy_status": "review" if attention else "todo",
        }

    starts: list[dict] = []
    page_errors: list[str] = []
    console_errors: list[str] = []
    blocked: list[tuple[str, str]] = []
    unknown: list[tuple[str, str]] = []
    observations: list[dict] = []

    def board() -> dict:
        return {
            "workspace": WORKSPACES[0],
            "tasks": [task, held, deep],
            "sessions": [],
            "reports": [],
            "markdown_documents": [],
            "snapshot_path": None,
            "tasks_pagination": {
                "next_cursor": None,
                "has_more": False,
                "status_counts": {"todo": 1, "review": 2},
            },
            "task_lifecycles": {
                "detail-task": lifecycle(
                    "detail-task", "draft", "not_started", "Not started", False
                ),
                "held-task": lifecycle(
                    "held-task",
                    "needs_user",
                    "reviewer_needs_input",
                    "AI reviewer has a question for you",
                    True,
                ),
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
                "/api/workspaces/ws-1/tasks/deep-review-task/reports",
                "/api/workspaces/ws-1/tasks/held-task/reports",
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
            held_card = page.locator(".task-card--review").filter(has_text=held["title"])
            note = held_card.locator(".lifecycle-note--attention")
            await expect(note).to_have_text("Needs you · AI reviewer has a question for you")
            note_box, card_box = (
                await note.bounding_box(),
                await held_card.bounding_box(),
            )
            assert note_box and card_box and note_box["width"] > 0
            assert note_box["x"] + note_box["width"] <= card_box["x"] + card_box["width"] + 1
            note_style = await note.evaluate(
                "element => { const style = getComputedStyle(element);"
                " return { background: style.backgroundColor, color: style.color,"
                " weight: style.fontWeight } }"
            )
            assert note_style["background"] not in (
                "rgba(0, 0, 0, 0)",
                "transparent",
            ), note_style
            detail_card = page.locator(".task-card--todo").filter(has_text=task["title"])
            await expect(detail_card.locator(".lifecycle-note")).to_have_count(0)
            await page.screenshot(path=str(output / f"lifecycle-{width}.png"), full_page=True)
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
            # The state pill renders sentence case ("Working"); the raw status
            # string stays in the payload and the board badge dot carries tone.
            await expect(panel.locator(".detail-eyebrow")).to_have_text("Working")
            await expect(start).to_be_hidden()
            if width <= 760:
                await toggle.click()
                await expect(toggle).to_have_attribute("aria-expanded", "false")
                await expect(drawer).to_be_hidden()
            observations.append(
                {
                    "width": width,
                    "drawer_display": "block",
                    "start_submitted": True,
                    "lifecycle_note": "Needs you · AI reviewer has a question for you",
                    "lifecycle_note_style": note_style,
                }
            )
            await panel.get_by_role("button", name="Close task detail", exact=True).click()
            await expect(panel).to_be_hidden()

        # A review task whose evidence fills the screen must still offer the
        # decision without hunting for it: the compact bar appears while the
        # Decision section is off screen, jumps to it, then retires itself.
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.goto(url, wait_until="networkidle")
        await page.locator(".task-card h3").filter(has_text=deep["title"]).click()
        panel = page.locator(".task-detail-panel")
        await expect(panel).to_be_visible()
        decision = panel.locator(".decision-section")
        review_bar = panel.locator(".detail-review-bar")
        await expect(review_bar).to_be_visible()
        await expect(review_bar.locator(".detail-review-text")).not_to_be_empty()
        await page.screenshot(path=str(output / "review-deep-scroll-390.png"), full_page=True)
        decision_box = await decision.bounding_box()
        body_box = await panel.locator(".detail-body").bounding_box()
        assert decision_box and body_box
        assert decision_box["y"] >= body_box["y"] + body_box["height"] - 1, (
            "the Decision section should start below the visible body, "
            f"decision y={decision_box['y']} body bottom={body_box['y'] + body_box['height']}"
        )
        await review_bar.get_by_role("button", name="Review decision", exact=True).click()
        await expect(decision).to_be_in_viewport()
        await expect(review_bar).to_be_hidden()
        # The decision affordance itself stays a real, enabled control once the
        # reviewer is at the decision (the drawer is collapsed on mobile).
        actions_toggle = panel.locator(".detail-footer-toggle")
        await expect(actions_toggle).to_be_visible()
        await actions_toggle.click()
        drawer = panel.locator(".detail-action-drawer")
        await expect(drawer).to_be_visible()
        decision_buttons = drawer.locator("button:enabled")
        assert await decision_buttons.count() >= 1, "the decision offers no enabled action"
        decision_labels = [
            (await decision_buttons.nth(index).inner_text()).strip()
            for index in range(await decision_buttons.count())
        ]
        await page.screenshot(path=str(output / "review-decision-revealed-390.png"), full_page=True)
        observations.append(
            {
                "review_bar": "visible while the decision was off screen",
                "review_bar_jump": "revealed the decision and retired itself",
                "decision_actions": decision_labels,
            }
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
