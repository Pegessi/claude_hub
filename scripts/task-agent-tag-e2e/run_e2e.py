#!/usr/bin/env python3
"""Isolated agent_tag E2E: CLI create, persistence, API board, browser badge."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

E2E_HELPERS = Path(__file__).resolve().parents[1] / "agent-tree-e2e"
sys.path.insert(0, str(E2E_HELPERS))

import run_e2e as base  # noqa: E402

PORT = int(os.environ.get("CLAUDE_HUB_E2E_PORT", "19176"))
FRONTEND_PORT = int(os.environ.get("CLAUDE_HUB_E2E_FRONTEND_PORT", "5176"))
BASE = f"http://127.0.0.1:{PORT}"
FRONTEND_BASE = f"http://127.0.0.1:{FRONTEND_PORT}"
E2E_ROOT = Path(os.environ["CLAUDE_HUB_E2E_HOME"])
REPO = Path(os.environ["CLAUDE_HUB_E2E_REPO"])
BACKEND = Path(os.environ["CLAUDE_HUB_E2E_BACKEND"])
ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
EVIDENCE = E2E_ROOT / "agent_tag_evidence.json"
EXPECTED_PARENT = os.environ.get(
    "CLAUDE_HUB_AGENT_TAG_EXPECTED_PARENT",
    "fac68cba891b5c80931c3fd7edaf62ff8f6d4191",
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


def http(method: str, path: str, body: dict | None = None, timeout: int = 60) -> tuple[int, Any]:
    if method == "POST" and "/reports" in path:
        raise RuntimeError(f"harness must not POST reports: {path}")
    return base.http(method, path, body=body, timeout=timeout)


def task_cli(*args: str) -> Any:
    python = Path(os.environ.get("CLAUDE_HUB_E2E_PYTHON", sys.executable))
    cmd = [
        str(python),
        "-m",
        "claude_hub.cli",
        "--json",
        "--base-url",
        BASE,
        "task",
        *args,
    ]
    env = os.environ.copy()
    env.update(base.NOPROXY)
    env.pop("VIRTUAL_ENV", None)
    env["PYTHONPATH"] = str(BACKEND)
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"task CLI failed ({completed.returncode}): {completed.stderr or completed.stdout}"
        )
    return json.loads(completed.stdout)


def start_frontend_preview() -> subprocess.Popen:
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
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(FRONTEND_BASE, timeout=2) as resp:
                if resp.status == 200:
                    return proc
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("frontend preview did not become ready")


def assert_board_badge(workspace_id: str, tag: str) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(FRONTEND_BASE, wait_until="networkidle", timeout=60000)
        page.evaluate(
            "(workspaceId) => localStorage.setItem('claude_hub_active_workspace_id', workspaceId)",
            workspace_id,
        )
        page.get_by_role("tab", name="Agent Workspace").click()
        page.wait_for_selector(".task-card", timeout=60000)
        tagged_badge_count = page.locator(".agent-tag-badge", has_text=tag).count()
        plain_card = page.locator(".task-card", has=page.locator("h3", has_text="Plain task"))
        untagged_badge_count = plain_card.locator(".agent-tag-badge").count()
        assert tagged_badge_count == 1, page.content()
        assert untagged_badge_count == 0, page.content()
        browser.close()
    return {
        "url": FRONTEND_BASE,
        "tag_visible": tag,
        "tagged_badge_count": tagged_badge_count,
        "untagged_badge_count": untagged_badge_count,
    }


def main() -> None:
    provenance = git_provenance()
    if provenance["git_dirty"]:
        raise RuntimeError(f"git worktree is dirty before E2E: {provenance}")
    if provenance["direct_parent"] != EXPECTED_PARENT:
        raise RuntimeError(
            f"unexpected direct parent {provenance['direct_parent']} != {EXPECTED_PARENT}"
        )
    if provenance["git_diff_check_exit_code"] != 0:
        raise RuntimeError("git diff --check failed before E2E")

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
    try:
        base.wait_health()
        status, workspace = http(
            "POST",
            "/api/workspaces",
            {
                "name": "Agent Tag E2E",
                "path": str(REPO),
                "session_prefix": "at-e2e",
            },
        )
        assert status == 201, workspace
        workspace_id = workspace["id"]
        evidence["workspace_id"] = workspace_id

        tagged = task_cli(
            "create",
            workspace_id,
            "--title",
            "Tagged task",
            "--prompt",
            "Tagged prompt",
            "--agent-tag",
            "review-bot",
        )
        untagged = task_cli(
            "create",
            workspace_id,
            "--title",
            "Plain task",
            "--prompt",
            "Plain prompt",
        )
        evidence["tagged_task_id"] = tagged["id"]
        evidence["untagged_task_id"] = untagged["id"]
        evidence["tagged_agent_tag"] = tagged.get("agent_tag")
        evidence["tagged_api_response"] = tagged
        evidence["untagged_api_response"] = untagged
        assert tagged.get("agent_tag") == "review-bot"
        assert untagged.get("agent_tag") in (None,)

        invalid = subprocess.run(
            [
                str(os.environ.get("CLAUDE_HUB_E2E_PYTHON", sys.executable)),
                "-m",
                "claude_hub.cli",
                "--json",
                "--base-url",
                BASE,
                "task",
                "create",
                workspace_id,
                "--title",
                "Bad",
                "--prompt",
                "Bad",
                "--agent-tag",
                "   ",
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(BACKEND), **base.NOPROXY},
        )
        assert invalid.returncode != 0, invalid.stdout
        evidence["invalid_empty_cli_exit_code"] = invalid.returncode

        invalid_control = subprocess.run(
            [
                str(os.environ.get("CLAUDE_HUB_E2E_PYTHON", sys.executable)),
                "-m",
                "claude_hub.cli",
                "--json",
                "--base-url",
                BASE,
                "task",
                "create",
                workspace_id,
                "--title",
                "Bad control",
                "--prompt",
                "Bad",
                "--agent-tag",
                "bad\nline",
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(BACKEND), **base.NOPROXY},
        )
        assert invalid_control.returncode != 0, invalid_control.stdout
        evidence["invalid_control_cli_exit_code"] = invalid_control.returncode

        _, events = http(
            "GET",
            f"/api/workspaces/{workspace_id}/tasks/{tagged['id']}/events?since_sequence=0",
        )
        evidence["tagged_task_mailbox_events"] = events or []
        evidence["tagged_mailbox_sequence_max"] = max(
            (item.get("sequence", 0) for item in (events or []) if isinstance(item, dict)),
            default=0,
        )
        evidence["tagged_consumer_ack_sequence"] = tagged.get("consumer_ack_sequence", 0)

        state_file = E2E_ROOT / ".claude_hub" / "workspaces" / workspace_id / "state.json"
        raw_state = json.loads(state_file.read_text(encoding="utf-8"))
        raw_tagged = next(item for item in raw_state["tasks"] if item["id"] == tagged["id"])
        raw_plain = next(item for item in raw_state["tasks"] if item["id"] == untagged["id"])
        evidence["raw_state_tagged_agent_tag"] = raw_tagged.get("agent_tag")
        evidence["raw_state_untagged_has_agent_tag"] = "agent_tag" in raw_plain
        evidence["raw_state_tagged_task"] = raw_tagged
        evidence["raw_state_untagged_task"] = raw_plain
        assert raw_tagged.get("agent_tag") == "review-bot"
        assert "agent_tag" not in raw_plain

        _, board = http("GET", f"/api/workspaces/{workspace_id}/board")
        board_by_id = {item["id"]: item for item in board["tasks"]}
        evidence["board_tagged_agent_tag"] = board_by_id[tagged["id"]].get("agent_tag")
        evidence["board_untagged_agent_tag"] = board_by_id[untagged["id"]].get("agent_tag")
        assert board_by_id[tagged["id"]]["agent_tag"] == "review-bot"
        assert board_by_id[untagged["id"]].get("agent_tag") in (None,)

        frontend_proc = start_frontend_preview()
        browser = assert_board_badge(workspace_id, "review-bot")
        evidence["browser"] = browser
        evidence["tagged_badge_count"] = browser["tagged_badge_count"]
        evidence["untagged_badge_count"] = browser["untagged_badge_count"]
    finally:
        if frontend_proc is not None:
            base.stop_backend(frontend_proc)
        base.stop_backend(backend_proc)
        base.kill_e2e_tmux()

    post_provenance = git_provenance()
    evidence.update(
        {
            "post_e2e_head_sha": post_provenance["head_sha"],
            "post_e2e_git_dirty": post_provenance["git_dirty"],
        }
    )
    assert post_provenance["head_sha"] == provenance["head_sha"]
    assert post_provenance["git_dirty"] is False

    EVIDENCE.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
