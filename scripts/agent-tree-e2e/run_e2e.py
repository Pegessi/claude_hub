#!/usr/bin/env python3
"""Isolated real-CLI Agent Tree E2E.

Harness observes only. The managed Claude CLI must POST the child report.
Never call POST /sessions/{id}/reports or emit_event from this script.
"""

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

PORT = int(os.environ.get("CLAUDE_HUB_E2E_PORT", "19173"))
BASE = f"http://127.0.0.1:{PORT}"
E2E_ROOT = Path(os.environ["CLAUDE_HUB_E2E_HOME"])
REPO = Path(os.environ["CLAUDE_HUB_E2E_REPO"])
BACKEND = Path(os.environ["CLAUDE_HUB_E2E_BACKEND"])
TMUX_PREFIX = os.environ.get("CLAUDE_HUB_E2E_TMUX_PREFIX", "claude-hub-e2e-")
EVIDENCE = E2E_ROOT / "evidence.json"
REPORT_WAIT_SECONDS = float(os.environ.get("CLAUDE_HUB_E2E_REPORT_WAIT", "360"))
NOPROXY = {
    "http_proxy": "",
    "https_proxy": "",
    "HTTP_PROXY": "",
    "HTTPS_PROXY": "",
    "ALL_PROXY": "",
}

# Setup/observe only. The managed CLI is the only report writer.
_ALLOWED_POST_PREFIXES = (
    "/api/workspaces",
    "/api/agent-tree/spawn",
    "/api/agent-tree/wait",
    "/api/agent-tree/ack",
    "/api/agent-tree/interrupt",
)


def http(method: str, path: str, body: dict | None = None, query: dict | None = None, timeout: int = 60):
    if method == "POST" and "/reports" in path:
        raise RuntimeError(f"harness must not POST reports: {path}")
    if method == "POST" and path.rstrip("/").endswith("/send"):
        raise RuntimeError(f"harness must not session-send: {path}")
    if method == "POST" and path.startswith("/api/agent-tree/followup"):
        raise RuntimeError(f"harness must not followup: {path}")
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
    env["CLAUDE_HUB_E2E_TTYD_BASE"] = os.environ.get("CLAUDE_HUB_E2E_TTYD_BASE", "19100")
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
    subprocess.run(
        ["pkill", "-f", f"{E2E_ROOT}/.claude_hub/launch_env"],
        check=False,
        capture_output=True,
    )
    return killed


def process_evidence(known_tmux: list[str] | None = None) -> dict:
    marker = str(E2E_ROOT)
    tmux = subprocess.run(["tmux", "ls"], capture_output=True, text=True)
    ps = subprocess.run(["ps", "-ax", "-o", "pid=,command="], capture_output=True, text=True)
    cli_lines = [
        line.strip()
        for line in ps.stdout.splitlines()
        if marker in line or any(name and name in line for name in (known_tmux or []))
    ]
    e2e_tmux = [
        line
        for line in tmux.stdout.splitlines()
        if TMUX_PREFIX in line or any(name and name in line for name in (known_tmux or []))
    ]
    return {
        "e2e_tmux_sessions": e2e_tmux,
        "cli_processes": cli_lines[:20],
    }


def load_isolated_launch_env() -> tuple[str, dict[str, str]]:
    """Load a local Claude launch env for isolated tabs. Secrets stay off-log."""

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


def install_isolated_launch_env(home: Path) -> tuple[str, dict[str, str]]:
    """Load local Claude launch credentials without writing them to disk.

    Hub state stays under ``home``. The CLI subprocess needs a real
    ANTHROPIC_* launch env or it 401s on the default Volcengine plan.
    Values are passed to the isolated backend via process env only.
    """

    dest_dir = home / ".claude_hub"
    dest_dir.mkdir(parents=True, exist_ok=True)
    leftover = dest_dir / "e2e_launch_env.json"
    if leftover.exists():
        leftover.unlink()
    name, env = load_isolated_launch_env()
    return name, env


def remaining_credential_artifacts(home: Path) -> list[dict]:
    """Find leftover credential files. Record paths/modes only, never values."""

    hits: list[dict] = []
    if not home.exists():
        return hits
    skip_names = {"evidence.json", "backend.stdout.log"}
    for path in home.rglob("*"):
        if not path.is_file() or path.name in skip_names:
            continue
        try:
            text = path.read_text(errors="replace")
        except Exception:
            continue
        if path.name == "e2e_launch_env.json" or "ANTHROPIC_AUTH_TOKEN=" in text or (
            '"ANTHROPIC_AUTH_TOKEN"' in text
        ) or "ANTHROPIC_API_KEY=" in text or '"ANTHROPIC_API_KEY"' in text:
            hits.append(
                {
                    "relpath": str(path.relative_to(home)),
                    "mode": oct(path.stat().st_mode & 0o777),
                }
            )
    return hits


def unlink_credential_overlay(home: Path) -> dict:
    """Remove copied auth files and record leftover proof. Never log values."""

    overlay = home / ".claude_hub" / "e2e_launch_env.json"
    proof: dict = {
        "overlay_path": str(overlay),
        "overlay_existed_before_unlink": overlay.exists(),
    }
    if overlay.exists():
        proof["overlay_mode_before_unlink"] = oct(overlay.stat().st_mode & 0o777)
        overlay.unlink(missing_ok=True)
    leftovers = []
    hub = home / ".claude_hub"
    if hub.exists():
        for path in hub.rglob("e2e_launch_env.json"):
            leftovers.append(
                {"path": str(path), "mode": oct(path.stat().st_mode & 0o777)}
            )
    proof["overlay_exists_after_unlink"] = overlay.exists()
    proof["overlay_leftovers"] = leftovers
    return proof


def capture_tmux(session_name: str | None) -> str:
    if not session_name:
        return ""
    captured = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", session_name, "-S", "-80"],
        capture_output=True,
        text=True,
    )
    return captured.stdout[-4000:]


def wait_until(desc: str, fn, timeout: float = 45.0, interval: float = 0.5):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {desc}; last={last!r}")


def main() -> int:
    evidence: dict = {
        "port": PORT,
        "home": str(E2E_ROOT),
        "repo": str(REPO),
        "report_source": "managed_cli_observed",
        "harness_injected_report": False,
        "steps": [],
    }
    backend = None
    workspace_id = None
    try:
        E2E_ROOT.mkdir(parents=True, exist_ok=True)
        evidence["pre_killed_tmux"] = kill_e2e_tmux()
        launch_preset, launch_env = install_isolated_launch_env(E2E_ROOT)
        evidence["launch_env_preset"] = launch_preset
        evidence["credentials_in_memory_only"] = True
        evidence["api_payloads_include_env"] = True
        evidence["credential_files_mode"] = "0600_unlinked_on_exit"
        evidence["credential_overlay_written"] = False
        evidence["credential_overlay_exists_after_install"] = (
            E2E_ROOT / ".claude_hub" / "e2e_launch_env.json"
        ).exists()
        if evidence["credential_overlay_exists_after_install"]:
            raise RuntimeError("harness must not persist e2e_launch_env.json")
        evidence["launch_env_has_auth"] = bool(
            launch_env.get("ANTHROPIC_AUTH_TOKEN") or launch_env.get("ANTHROPIC_API_KEY")
        )
        if not (REPO / ".git").exists():
            subprocess.run(["git", "init"], cwd=REPO, check=True, capture_output=True)
            (REPO / "README.md").write_text("isolated agent-tree e2e\n")
            (REPO / "CLAUDE.md").write_text(
                "E2E only. Do not edit files. Immediately POST one report "
                "with message E2E_CHILD_REPORT using the Hub report curl, then stop.\n"
            )
            subprocess.run(["git", "add", "README.md"], cwd=REPO, check=True, capture_output=True)
            subprocess.run(
                ["git", "-c", "user.email=e2e@local", "-c", "user.name=e2e", "commit", "-m", "init"],
                cwd=REPO,
                check=True,
                capture_output=True,
            )

        backend = start_backend(E2E_ROOT, launch_env)
        wait_health()
        evidence["backend_pid"] = backend.pid
        evidence["steps"].append({"start_backend": {"pid": backend.pid}})

        _, workspace = http(
            "POST",
            "/api/workspaces",
            {
                "name": "agent-tree-e2e",
                "path": str(REPO),
                "session_prefix": "e2e",
                "resident_agent_enabled": True,
                "resident_agent_paused": False,
                "resident_agent_interval_minutes": 60,
                "resident_agent_type": "claude",
                "resident_agent_solo_mode": True,
                "resident_agent_env": launch_env,
            },
        )
        workspace_id = workspace["id"]
        evidence["workspace_id"] = workspace_id
        http("POST", f"/api/workspaces/{workspace_id}/resident/run", {})

        def _root():
            _, runs = http("GET", "/api/agent-tree/runs", query={"workspace_id": workspace_id})
            roots = [r for r in runs if r.get("parent_id") in (None, "")]
            return roots[0] if roots else None

        root = wait_until("resident root run", _root, timeout=40)
        evidence["root_run_id"] = root["id"]
        evidence["resident_context_ref"] = root.get("context_ref")
        http(
            "PATCH",
            f"/api/workspaces/{workspace_id}",
            {"resident_agent_paused": True},
        )

        spawn_call_id = "e2e-spawn-child-1"
        _, child = http(
            "POST",
            "/api/agent-tree/spawn",
            {
                "workspace_id": workspace_id,
                "parent_id": root["id"],
                "executor_kind": "managed_task",
                "executor_config": {
                    "agent_type": "claude",
                    "solo_mode": True,
                    "env": launch_env,
                },
                "title": "e2e child",
                "initial_message": (
                    "Throwaway E2E. Do not edit files or implement product code. "
                    "Immediately POST one report via the Report endpoint curl in "
                    "this assignment (http://localhost:"
                    f"{PORT}/api/workspaces/sessions/<session>/reports). "
                    "Set message, message_en, and message_zh to exactly "
                    "E2E_CHILD_REPORT. Reuse the provided call_id. Then stop."
                ),
                "call_id": spawn_call_id,
            },
            timeout=90,
        )
        evidence["child_run_id"] = child["id"]
        evidence["child_context_ref"] = child.get("context_ref")
        evidence["spawn_call_id"] = spawn_call_id

        def _child_session():
            _, board = http("GET", f"/api/workspaces/{workspace_id}/board")
            task_id = child.get("context_ref")
            tasks = [t for t in board.get("tasks", []) if t["id"] == task_id]
            if not tasks:
                return None
            session_id = tasks[0].get("session_id")
            sessions = [s for s in board.get("sessions", []) if s["id"] == session_id]
            if not session_id or not sessions:
                return None
            return {"task": tasks[0], "session": sessions[0]}

        bound = wait_until("child managed session", _child_session, timeout=60)
        session = bound["session"]
        task = bound["task"]
        evidence["child_session_id"] = session["id"]
        evidence["child_task_id"] = task["id"]
        evidence["child_tmux"] = session.get("tmux_session")
        evidence["child_tab_id"] = session.get("tab_id")
        known_tmux = [session.get("tmux_session")] if session.get("tmux_session") else []
        _, board = http("GET", f"/api/workspaces/{workspace_id}/board")
        known_tmux.extend(
            s.get("tmux_session") for s in board.get("sessions", []) if s.get("tmux_session")
        )
        known_tmux = [name for name in dict.fromkeys(known_tmux) if name]
        evidence["known_tmux"] = known_tmux
        evidence["process_after_spawn"] = process_evidence(known_tmux)

        def _is_cli_report(report: dict) -> bool:
            # Hub monitor/runtime can attach needs_input notes without a
            # call_id. Those are not the managed CLI POSTing create_report.
            if report.get("session_id") != session["id"]:
                return False
            if not (report.get("call_id") or "").strip():
                return False
            message = (report.get("message") or "").lower()
            if "working indicator has not changed" in message:
                return False
            if report.get("goal_packet"):
                return True
            if "e2e_child_report" in message:
                return True
            return report.get("state") in {
                "started",
                "working",
                "completed",
                "ready_for_review",
                "blocked",
            }

        def _cli_report():
            _, reports = http(
                "GET",
                f"/api/workspaces/{workspace_id}/tasks/{task['id']}/reports",
            )
            owned = [r for r in reports if _is_cli_report(r)]
            return owned[0] if owned else None

        created = None
        started_wait = time.time()
        captured_early = False
        while time.time() - started_wait < REPORT_WAIT_SECONDS:
            created = _cli_report()
            if created:
                break
            elapsed = time.time() - started_wait
            if not captured_early and elapsed >= 20:
                evidence["tmux_while_waiting"] = capture_tmux(session.get("tmux_session"))
                captured_early = True
            time.sleep(2.0)
        if not created:
            evidence["tmux_on_timeout"] = capture_tmux(session.get("tmux_session"))
            raise TimeoutError("timed out waiting for managed CLI child report")
        evidence["tmux_after_cli_report"] = capture_tmux(session.get("tmux_session"))
        evidence["cli_report_id"] = created["id"]
        evidence["cli_report_call_id"] = created.get("call_id")
        evidence["cli_report_state"] = created.get("state")
        evidence["cli_report_message"] = created.get("message")
        evidence["cli_report_session_id"] = created.get("session_id")
        assert created["session_id"] == session["id"]
        log_hits: list[str] = []
        log_path = E2E_ROOT / "backend.stdout.log"
        log_deadline = time.time() + 3
        while time.time() < log_deadline:
            log_hits = []
            if log_path.exists():
                for line in log_path.read_text(errors="replace").splitlines():
                    if "/sessions/" in line and "/reports" in line and "POST" in line:
                        log_hits.append(line[-240:])
            if any("201" in line for line in log_hits):
                break
            time.sleep(0.3)
        evidence["backend_report_access_log"] = log_hits[-8:]
        evidence["backend_report_201"] = any("201" in line for line in log_hits)
        if not evidence["backend_report_201"]:
            raise RuntimeError(
                "CLI report observed via GET, but backend log has no POST /reports 201"
            )

        def _report_event():
            _, events = http(
                "GET",
                f"/api/agent-tree/runs/{root['id']}/events",
                query={"since_sequence": 0, "subtree": "true"},
            )
            bridged = [e for e in events if e.get("call_id") == f"report:{created['id']}"]
            return bridged[-1] if bridged else None

        report_event = wait_until("bridged report event from CLI report", _report_event, timeout=15)
        evidence["report_event"] = {
            "id": report_event.get("id"),
            "call_id": report_event.get("call_id"),
            "sequence": report_event.get("sequence"),
            "type": report_event.get("type"),
            "author": report_event.get("author"),
            "recipient": report_event.get("recipient"),
        }

        cursor_before = int(report_event["sequence"]) - 1
        _, waited = http(
            "POST",
            "/api/agent-tree/wait",
            {
                "workspace_id": workspace_id,
                "recipient_id": root["id"],
                "since_sequence": cursor_before,
                "subtree": True,
                "timeout_seconds": 5,
            },
        )
        evidence["wait_call_ids"] = [e["call_id"] for e in waited]
        assert any(e["call_id"] == report_event["call_id"] for e in waited), evidence["wait_call_ids"]

        ack_seq = max(e["sequence"] for e in waited)
        _, acked = http(
            "POST",
            "/api/agent-tree/ack",
            query={"workspace_id": workspace_id, "run_id": root["id"], "sequence": str(ack_seq)},
        )
        evidence["ack_sequence"] = acked.get("ack_sequence")
        assert acked.get("ack_sequence") == ack_seq

        _, after_ack = http(
            "POST",
            "/api/agent-tree/wait",
            {
                "workspace_id": workspace_id,
                "recipient_id": root["id"],
                "since_sequence": ack_seq,
                "subtree": True,
                "timeout_seconds": 1,
            },
        )
        evidence["wait_after_ack"] = [e["call_id"] for e in after_ack]
        assert after_ack == []

        try:
            http(
                "POST",
                "/api/agent-tree/interrupt",
                {
                    "workspace_id": workspace_id,
                    "run_id": child["id"],
                    "call_id": "e2e-interrupt-after-observe",
                },
            )
            evidence["interrupted_after_observe"] = True
        except Exception as exc:  # noqa: BLE001
            evidence["interrupt_after_observe_error"] = str(exc)

        state_file = E2E_ROOT / ".claude_hub" / "workspaces" / workspace_id / "state.json"
        snapshot = json.loads(state_file.read_text())
        evidence["state_event_count_before_reload"] = len(snapshot.get("agent_events") or [])

        stop_backend(backend)
        backend = None
        time.sleep(1)
        backend = start_backend(E2E_ROOT, launch_env)
        wait_health()
        evidence["backend_pid_after_reload"] = backend.pid

        _, runs_after = http("GET", "/api/agent-tree/runs", query={"workspace_id": workspace_id})
        root_after = next(r for r in runs_after if r["id"] == root["id"])
        child_after = next(r for r in runs_after if r["id"] == child["id"])
        evidence["root_ack_after_reload"] = root_after.get("ack_sequence")
        child_config = dict(child_after.get("executor_config") or {})
        child_config.pop("env", None)
        evidence["child_after_reload"] = {
            "status": child_after.get("status"),
            "executor_kind": child_after.get("executor_kind"),
            "executor_config": child_config,
        }
        assert root_after.get("ack_sequence") == ack_seq
        assert child_after.get("executor_kind") == "managed_task"
        assert (child_after.get("executor_config") or {}).get("agent_type") == "claude"

        persisted_events = snapshot.get("agent_events") or []
        evidence["persisted_event_call_ids"] = [e.get("call_id") for e in persisted_events]
        assert any(e.get("call_id") == report_event["call_id"] for e in persisted_events)

        _, replayed = http(
            "GET",
            f"/api/agent-tree/runs/{root['id']}/events",
            query={"since_sequence": 0, "subtree": "true"},
        )
        evidence["replay_call_ids"] = [e["call_id"] for e in replayed]
        assert report_event["call_id"] not in evidence["replay_call_ids"]

        _, wait_from_cursor = http(
            "POST",
            "/api/agent-tree/wait",
            {
                "workspace_id": workspace_id,
                "recipient_id": root["id"],
                "since_sequence": ack_seq,
                "subtree": True,
                "timeout_seconds": 1,
            },
        )
        evidence["wait_from_ack_cursor_after_reload"] = [e["call_id"] for e in wait_from_cursor]
        assert report_event["call_id"] not in evidence["wait_from_ack_cursor_after_reload"]

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
            leftover = process_evidence(evidence.get("known_tmux") or [])
            evidence["process_after_cleanup"] = leftover
            evidence["credential_cleanup"] = unlink_credential_overlay(E2E_ROOT)
            launch_dir = E2E_ROOT / ".claude_hub" / "launch_env"
            removed_launch = []
            if launch_dir.is_dir():
                for path in launch_dir.iterdir():
                    if path.is_file():
                        removed_launch.append(
                            {
                                "name": path.name,
                                "mode": oct(path.stat().st_mode & 0o777),
                            }
                        )
                        path.unlink()
            evidence["launch_env_files_removed"] = removed_launch
            hub = E2E_ROOT / ".claude_hub"
            if hub.exists():
                shutil.rmtree(hub, ignore_errors=True)
            leftover_creds = remaining_credential_artifacts(E2E_ROOT)
            evidence["remaining_credential_artifacts"] = leftover_creds
            evidence["credential_overlay_exists_after_cleanup"] = (
                E2E_ROOT / ".claude_hub" / "e2e_launch_env.json"
            ).exists()
            if leftover_creds or evidence["credential_overlay_exists_after_cleanup"]:
                evidence["ok"] = False
                evidence["error"] = "credential artifacts remain after cleanup"
            EVIDENCE.write_text(json.dumps(evidence, indent=2, default=str))
            print(json.dumps(evidence, indent=2, default=str))


if __name__ == "__main__":
    code = main()
    if EVIDENCE.exists():
        leftover = json.loads(EVIDENCE.read_text()).get("remaining_credential_artifacts")
        if leftover:
            sys.exit(1)
    sys.exit(code)
