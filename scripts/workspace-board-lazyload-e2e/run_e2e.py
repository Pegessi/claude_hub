#!/usr/bin/env python3
"""E2E: workspace board Done-only lazy-load with full non-Done columns."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

E2E_HELPERS = Path(__file__).resolve().parents[1] / "agent-tree-e2e"
sys.path.insert(0, str(E2E_HELPERS))

import run_e2e as base  # noqa: E402

PORT = int(os.environ.get("CLAUDE_HUB_E2E_PORT", "19177"))
FRONTEND_PORT = int(os.environ.get("CLAUDE_HUB_E2E_FRONTEND_PORT", "5177"))
BASE = f"http://127.0.0.1:{PORT}"
FRONTEND_BASE = f"http://127.0.0.1:{FRONTEND_PORT}"
E2E_ROOT = Path(os.environ["CLAUDE_HUB_E2E_HOME"])
REPO = Path(os.environ["CLAUDE_HUB_E2E_REPO"])
BACKEND = Path(os.environ["CLAUDE_HUB_E2E_BACKEND"])
ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
EVIDENCE = E2E_ROOT / "board_lazyload_evidence.json"
SCREENSHOT = E2E_ROOT / "board_lazyload_after_load_more.png"
DONE_FIXTURE_COUNT = 18
OPEN_FIXTURES = (
    ("Open todo stale", "todo"),
    ("Open queued stale", "queued"),
    ("Open working stale", "working"),
    ("Open review stale", "review"),
)
EXPECTED_PARENT = os.environ.get(
    "CLAUDE_HUB_BOARD_LAZYLOAD_EXPECTED_PARENT",
    "6cd9df1",
)


def git_provenance() -> dict[str, Any]:
    head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    parent = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD^"], text=True).strip()
    dirty_output = subprocess.check_output(
        ["git", "-C", str(ROOT), "status", "--porcelain"],
        text=True,
    ).strip()
    diff_check = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--check"],
        capture_output=True,
        text=True,
    )
    return {
        "head_sha": head,
        "direct_parent": parent,
        "git_dirty": bool(dirty_output),
        "git_diff_check_exit_code": diff_check.returncode,
    }


def http(method: str, path: str, body: dict | None = None, query: dict | None = None) -> tuple[int, Any]:
    if method == "POST" and "/reports" in path:
        raise RuntimeError(f"harness must not POST reports: {path}")
    return base.http(method, path, body=body, query=query)


def start_frontend_preview() -> subprocess.Popen:
    subprocess.run(
        ["pnpm", "build"],
        cwd=str(FRONTEND),
        check=True,
        env={**os.environ, **base.NOPROXY},
    )
    env = os.environ.copy()
    env.update(base.NOPROXY)
    env["VITE_API_TARGET"] = BASE
    log_path = E2E_ROOT / "frontend.preview.log"
    log_f = open(log_path, "ab")
    proc = subprocess.Popen(
        ["pnpm", "exec", "vite", "preview", "--host", "127.0.0.1", "--port", str(FRONTEND_PORT)],
        cwd=str(FRONTEND),
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    proc._e2e_log = log_f  # type: ignore[attr-defined]
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(FRONTEND_BASE, timeout=2) as resp:
                if resp.status == 200:
                    return proc
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("frontend preview did not become ready")


def stop_frontend(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
    except Exception:
        proc.kill()
    log_f = getattr(proc, "_e2e_log", None)
    if log_f:
        log_f.close()


def is_initial_board_url(url: str) -> bool:
    return "tasks_limit=15" in url and "tasks_cursor" not in url


def is_cursor_board_url(url: str) -> bool:
    return "tasks_limit=15" in url and "tasks_cursor=" in url


def _board_done_sort_key(task: dict[str, Any]) -> tuple[float, float, str]:
    def _ts(value: str | None) -> float:
        if not value:
            return 0.0
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()

    updated = _ts(task.get("updated_at"))
    created = _ts(task.get("created_at"))
    return (-updated, created, task["id"])


def browser_lazyload_evidence(
    workspace_id: str,
    sixteenth_done_title: str,
    open_titles: list[str],
    expected_remaining: int,
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    board_requests: list[dict[str, Any]] = []
    board_responses: list[dict[str, Any]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

        def on_request(request) -> None:
            if "/board" in request.url:
                board_requests.append(
                    {
                        "url": request.url,
                        "method": request.method,
                    }
                )

        def on_response(response) -> None:
            if "/board" in response.url and response.status == 200:
                try:
                    body = response.json()
                except Exception:
                    return
                board_responses.append(
                    {
                        "url": response.url,
                        "status": response.status,
                        "tasks_length": len(body.get("tasks", [])),
                        "task_ids": [task["id"] for task in body.get("tasks", [])],
                        "task_titles": [task.get("title", "") for task in body.get("tasks", [])],
                        "task_statuses": [task.get("status", "") for task in body.get("tasks", [])],
                        "tasks_pagination": body.get("tasks_pagination"),
                    }
                )

        page.on("request", on_request)
        page.on("response", on_response)
        page.goto(FRONTEND_BASE, wait_until="networkidle", timeout=120000)
        page.evaluate(
            "(workspaceId) => localStorage.setItem('claude_hub_active_workspace_id', workspaceId)",
            workspace_id,
        )
        page.get_by_role("tab", name="Agent Workspace").click()
        page.wait_for_selector(".task-card", timeout=120000)
        page.wait_for_timeout(800)

        initial_cards = page.locator(".task-card").count()
        initial_titles = page.locator(".task-card h3").all_text_contents()
        initial_board_responses = [
            item for item in board_responses if is_initial_board_url(item["url"])
        ]
        for title in open_titles:
            assert page.locator(".task-card h3", has_text=title).count() >= 1, title

        sixteenth_visible_before = page.locator(".task-card h3", has_text=sixteenth_done_title).count()
        load_more = page.locator(".board-history-load-more")
        assert load_more.count() == 1, page.content()
        assert f"({expected_remaining} more)" in load_more.inner_text(), load_more.inner_text()

        load_more.click()
        page.wait_for_function(
            "(title) => Array.from(document.querySelectorAll('.task-card h3')).some("
            "el => el.textContent && el.textContent.includes(title))",
            arg=sixteenth_done_title,
            timeout=60000,
        )
        page.wait_for_timeout(500)

        cursor_board_responses = [
            item for item in board_responses if is_cursor_board_url(item["url"])
        ]

        after_cards = page.locator(".task-card").count()
        after_titles = page.locator(".task-card h3").all_text_contents()
        sixteenth_visible_after = page.locator(".task-card h3", has_text=sixteenth_done_title).count()
        for title in open_titles:
            assert page.locator(".task-card h3", has_text=title).count() >= 1, title
        page.screenshot(path=str(SCREENSHOT), full_page=True)
        browser.close()

    return {
        "initial_dom_task_cards": initial_cards,
        "initial_dom_titles": initial_titles,
        "after_load_more_dom_task_cards": after_cards,
        "after_load_more_dom_titles": after_titles,
        "sixteenth_done_title_expected": sixteenth_done_title,
        "open_titles_expected": open_titles,
        "load_more_remaining_expected": expected_remaining,
        "sixteenth_visible_before": sixteenth_visible_before,
        "sixteenth_visible_after": sixteenth_visible_after,
        "initial_board_requests_with_limit_15": len(
            [item for item in board_requests if is_initial_board_url(item["url"])]
        ),
        "board_requests": board_requests,
        "board_responses": board_responses,
        "initial_board_responses_with_limit_15": initial_board_responses,
        "cursor_board_responses_with_limit_15": cursor_board_responses,
        "screenshot": str(SCREENSHOT),
    }


def main() -> None:
    provenance = git_provenance()
    if provenance["git_dirty"]:
        raise RuntimeError(f"git worktree is dirty before E2E: {provenance}")
    if not provenance["direct_parent"].startswith(EXPECTED_PARENT):
        raise RuntimeError(
            f"unexpected direct parent {provenance['direct_parent']} !~= {EXPECTED_PARENT}"
        )

    if E2E_ROOT.exists():
        shutil.rmtree(E2E_ROOT, ignore_errors=True)
    E2E_ROOT.mkdir(parents=True, exist_ok=True)
    REPO.mkdir(parents=True, exist_ok=True)

    backend_proc = base.start_backend(E2E_ROOT)
    frontend_proc: subprocess.Popen | None = None
    evidence: dict[str, Any] = {
        "base_url": BASE,
        "frontend_url": FRONTEND_BASE,
        **provenance,
    }
    workspace_id: str | None = None
    open_task_ids: list[str] = []
    done_task_ids: list[str] = []
    passed = False
    delete_error: Exception | None = None
    try:
        base.wait_health()
        status, workspace = http(
            "POST",
            "/api/workspaces",
            {
                "name": "Board Lazyload E2E",
                "path": str(REPO),
                "session_prefix": "bl-e2e",
            },
        )
        assert status == 201, workspace
        workspace_id = workspace["id"]
        evidence["workspace_id"] = workspace_id

        for title, task_status in OPEN_FIXTURES:
            create_status, task = http(
                "POST",
                f"/api/workspaces/{workspace_id}/tasks",
                {"title": title, "prompt": f"Fixture {title}"},
            )
            assert create_status == 201, task
            open_task_ids.append(task["id"])
            patch_status, _ = http(
                "PATCH",
                f"/api/workspaces/tasks/{task['id']}",
                {"status": task_status},
            )
            assert patch_status == 200
            time.sleep(0.02)

        for index in range(DONE_FIXTURE_COUNT):
            create_status, task = http(
                "POST",
                f"/api/workspaces/{workspace_id}/tasks",
                {
                    "title": f"Done fixture {index:02d}",
                    "prompt": f"Done fixture task {index}",
                },
            )
            assert create_status == 201, task
            done_task_ids.append(task["id"])
            patch_status, _ = http(
                "PATCH",
                f"/api/workspaces/tasks/{task['id']}",
                {"status": "done"},
            )
            assert patch_status == 200
            time.sleep(0.02)

        evidence["open_fixture_task_ids"] = open_task_ids
        evidence["done_fixture_task_ids"] = done_task_ids

        status, full_board = http(
            "GET",
            f"/api/workspaces/{workspace_id}/board",
            query={"tasks_limit": "100"},
        )
        assert status == 200, full_board
        assert len(full_board["tasks"]) == len(open_task_ids) + DONE_FIXTURE_COUNT
        done_on_board = [task for task in full_board["tasks"] if task["status"] == "done"]
        open_on_board = [task for task in full_board["tasks"] if task["status"] != "done"]
        assert len(done_on_board) == DONE_FIXTURE_COUNT
        assert len(open_on_board) == len(open_task_ids)
        evidence["full_board_done_titles"] = [task.get("title", "") for task in done_on_board]
        evidence["full_board_open_titles"] = [task.get("title", "") for task in open_on_board]

        sorted_done = sorted(done_on_board, key=_board_done_sort_key)
        sixteenth_done = sorted_done[15]
        sixteenth_title = sixteenth_done["title"]
        expected_page2_ids = [task["id"] for task in sorted_done[15:]]
        expected_remaining = DONE_FIXTURE_COUNT - 15
        evidence["sixteenth_done_task_id"] = sixteenth_done["id"]
        evidence["sixteenth_done_title"] = sixteenth_title

        status, initial_board = http(
            "GET",
            f"/api/workspaces/{workspace_id}/board",
            query={"tasks_limit": "15"},
        )
        assert status == 200, initial_board
        evidence["initial_api_task_count"] = len(initial_board["tasks"])
        evidence["initial_api_pagination"] = initial_board.get("tasks_pagination")
        evidence["initial_api_task_ids"] = [task["id"] for task in initial_board["tasks"]]
        initial_done = [task for task in initial_board["tasks"] if task["status"] == "done"]
        initial_open = [task for task in initial_board["tasks"] if task["status"] != "done"]
        assert len(initial_open) == len(open_task_ids)
        assert len(initial_done) == 15
        assert len(initial_board["tasks"]) == len(open_task_ids) + 15
        assert initial_board["tasks_pagination"]["total_count"] == DONE_FIXTURE_COUNT
        assert set(task["id"] for task in initial_open) == set(open_task_ids)
        assert {task["id"] for task in initial_done} == {task["id"] for task in sorted_done[:15]}

        status, page2_board = http(
            "GET",
            f"/api/workspaces/{workspace_id}/board",
            query={
                "tasks_limit": "15",
                "tasks_cursor": initial_board["tasks_pagination"]["next_cursor"],
            },
        )
        assert status == 200, page2_board
        evidence["page2_api_task_count"] = len(page2_board["tasks"])
        evidence["page2_api_task_ids"] = [task["id"] for task in page2_board["tasks"]]
        assert len(page2_board["tasks"]) == expected_remaining
        assert all(task["status"] == "done" for task in page2_board["tasks"])
        assert evidence["page2_api_task_ids"] == expected_page2_ids
        assert evidence["page2_api_task_ids"][0] == sixteenth_done["id"]

        frontend_proc = start_frontend_preview()
        browser = browser_lazyload_evidence(
            workspace_id,
            sixteenth_title,
            [title for title, _ in OPEN_FIXTURES],
            expected_remaining,
        )
        evidence["browser"] = browser

        expected_initial_cards = len(open_task_ids) + 15
        assert browser["initial_dom_task_cards"] == expected_initial_cards, browser
        assert browser["initial_board_requests_with_limit_15"] >= 1, browser
        assert browser["initial_board_responses_with_limit_15"], browser
        for response in browser["initial_board_responses_with_limit_15"]:
            assert "tasks_cursor" not in response["url"], response
            assert response["tasks_length"] == expected_initial_cards, response
            assert response["task_ids"] == evidence["initial_api_task_ids"], response
            assert len([s for s in response["task_statuses"] if s != "done"]) == len(open_task_ids)
        assert browser["cursor_board_responses_with_limit_15"], browser
        cursor_response = browser["cursor_board_responses_with_limit_15"][-1]
        assert cursor_response["task_ids"] == expected_page2_ids, cursor_response
        assert cursor_response["task_ids"][0] == sixteenth_done["id"], cursor_response
        assert cursor_response["tasks_length"] == len(expected_page2_ids), cursor_response
        assert browser["sixteenth_visible_before"] == 0, browser
        assert browser["sixteenth_visible_after"] >= 1, browser
        assert browser["after_load_more_dom_task_cards"] == len(open_task_ids) + DONE_FIXTURE_COUNT, browser
        assert sixteenth_title in browser["after_load_more_dom_titles"], browser
        assert sixteenth_title not in browser["initial_dom_titles"], browser

        evidence["expected_page2_ids"] = expected_page2_ids
        passed = True
        evidence["passed"] = True
        EVIDENCE.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(json.dumps({"passed": True, "evidence": str(EVIDENCE)}, indent=2))
    finally:
        if workspace_id:
            try:
                delete_status, _ = http("DELETE", f"/api/workspaces/{workspace_id}")
                evidence["fixture_workspace_deleted"] = delete_status in {200, 204}
                evidence["fixture_delete_status"] = delete_status
                if delete_status not in {200, 204}:
                    delete_error = RuntimeError(f"fixture workspace delete failed: {delete_status}")
            except Exception as exc:
                evidence["fixture_workspace_deleted"] = False
                evidence["fixture_delete_error"] = str(exc)
                delete_error = exc
        stop_frontend(frontend_proc)
        base.stop_backend(backend_proc)
        if passed:
            EVIDENCE.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        elif E2E_ROOT.exists():
            failure_evidence = E2E_ROOT / "board_lazyload_evidence.failure.json"
            failure_evidence.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        if passed and not evidence.get("fixture_workspace_deleted"):
            delete_error = delete_error or RuntimeError(
                "E2E passed but fixture workspace was not deleted"
            )
        if delete_error is not None and passed:
            raise delete_error


if __name__ == "__main__":
    main()
