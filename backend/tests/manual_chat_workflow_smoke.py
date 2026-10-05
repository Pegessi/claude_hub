"""Opt-in HTTP/CLI/browser acceptance using an isolated Hub and real Codex.

Run from the candidate backend with its package on PYTHONPATH after building
the candidate frontend. This creates only task-owned runtime resources; it
does not address an existing Hub or create a recurring schedule.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-provider", action="store_true", required=True)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    backend = Path(__file__).resolve().parents[1]
    checkout = backend.parent
    runtime = Path(tempfile.mkdtemp(prefix="hub-chat-workflow-live-"))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    socket_name = f"ch-workflow-{uuid.uuid4().hex[:12]}"
    url = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.update(
        CLAUDE_HUB_HOME=str(runtime),
        CLAUDE_HUB_STATE_ROOT=str(runtime / "workspaces"),
        CLAUDE_HUB_TMUX_SOCKET=socket_name,
        CLAUDE_HUB_URL=url,
        PYTHONPATH=str(backend),
        PORT=str(port),
        SERVE_FRONTEND="true",
    )
    env.pop("CLAUDE_HUB_TAB_ID", None)
    env.pop("CLAUDE_HUB_TEST_BACKEND_URL", None)
    env.pop("CLAUDE_HUB_ALLOW_LIVE_RUNTIME", None)
    result: dict = {"checkout": str(checkout), "runtime": str(runtime), "url": url}
    process = None
    owned_groups: list[int] = []
    work_id = tab_id = None
    log = (runtime / "server-output.log").open("w")
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "claude_hub.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=backend,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        owned_groups.append(process.pid)
        with httpx.Client(base_url=url, trust_env=False, timeout=150) as client:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("isolated backend exited before readiness")
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                raise TimeoutError("isolated backend did not become ready")

            def request(method: str, path: str, **kwargs):
                response = client.request(method, path, **kwargs)
                response.raise_for_status()
                return response.json()

            workspace = request("POST", "/api/workspaces/ensure", json={"path": str(checkout)})
            wid = workspace["id"]
            request("PUT", f"/api/workspaces/{wid}/feedback/automation", json={"enabled": False})
            assert (
                request(
                    "GET",
                    f"/api/workspaces/{wid}/feedback/context",
                    params={"query": "unrelated-smoke-topic"},
                )
                == []
            )
            tab = request(
                "POST",
                "/api/tabs",
                json={
                    "name": "Isolated workflow acceptance",
                    "cwd": str(checkout),
                    "agent_type": "codex",
                    "session_kind": "chat",
                    "env": {"PYTHONPATH": str(backend), "CLAUDE_HUB_URL": url},
                },
            )
            tab_id = tab["id"]
            cli_env = {**env, "CLAUDE_HUB_TAB_ID": tab_id}
            prompt = (
                "This is a bounded transport acceptance test. Do not edit files, inspect the repo, "
                "delegate, or create schedules. Use only the work report command from your Chat-linked "
                "assignment: first send kind=progress summary=HUB_WORKFLOW_PROGRESS with validation=transport-smoke. "
                "Then send kind=completed summary=HUB_WORKFLOW_COMPLETE with validation=transport-smoke, and stop. "
                "Use the explicit source tab/task/session IDs provided by that assignment. "
                f"The work CLI interpreter is {sys.executable}; run it with -m claude_hub.cli "
                "using the inherited PYTHONPATH."
            )
            command = [
                sys.executable,
                "-m",
                "claude_hub.cli",
                "work",
                "create",
                "--workspace-id",
                wid,
                "--request-key",
                "acceptance-once",
                "--title",
                "Verify linked task reporting",
                "--prompt",
                prompt,
                "--agent-type",
                "codex",
                "--task-mode",
                "direct",
                "--cwd",
                str(checkout),
            ]
            created = subprocess.run(
                command, cwd=backend, env=cli_env, capture_output=True, text=True, timeout=160
            )
            (runtime / "cli-create.json").write_text(created.stdout)
            if created.returncode:
                raise RuntimeError(
                    f"work create failed (exit {created.returncode}); see CLI artifact"
                )
            work = json.loads(created.stdout)
            work_id = work["id"]
            result.update(work_id=work_id, tab_id=tab_id, workspace_id=wid)
            retry = request(
                "POST",
                f"/api/tabs/{tab_id}/work",
                json={
                    "workspace_id": wid,
                    "request_key": "acceptance-once",
                    "title": "Verify linked task reporting",
                    "prompt": prompt,
                    "agent_type": "codex",
                    "task_mode": "direct",
                    "cwd": str(checkout),
                    "kind": "task",
                },
            )
            assert retry["id"] == work_id and retry["run_count"] == 1
            deadline = time.monotonic() + args.timeout
            seen_progress = False
            while time.monotonic() < deadline:
                work = request("GET", f"/api/tabs/{tab_id}/work/{work_id}")
                latest = work.get("latest_result") or {}
                seen_progress |= latest.get("kind") == "progress"
                if work["status"] in {"completed", "failed", "review", "stopped"}:
                    break
                time.sleep(1)
            (runtime / "work.json").write_text(json.dumps(work, indent=2))
            assert work["status"] == "review", f"work did not await acceptance: {work['status']}"
            assert work["latest_result"]["summary"] == "HUB_WORKFLOW_COMPLETE"
            assert work["run_count"] == 1 and len(work["executions"]) == 1
            result["observed_progress_poll"] = seen_progress
            task_id = work["executions"][0]["task_id"]
            reports = request("GET", f"/api/workspaces/{wid}/tasks/{task_id}/reports")
            assert any(
                r.get("state") == "working" and r.get("chat_work_outcome") == "progress"
                for r in reports
            )
            result["progress_preserved_before_completion"] = "pass"
            # Direct tasks retain the existing caller acceptance gate. The test
            # controller accepts only after checking the requested report evidence.
            accepted = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "claude_hub.cli",
                    "task",
                    "accept",
                    task_id,
                    "--workspace-id",
                    wid,
                    "--cleanup-session",
                ],
                cwd=backend,
                env=cli_env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            (runtime / "cli-accept.json").write_text(accepted.stdout)
            (runtime / "cli-accept.stderr").write_text(accepted.stderr)
            assert accepted.returncode == 0, "controller acceptance failed; see CLI artifact"
            work = request("GET", f"/api/tabs/{tab_id}/work/{work_id}")
            assert work["status"] == "completed"
            (runtime / "work.json").write_text(json.dumps(work, indent=2))
            result["caller_acceptance"] = "pass"
            for _ in range(20):
                sessions = request("GET", "/api/workspaces/sessions")
                owned = [
                    s
                    for s in sessions
                    if s.get("workspace_id") == wid and s.get("caller_owned_ephemeral")
                ]
                if not owned:
                    break
                time.sleep(1)
            assert not owned, "completed work retained its caller-owned worker"
            result["owned_worker_cleanup"] = "pass"

            process.terminate()
            process.wait(timeout=20)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "claude_hub.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=backend,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            owned_groups.append(process.pid)
            for _ in range(100):
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            recovered = request("GET", f"/api/tabs/{tab_id}/work/{work_id}")
            assert recovered["status"] == "completed"
            assert (
                recovered["run_count"] == 1
                and recovered["latest_result"]["summary"] == "HUB_WORKFLOW_COMPLETE"
            )
            result["cold_restart"] = "pass"

            # Render the built candidate against the actual API, not intercepted fixtures.
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page_errors = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.goto(url, wait_until="domcontentloaded")
                card = page.locator(f'[data-work-id="{work_id}"]')
                card.wait_for(timeout=30_000)
                assert card.get_attribute("data-status") == "completed"
                page.screenshot(path=str(runtime / "browser.png"), full_page=True)
                browser.close()
                assert not page_errors, page_errors
            result["status"] = "pass"
    except Exception as exc:
        result.update(status="failed", error_type=type(exc).__name__, error=str(exc)[:1000])
        raise
    finally:
        if process and process.poll() is None:
            # Stop only the task-owned work, then terminate only this backend.
            if work_id and tab_id and result.get("status") != "pass":
                try:
                    with httpx.Client(base_url=url, trust_env=False, timeout=15) as client:
                        client.patch(f"/api/tabs/{tab_id}/work/{work_id}", json={"action": "stop"})
                except httpx.HTTPError:
                    pass
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log.close()
        for group in owned_groups:
            try:
                os.killpg(group, signal.SIGTERM)
            except ProcessLookupError:
                pass
        # An isolated tmux server is owned by this test, including on failed startup.
        subprocess.run(["tmux", "-L", socket_name, "kill-server"], capture_output=True, check=False)
        (runtime / "result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
