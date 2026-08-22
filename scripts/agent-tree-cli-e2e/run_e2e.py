#!/usr/bin/env python3
"""Isolated Agent Tree CLI E2E.

Setup/observe via REST. Parent spawn/wait/ack/runs/events go through
``claude-hub agent-tree``. The managed Claude CLI POSTs the child report.
Never call POST /api/agent-tree or POST /reports from this harness.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

E2E_HELPERS = Path(__file__).resolve().parents[1] / "agent-tree-e2e"
sys.path.insert(0, str(E2E_HELPERS))

import run_e2e as base  # noqa: E402

EVIDENCE = base.E2E_ROOT / "cli_e2e_evidence.json"


def http(
    method: str,
    path: str,
    body: dict | None = None,
    query: dict | None = None,
    timeout: int = 60,
):
    if method == "POST" and path.startswith("/api/agent-tree"):
        raise RuntimeError(f"CLI E2E must not raw POST {path}")
    return base.http(method, path, body=body, query=query, timeout=timeout)


def tree_cli(*args: str, timeout: int = 90) -> tuple[object, str]:
    python = Path(os.environ.get("CLAUDE_HUB_E2E_PYTHON", sys.executable))
    cmd = [
        str(python),
        "-m",
        "claude_hub.cli",
        "--json",
        "--base-url",
        base.BASE,
        "agent-tree",
        *args,
    ]
    env = os.environ.copy()
    env.update(base.NOPROXY)
    env["PYTHONPATH"] = str(base.BACKEND)
    env["PYTHONUNBUFFERED"] = "1"
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(base.BACKEND),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"claude-hub agent-tree {' '.join(args)} failed "
            f"({completed.returncode}): {completed.stderr}\n{completed.stdout}"
        )
    payload = json.loads(completed.stdout) if completed.stdout.strip() else None
    return payload, completed.stderr


def main() -> int:
    evidence: dict = {
        "port": base.PORT,
        "home": str(base.E2E_ROOT),
        "repo": str(base.REPO),
        "base_url": base.BASE,
        "parent_surface": "claude-hub agent-tree",
        "report_source": "managed_cli_observed",
        "harness_injected_report": False,
        "live_8173_touched": False,
        "steps": [],
    }
    backend = None
    workspace_id = None
    try:
        if base.PORT == 8173:
            raise RuntimeError("CLI E2E must not use live port 8173")
        base.E2E_ROOT.mkdir(parents=True, exist_ok=True)
        evidence["pre_killed_tmux"] = base.kill_e2e_tmux()
        launch_preset, launch_env = base.install_isolated_launch_env(base.E2E_ROOT)
        evidence["launch_env_preset"] = launch_preset
        evidence["credentials_in_memory_only"] = True
        evidence["credential_overlay_exists_after_install"] = (
            base.E2E_ROOT / ".claude_hub" / "e2e_launch_env.json"
        ).exists()
        if evidence["credential_overlay_exists_after_install"]:
            raise RuntimeError("harness must not persist e2e_launch_env.json")
        evidence["launch_env_has_auth"] = bool(
            launch_env.get("ANTHROPIC_AUTH_TOKEN") or launch_env.get("ANTHROPIC_API_KEY")
        )
        if not (base.REPO / ".git").exists():
            subprocess.run(["git", "init"], cwd=base.REPO, check=True, capture_output=True)
            (base.REPO / "README.md").write_text("isolated agent-tree cli e2e\n")
            (base.REPO / "CLAUDE.md").write_text(
                "E2E only. Do not edit files. Immediately POST one report "
                "with message E2E_CHILD_REPORT using the Hub report curl, then stop.\n"
            )
            subprocess.run(
                ["git", "add", "README.md"],
                cwd=base.REPO,
                check=True,
                capture_output=True,
            )
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
                cwd=base.REPO,
                check=True,
                capture_output=True,
            )

        backend = base.start_backend(base.E2E_ROOT, launch_env)
        base.wait_health()
        evidence["backend_pid"] = backend.pid
        evidence["steps"].append({"start_backend": {"pid": backend.pid}})

        _, workspace = http(
            "POST",
            "/api/workspaces",
            {
                "name": "agent-tree-cli-e2e",
                "path": str(base.REPO),
                "session_prefix": "e2e-cli",
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
            roots, _ = tree_cli("roots", workspace_id)
            return roots[0] if roots else None

        root = base.wait_until("resident root run via CLI", _root, timeout=40)
        evidence["root_run_id"] = root["id"]
        evidence["resident_context_ref"] = root.get("context_ref")
        http("PATCH", f"/api/workspaces/{workspace_id}", {"resident_agent_paused": True})

        spawn_call_id = "e2e-cli-spawn-child-1"
        spawn_args = [
            "spawn",
            workspace_id,
            root["id"],
            "--message",
            (
                "Throwaway E2E. Do not edit files or implement product code. "
                "Immediately POST one report via the Report endpoint curl in "
                "this assignment (http://127.0.0.1:"
                f"{base.PORT}/api/workspaces/sessions/<session>/reports). "
                "Set message, message_en, and message_zh to exactly "
                "E2E_CHILD_REPORT. Reuse the provided call_id. Then stop."
            ),
            "--title",
            "e2e cli child",
            "--agent-type",
            "claude",
            "--solo-mode",
            "--call-id",
            spawn_call_id,
        ]
        for key, value in launch_env.items():
            spawn_args.extend(["--env", f"{key}={value}"])
        child, spawn_stderr = tree_cli(*spawn_args, timeout=90)
        evidence["child_run_id"] = child["id"]
        evidence["child_context_ref"] = child.get("context_ref")
        evidence["spawn_call_id"] = spawn_call_id
        evidence["spawn_stderr_has_call_id"] = f"call_id={spawn_call_id}" in spawn_stderr
        evidence["steps"].append({"cli_spawn": {"run_id": child["id"]}})

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

        bound = base.wait_until("child managed session", _child_session, timeout=60)
        session = bound["session"]
        task = bound["task"]
        evidence["child_session_id"] = session["id"]
        evidence["child_task_id"] = task["id"]
        evidence["child_tmux"] = session.get("tmux_session")
        known_tmux = [session.get("tmux_session")] if session.get("tmux_session") else []
        _, board = http("GET", f"/api/workspaces/{workspace_id}/board")
        known_tmux.extend(
            s.get("tmux_session") for s in board.get("sessions", []) if s.get("tmux_session")
        )
        known_tmux = [name for name in dict.fromkeys(known_tmux) if name]
        evidence["known_tmux"] = known_tmux
        evidence["process_after_spawn"] = base.process_evidence(known_tmux)

        def _is_cli_report(report: dict) -> bool:
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
            _, reports = http("GET", f"/api/workspaces/{workspace_id}/tasks/{task['id']}/reports")
            owned = [r for r in reports if _is_cli_report(r)]
            return owned[0] if owned else None

        created = None
        started_wait = time.time()
        captured_early = False
        while time.time() - started_wait < base.REPORT_WAIT_SECONDS:
            created = _cli_report()
            if created:
                break
            if not captured_early and time.time() - started_wait >= 20:
                evidence["tmux_while_waiting"] = base.capture_tmux(session.get("tmux_session"))
                captured_early = True
            time.sleep(2.0)
        if not created:
            evidence["tmux_on_timeout"] = base.capture_tmux(session.get("tmux_session"))
            raise TimeoutError("timed out waiting for managed CLI child report")
        evidence["cli_report_id"] = created["id"]
        evidence["cli_report_call_id"] = created.get("call_id")
        evidence["cli_report_state"] = created.get("state")
        evidence["cli_report_message"] = created.get("message")
        assert created["session_id"] == session["id"]

        def _report_event():
            events, _ = tree_cli("events", root["id"], "--since-sequence", "0", "--subtree")
            bridged = [e for e in events if e.get("call_id") == f"report:{created['id']}"]
            return bridged[-1] if bridged else None

        report_event = base.wait_until("bridged report event via CLI", _report_event, timeout=15)
        evidence["report_event"] = {
            "id": report_event.get("id"),
            "call_id": report_event.get("call_id"),
            "sequence": report_event.get("sequence"),
            "type": report_event.get("type"),
        }

        cursor_before = int(report_event["sequence"]) - 1
        waited, wait_stderr = tree_cli(
            "wait",
            workspace_id,
            root["id"],
            "--since-sequence",
            str(cursor_before),
            "--timeout-seconds",
            "5",
            "--ack",
            timeout=20,
        )
        evidence["wait_call_ids"] = [e["call_id"] for e in waited]
        assert any(e["call_id"] == report_event["call_id"] for e in waited), evidence[
            "wait_call_ids"
        ]

        ack_seq = max(int(e["sequence"]) for e in waited)
        ack_obj = None
        for line in wait_stderr.splitlines():
            stripped = line.strip()
            if stripped.startswith("{") and "acked_sequence" in stripped:
                ack_obj = json.loads(stripped)
        evidence["wait_ack_stderr"] = ack_obj
        assert ack_obj == {"acked_sequence": ack_seq}

        runs_after_ack, _ = tree_cli("runs", workspace_id)
        root_acked = next(item for item in runs_after_ack if item["id"] == root["id"])
        evidence["ack_sequence"] = root_acked.get("ack_sequence")
        assert root_acked.get("ack_sequence") == ack_seq

        after_ack, _ = tree_cli(
            "wait",
            workspace_id,
            root["id"],
            "--since-sequence",
            str(ack_seq),
            "--timeout-seconds",
            "1",
            timeout=15,
        )
        evidence["wait_after_ack"] = [e["call_id"] for e in after_ack]
        assert after_ack == []
        events_after_ack, _ = tree_cli(
            "events", root["id"], "--since-sequence", str(ack_seq), "--subtree"
        )
        evidence["events_after_ack"] = [e["call_id"] for e in events_after_ack]
        assert report_event["call_id"] not in evidence["events_after_ack"]
        assert events_after_ack == []

        try:
            tree_cli(
                "interrupt",
                workspace_id,
                child["id"],
                "--reason",
                "e2e-observe-done",
                "--call-id",
                "e2e-cli-interrupt-1",
            )
            evidence["interrupted_after_observe"] = True
        except Exception as exc:  # noqa: BLE001
            evidence["interrupt_after_observe_error"] = str(exc)

        state_file = base.E2E_ROOT / ".claude_hub" / "workspaces" / workspace_id / "state.json"
        snapshot = json.loads(state_file.read_text())
        evidence["state_event_count_before_reload"] = len(snapshot.get("agent_events") or [])

        base.stop_backend(backend)
        backend = None
        time.sleep(1)
        backend = base.start_backend(base.E2E_ROOT, launch_env)
        base.wait_health()
        evidence["backend_pid_after_reload"] = backend.pid

        runs_after, _ = tree_cli("runs", workspace_id)
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

        replayed_from_zero, _ = tree_cli("events", root["id"], "--since-sequence", "0", "--subtree")
        replayed_from_ack, _ = tree_cli(
            "events", root["id"], "--since-sequence", str(ack_seq), "--subtree"
        )
        evidence["replay_call_ids"] = [e["call_id"] for e in replayed_from_zero]
        evidence["events_from_ack_cursor_after_reload"] = [e["call_id"] for e in replayed_from_ack]
        assert report_event["call_id"] not in evidence["replay_call_ids"]
        assert report_event["call_id"] not in evidence["events_from_ack_cursor_after_reload"]

        wait_from_cursor, _ = tree_cli(
            "wait",
            workspace_id,
            root["id"],
            "--since-sequence",
            str(ack_seq),
            "--timeout-seconds",
            "1",
            timeout=15,
        )
        evidence["wait_from_ack_cursor_after_reload"] = [e["call_id"] for e in wait_from_cursor]
        assert report_event["call_id"] not in evidence["wait_from_ack_cursor_after_reload"]
        later_ids = [
            call_id
            for call_id in (
                evidence["events_from_ack_cursor_after_reload"]
                + evidence["wait_from_ack_cursor_after_reload"]
            )
            if call_id and call_id != report_event["call_id"]
        ]
        evidence["later_events_after_ack_cursor"] = later_ids
        assert later_ids, (
            "ACK-cursor replay must observe later events so the missing "
            "ACKed report is not a vacuous empty-stream pass"
        )

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
            base.stop_backend(backend)
            evidence["killed_tmux"] = base.kill_e2e_tmux(evidence.get("known_tmux") or [])
            leftover = base.process_evidence(evidence.get("known_tmux") or [])
            evidence["process_after_cleanup"] = leftover
            evidence["credential_cleanup"] = base.unlink_credential_overlay(base.E2E_ROOT)
            launch_dir = base.E2E_ROOT / ".claude_hub" / "launch_env"
            if launch_dir.is_dir():
                for path in launch_dir.iterdir():
                    if path.is_file():
                        path.unlink()
            hub = base.E2E_ROOT / ".claude_hub"
            if hub.exists():
                import shutil

                shutil.rmtree(hub, ignore_errors=True)
            leftover_creds = base.remaining_credential_artifacts(base.E2E_ROOT)
            evidence["remaining_credential_artifacts"] = leftover_creds
            evidence["credential_overlay_exists_after_cleanup"] = (
                base.E2E_ROOT / ".claude_hub" / "e2e_launch_env.json"
            ).exists()
            if leftover_creds or evidence["credential_overlay_exists_after_cleanup"]:
                evidence["ok"] = False
                evidence["error"] = "credential artifacts remain after cleanup"
            EVIDENCE.write_text(json.dumps(evidence, indent=2, default=str))
            print(json.dumps(evidence, indent=2, default=str))


if __name__ == "__main__":
    sys.exit(main())
