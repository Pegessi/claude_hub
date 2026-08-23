#!/usr/bin/env python3
"""AC12: throwaway Task Graph E2E — Task CLI/API only (no agent-tree driver)."""

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

PORT = int(os.environ.get("CLAUDE_HUB_E2E_PORT", "19174"))
BASE = f"http://127.0.0.1:{PORT}"
E2E_ROOT = Path(os.environ["CLAUDE_HUB_E2E_HOME"])
REPO = Path(os.environ["CLAUDE_HUB_E2E_REPO"])
BACKEND = Path(os.environ["CLAUDE_HUB_E2E_BACKEND"])
TMUX_PREFIX = os.environ.get("CLAUDE_HUB_E2E_TMUX_PREFIX", "claude-hub-tg-e2e-")
EVIDENCE = E2E_ROOT / "evidence.json"
REPORT_WAIT_SECONDS = float(os.environ.get("CLAUDE_HUB_E2E_REPORT_WAIT", "360"))
NOPROXY = {
    "http_proxy": "",
    "https_proxy": "",
    "HTTP_PROXY": "",
    "HTTPS_PROXY": "",
    "ALL_PROXY": "",
}

_ALLOWED_POST_PREFIXES = ("/api/workspaces",)


def http(method: str, path: str, body: dict | None = None, query: dict | None = None, timeout: int = 60):
    if method == "POST" and "/reports" in path:
        raise RuntimeError(f"harness must not POST reports: {path}")
    if method == "POST" and path.startswith("/api/agent-tree"):
        raise RuntimeError(f"Task Graph E2E must not call agent-tree: {path}")
    if method == "POST" and not path.startswith(_ALLOWED_POST_PREFIXES):
        raise RuntimeError(f"unexpected harness POST {path}")
    url = BASE + path
    if query:
        from urllib.parse import urlencode

        url += "?" + urlencode(query)
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return resp.status, None
            return resp.status, json.loads(raw.decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()
        raise RuntimeError(f"{method} {url} -> {exc.code}: {detail}") from exc


def wait_health(timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            status, payload = http("GET", "/health", timeout=2)
            if status == 200 and payload and payload.get("status") == "healthy":
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.3)
    raise RuntimeError(f"backend not healthy: {last}")


def start_backend(home: Path, launch_env: dict[str, str] | None = None) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(NOPROXY)
    env["CLAUDE_HUB_E2E_HOME"] = str(home)
    env["CLAUDE_HUB_E2E_PORT"] = str(PORT)
    env["CLAUDE_HUB_E2E_TTYD_BASE"] = os.environ.get("CLAUDE_HUB_E2E_TTYD_BASE", "19200")
    env["CLAUDE_HUB_E2E_TMUX_PREFIX"] = TMUX_PREFIX
    env["PYTHONPATH"] = str(BACKEND)
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("CLAUDE_HUB_E2E_LAUNCH_ENV_JSON", None)
    if launch_env:
        env["CLAUDE_HUB_E2E_LAUNCH_ENV_JSON"] = json.dumps(launch_env)
    log_path = home / "backend.stdout.log"
    log_f = open(log_path, "ab")
    python = Path(os.environ.get("CLAUDE_HUB_E2E_PYTHON", sys.executable))
    proc = subprocess.Popen(
        [str(python), str(Path(__file__).with_name("serve.py"))],
        cwd=str(BACKEND),
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    proc._e2e_log = log_f  # type: ignore[attr-defined]
    return proc


def stop_backend(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    except Exception:
        pass
    log_f = getattr(proc, "_e2e_log", None)
    if log_f:
        try:
            log_f.close()
        except Exception:
            pass


def kill_e2e_tmux(extra_names: list[str] | None = None) -> list[str]:
    extra = set(extra_names or [])
    killed: list[str] = []
    panes = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{session_name} #{pane_current_path}"],
        capture_output=True,
        text=True,
    )
    repo = str(REPO.resolve())
    home = str(E2E_ROOT.resolve())
    names = set()
    if panes.returncode == 0:
        for line in panes.stdout.splitlines():
            parts = line.split(" ", 1)
            if len(parts) != 2:
                continue
            name, path = parts
            if name.startswith(TMUX_PREFIX) or name in extra or repo in path or home in path:
                names.add(name)
    ls = subprocess.run(["tmux", "ls", "-F", "#{session_name}"], capture_output=True, text=True)
    if ls.returncode == 0:
        for name in ls.stdout.splitlines():
            if name.startswith(TMUX_PREFIX) or name in extra:
                names.add(name)
    for name in sorted(names):
        subprocess.run(["tmux", "kill-session", "-t", name], check=False)
        killed.append(name)
    return killed


def load_isolated_launch_env() -> tuple[str, dict[str, str]]:
    src = Path.home() / ".claude_hub" / "env_presets.json"
    if not src.exists():
        return "missing", {}
    try:
        data = json.loads(src.read_text())
    except Exception:
        return "unreadable", {}
    presets = list(data.get("custom_presets") or [])
    chosen = next((item for item in presets if item.get("name") == "day1"), None)
    if chosen is None and presets:
        chosen = presets[0]
    if chosen is None:
        return "none", {}
    env: dict[str, str] = {}
    for line in str(chosen.get("text") or "").splitlines():
        text = line.strip()
        if not text.startswith("export ") or "=" not in text:
            continue
        key, value = text[len("export ") :].split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    if "ANTHROPIC_BASE_URL" not in env:
        return "invalid", {}
    return str(chosen.get("name") or "custom"), env


def wait_until(desc: str, fn, timeout: float = 45.0, interval: float = 0.5):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {desc}; last={last!r}")


def _board_task(workspace_id: str, task_id: str) -> dict | None:
    _, board = http("GET", f"/api/workspaces/{workspace_id}/board")
    return next((t for t in board.get("tasks", []) if t["id"] == task_id), None)


def _session_report(task_id: str, session_id: str, reports: list[dict]) -> dict | None:
    owned = [
        r
        for r in reports
        if r.get("session_id") == session_id and (r.get("call_id") or "").strip()
    ]
    return owned[-1] if owned else None


def main() -> int:
    sys.path.insert(0, str(BACKEND))
    from claude_hub.services.task_graph import (  # noqa: WPS433
        make_resident_consumer_key,
        make_task_consumer_key,
    )

    evidence: dict = {
        "port": PORT,
        "home": str(E2E_ROOT),
        "repo": str(REPO),
        "driver": "task_graph_api_only",
        "harness_injected_report": False,
    }
    backend = None
    workspace_id = None
    try:
        E2E_ROOT.mkdir(parents=True, exist_ok=True)
        evidence["pre_killed_tmux"] = kill_e2e_tmux()
        launch_preset, launch_env = load_isolated_launch_env()
        evidence["launch_env_preset"] = launch_preset
        evidence["launch_env_has_auth"] = bool(
            launch_env.get("ANTHROPIC_AUTH_TOKEN") or launch_env.get("ANTHROPIC_API_KEY")
        )
        if not (REPO / ".git").exists():
            subprocess.run(["git", "init"], cwd=REPO, check=True, capture_output=True)
            (REPO / "README.md").write_text("isolated task-graph e2e\n")
            subprocess.run(["git", "add", "README.md"], cwd=REPO, check=True, capture_output=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=e2e@local",
                    "-c",
                    "user.name=e2e",
                    "commit",
                    "-m",
                    "init",
                ],
                cwd=REPO,
                check=True,
                capture_output=True,
            )

        backend = start_backend(E2E_ROOT, launch_env)
        wait_health()
        evidence["backend_pid"] = backend.pid

        _, workspace = http(
            "POST",
            "/api/workspaces",
            {
                "name": "task-graph-e2e",
                "path": str(REPO),
                "session_prefix": "e2e",
                "resident_agent_enabled": False,
            },
        )
        workspace_id = workspace["id"]
        evidence["workspace_id"] = workspace_id
        resident_key = make_resident_consumer_key(workspace_id)
        evidence["resident_consumer_key"] = resident_key
        assert resident_key == f"workspace:{workspace_id}:resident"

        _, parent = http(
            "POST",
            f"/api/workspaces/{workspace_id}/tasks",
            {
                "title": "e2e parent",
                "prompt": "Parent supervisor task for Task Graph E2E.",
                "agent_type": "claude",
                "task_mode": "reviewed",
            },
        )
        parent_id = parent["id"]
        evidence["parent_task_id"] = parent_id
        evidence["parent_consumer_key"] = make_task_consumer_key(parent_id)

        worker_call_id = "e2e-worker-report-1"
        _, child = http(
            "POST",
            f"/api/workspaces/{workspace_id}/tasks",
            {
                "title": "e2e child",
                "prompt": (
                    "Throwaway Task Graph E2E worker. Do not edit repo files. "
                    f"Immediately POST one report to http://127.0.0.1:{PORT}/api/workspaces/sessions/"
                    "<your-session-id>/reports with task_id set, call_id "
                    f"{worker_call_id!r}, state ready_for_review, and message/message_en/message_zh "
                    "exactly E2E_CHILD_REPORT. Then stop."
                ),
                "agent_type": "claude",
                "task_mode": "reviewed",
                "parent_task_id": parent_id,
            },
        )
        child_id = child["id"]
        evidence["child_task_id"] = child_id
        evidence["child_consumer_key"] = make_task_consumer_key(child_id)

        _, started = http(
            "POST",
            f"/api/workspaces/tasks/{child_id}/start",
            {"dispatch_reason": "task-graph-e2e"},
            timeout=120,
        )
        evidence["child_start_status"] = started.get("status")

        def _worker_bound():
            task = _board_task(workspace_id, child_id)
            if not task or not task.get("session_id"):
                return None
            session_id = task["session_id"]
            _, board = http("GET", f"/api/workspaces/{workspace_id}/board")
            session = next((s for s in board.get("sessions", []) if s["id"] == session_id), None)
            if not session:
                return None
            return {"task": task, "session": session}

        bound = wait_until("child worker session", _worker_bound, timeout=90)
        worker_session = bound["session"]
        evidence["worker_session_id"] = worker_session["id"]
        evidence["worker_tmux"] = worker_session.get("tmux_session")
        known_tmux = [worker_session.get("tmux_session")] if worker_session.get("tmux_session") else []

        def _worker_report():
            _, reports = http("GET", f"/api/workspaces/{workspace_id}/tasks/{child_id}/reports")
            report = _session_report(child_id, worker_session["id"], reports)
            if report and "e2e_child_report" in (report.get("message") or "").lower():
                return report
            return None

        worker_report = None
        started_wait = time.time()
        while time.time() - started_wait < REPORT_WAIT_SECONDS:
            worker_report = _worker_report()
            if worker_report:
                break
            time.sleep(2.0)
        if not worker_report:
            raise TimeoutError("timed out waiting for worker CLI report")
        evidence["worker_report_id"] = worker_report["id"]
        evidence["worker_report_call_id"] = worker_report.get("call_id")

        def _reviewer_bound():
            task = _board_task(workspace_id, child_id)
            if not task or not task.get("review_session_id"):
                return None
            reviewer_id = task["review_session_id"]
            _, board = http("GET", f"/api/workspaces/{workspace_id}/board")
            session = next((s for s in board.get("sessions", []) if s["id"] == reviewer_id), None)
            if not session:
                return None
            return {"task": task, "session": session}

        reviewer_bound = wait_until("reviewer session", _reviewer_bound, timeout=120)
        reviewer_session = reviewer_bound["session"]
        evidence["reviewer_session_id"] = reviewer_session["id"]
        if reviewer_session.get("tmux_session"):
            known_tmux.append(reviewer_session["tmux_session"])
        evidence["known_tmux"] = list(dict.fromkeys(n for n in known_tmux if n))

        def _reviewer_report():
            _, reports = http("GET", f"/api/workspaces/{workspace_id}/tasks/{child_id}/reports")
            owned = [
                r
                for r in reports
                if r.get("session_id") == reviewer_session["id"] and (r.get("call_id") or "").strip()
            ]
            for report in owned:
                if report.get("review_decision") or report.get("state") in {
                    "review_passed",
                    "review_failed",
                    "review_needs_input",
                }:
                    return report
            return owned[-1] if owned else None

        reviewer_report = None
        review_wait = time.time()
        while time.time() - review_wait < REPORT_WAIT_SECONDS:
            reviewer_report = _reviewer_report()
            if reviewer_report and (
                reviewer_report.get("review_decision")
                or reviewer_report.get("state") in {"review_passed", "review_failed"}
            ):
                break
            time.sleep(2.0)
        if not reviewer_report:
            raise TimeoutError("timed out waiting for reviewer verdict report")
        evidence["reviewer_report_id"] = reviewer_report["id"]
        evidence["reviewer_report_state"] = reviewer_report.get("state")
        evidence["reviewer_report_decision"] = reviewer_report.get("review_decision")

        _, subtree = http("GET", f"/api/workspaces/{workspace_id}/tasks/{parent_id}/tree")
        evidence["parent_subtree_ids"] = [item["id"] for item in subtree]
        assert child_id in evidence["parent_subtree_ids"]

        cursor = int(_board_task(workspace_id, parent_id).get("consumer_ack_sequence") or 0)
        _, waited = http(
            "POST",
            f"/api/workspaces/{workspace_id}/tasks/{parent_id}/wait",
            query={"subtree": "true", "timeout_seconds": "10", "since_sequence": str(cursor)},
        )
        evidence["parent_wait_count"] = len(waited)
        assert waited, "parent subtree wait returned no TaskMailbox events"
        for event in waited:
            assert event.get("task_id"), event
            assert event.get("actor_session_id"), event
            assert event.get("actor_role"), event
            if event.get("type") in {"REVIEW_STARTED", "REVIEW_PASSED", "REVIEW_FAILED", "REPORT"}:
                assert event.get("review_cycle") is not None, event
        evidence["parent_wait_actor_fields"] = [
            {
                "sequence": e.get("sequence"),
                "type": e.get("type"),
                "task_id": e.get("task_id"),
                "actor_session_id": e.get("actor_session_id"),
                "actor_role": e.get("actor_role"),
                "review_cycle": e.get("review_cycle"),
                "consumer_key": e.get("consumer_key"),
            }
            for e in waited
        ]
        ack_seq = max(int(e["sequence"]) for e in waited)
        _, acked = http(
            "POST",
            f"/api/workspaces/{workspace_id}/tasks/{parent_id}/ack",
            {"sequence": ack_seq},
        )
        evidence["parent_ack_sequence"] = acked.get("consumer_ack_sequence")
        assert acked.get("consumer_ack_sequence") == ack_seq

        state_file = E2E_ROOT / ".claude_hub" / "workspaces" / workspace_id / "state.json"
        snapshot = json.loads(state_file.read_text())
        evidence["task_event_count_before_reload"] = len(snapshot.get("task_events") or [])

        stop_backend(backend)
        backend = None
        time.sleep(1)
        backend = start_backend(E2E_ROOT, launch_env)
        wait_health()
        evidence["backend_pid_after_reload"] = backend.pid

        parent_after = _board_task(workspace_id, parent_id)
        child_after = _board_task(workspace_id, child_id)
        evidence["parent_ack_after_reload"] = parent_after.get("consumer_ack_sequence")
        evidence["child_ack_after_reload"] = child_after.get("consumer_ack_sequence")
        assert parent_after.get("consumer_ack_sequence") == ack_seq
        assert parent_after.get("id") == parent_id
        assert child_after.get("id") == child_id

        _, replay_wait = http(
            "POST",
            f"/api/workspaces/{workspace_id}/tasks/{parent_id}/wait",
            query={"subtree": "true", "timeout_seconds": "1", "since_sequence": str(ack_seq)},
        )
        acked_call_ids = {e.get("call_id") for e in waited}
        evidence["wait_after_reload"] = [e.get("call_id") for e in replay_wait]
        for event in replay_wait:
            call_id = event.get("call_id")
            if call_id in acked_call_ids:
                raise AssertionError(f"re-delivered acked event after reload: {call_id!r}")
            assert int(event["sequence"]) > ack_seq, event

        evidence["ok"] = True
        return 0
    except Exception as exc:  # noqa: BLE001
        evidence["ok"] = False
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        try:
            if workspace_id is not None:
                try:
                    http("DELETE", f"/api/workspaces/{workspace_id}")
                    evidence["deleted_workspace"] = True
                except Exception as exc:  # noqa: BLE001
                    evidence["delete_workspace_error"] = str(exc)
        finally:
            stop_backend(backend)
            evidence["killed_tmux"] = kill_e2e_tmux(evidence.get("known_tmux") or [])
            hub = E2E_ROOT / ".claude_hub"
            if hub.exists():
                shutil.rmtree(hub, ignore_errors=True)
            EVIDENCE.write_text(json.dumps(evidence, indent=2, default=str))
            print(json.dumps(evidence, indent=2, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
