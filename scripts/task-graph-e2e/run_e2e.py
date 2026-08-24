#!/usr/bin/env python3
"""AC12: throwaway Task Graph E2E — ``claude-hub task`` + workspace tasks API only.

Reuses ``scripts/agent-tree-e2e/run_e2e.py`` for start/stop backend, launch env,
and tmux/credential cleanup only. Never calls ``/api/agent-tree/*`` or AgentRun
assertions.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

E2E_HELPERS = Path(__file__).resolve().parents[1] / "agent-tree-e2e"
_HARNESS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_HELPERS))
sys.path.insert(0, str(_HARNESS_DIR))

import git_provenance as gp  # noqa: E402
import run_e2e as base  # noqa: E402

_SUITE_ROOT = Path(
    os.environ.get(
        "CLAUDE_HUB_E2E_SUITE_ROOT",
        str(Path(os.environ["CLAUDE_HUB_E2E_HOME"]).parent),
    )
)
EVIDENCE = _SUITE_ROOT / "evidence.json"
REPORT_WAIT_SECONDS = float(os.environ.get("CLAUDE_HUB_E2E_REPORT_WAIT", "360"))
BACKEND = Path(os.environ["CLAUDE_HUB_E2E_BACKEND"])

_WORKSPACE_POST_PREFIX = "/api/workspaces"
_VERDICT_EVENT_TYPES = frozenset(
    {"review_passed", "review_failed", "review_needs_input"}
)
_VERDICT_TYPES = frozenset({"review_passed", "review_failed", "review_needs_input"})
_TERMINAL_REPORT_STATES = _VERDICT_TYPES


def http(
    method: str,
    path: str,
    body: dict | None = None,
    query: dict | None = None,
    timeout: int = 60,
):
    if method == "POST" and "/reports" in path:
        raise RuntimeError(f"harness must not POST reports: {path}")
    if method == "POST" and path.startswith("/api/agent-tree"):
        raise RuntimeError(f"Task Graph E2E must not call agent-tree: {path}")
    if method == "POST" and not path.startswith(_WORKSPACE_POST_PREFIX):
        raise RuntimeError(f"unexpected harness POST {path}")
    return base.http(method, path, body=body, query=query, timeout=timeout)


def task_cli(*args: str, timeout: int = 120) -> Any:
    python = Path(os.environ.get("CLAUDE_HUB_E2E_PYTHON", sys.executable))
    cmd = [
        str(python),
        "-m",
        "claude_hub.cli",
        "--json",
        "--base-url",
        base.BASE,
        "task",
        *args,
    ]
    env = os.environ.copy()
    env.update(base.NOPROXY)
    env.pop("VIRTUAL_ENV", None)
    env["PYTHONPATH"] = str(BACKEND)
    env["PYTHONUNBUFFERED"] = "1"
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(BACKEND),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"claude-hub task {' '.join(args)} failed "
            f"({completed.returncode}): {completed.stderr}\n{completed.stdout}"
        )
    stdout = completed.stdout.strip()
    if not stdout:
        return None
    return json.loads(stdout)


def wait_until(desc: str, fn, timeout: float = 45.0, interval: float = 0.5):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {desc}; last={last!r}")


def _event_type(event: dict) -> str:
    return str(event.get("type", "")).lower()


def _target_event_snapshot(event: dict) -> dict:
    return {
        "sequence": event.get("sequence"),
        "type": event.get("type"),
        "call_id": event.get("call_id"),
        "task_id": event.get("task_id"),
        "actor_session_id": event.get("actor_session_id"),
        "actor_role": event.get("actor_role"),
        "review_cycle": event.get("review_cycle"),
        "compat_run_id": event.get("compat_run_id"),
    }


def _is_worker_report(event: dict, child_id: str, worker_session_id: str) -> bool:
    return (
        _event_type(event) == "report"
        and event.get("task_id") == child_id
        and str(event.get("actor_role", "")).lower() == "worker"
        and event.get("actor_session_id") == worker_session_id
    )


def _is_review_started(event: dict, child_id: str, reviewer_session_id: str) -> bool:
    return (
        _event_type(event) == "review_started"
        and event.get("task_id") == child_id
        and str(event.get("actor_role", "")).lower() == "reviewer"
        and event.get("actor_session_id") == reviewer_session_id
    )


def _payload_state(event: dict) -> str:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("state") or payload.get("report_state") or "").lower()


def _is_verdict(event: dict, child_id: str, reviewer_session_id: str) -> bool:
    """Terminal verdict only: explicit TaskEvent type or legacy payload state.

    Must not treat ``review_decision`` (including ``auto``) or ``review_started`` as a verdict.
    """

    if event.get("task_id") != child_id:
        return False
    if str(event.get("actor_role", "")).lower() != "reviewer":
        return False
    if event.get("actor_session_id") != reviewer_session_id:
        return False

    event_type = _event_type(event)
    if event_type in _VERDICT_EVENT_TYPES:
        return True

    if event_type == "report":
        return _payload_state(event) in _VERDICT_TYPES

    return False


def _is_verdict_task_event(
    event: dict, child_id: str, reviewer_session_id: str
) -> bool:
    return _is_verdict(event, child_id, reviewer_session_id)


def _classify_target_event(
    event: dict,
    child_id: str,
    worker_session_id: str,
    reviewer_session_id: str,
) -> str | None:
    if _is_worker_report(event, child_id, worker_session_id):
        return "worker_report"
    if _is_review_started(event, child_id, reviewer_session_id):
        return "review_started"
    if _is_verdict_task_event(event, child_id, reviewer_session_id):
        return "verdict"
    return None


def _filter_target_events(
    events: list[dict],
    child_id: str,
    worker_session_id: str,
    reviewer_session_id: str,
) -> list[dict]:
    matched: list[dict] = []
    for event in events:
        if _classify_target_event(
            event, child_id, worker_session_id, reviewer_session_id
        ):
            matched.append(event)
    return sorted(matched, key=lambda item: int(item["sequence"]))


def _assert_target_event_fields(event: dict, label: str) -> None:
    assert event.get("task_id"), f"{label} missing task_id: {event}"
    assert event.get("actor_session_id"), f"{label} missing actor_session_id: {event}"
    assert event.get("actor_role"), f"{label} missing actor_role: {event}"
    assert (
        event.get("review_cycle") is not None
    ), f"{label} missing review_cycle: {event}"
    assert event.get("sequence") is not None, f"{label} missing sequence: {event}"
    assert event.get("call_id"), f"{label} missing call_id: {event}"


def _assert_no_same_cycle_review_started_after_verdict(
    events: list[dict],
    child_id: str,
    reviewer_session_id: str,
) -> None:
    """Pre-reload guard: no duplicate review_started at the verdict cycle."""

    ordered = sorted(events, key=lambda item: int(item["sequence"]))
    verdict_seq: int | None = None
    verdict_cycle: Any = None
    for event in ordered:
        if _is_verdict_task_event(event, child_id, reviewer_session_id):
            verdict_seq = int(event["sequence"])
            verdict_cycle = event.get("review_cycle")
            break
    assert (
        verdict_seq is not None
    ), "missing terminal verdict TaskEvent in pre-reload set"
    for event in ordered:
        seq = int(event["sequence"])
        if seq <= verdict_seq:
            continue
        if (
            _is_review_started(event, child_id, reviewer_session_id)
            and event.get("review_cycle") == verdict_cycle
        ):
            raise AssertionError(
                "same-cycle review_started after terminal verdict: "
                f"{_target_event_snapshot(event)!r}"
            )


def _assert_unique_target_sequences_and_call_ids(
    events: list[dict], *, label: str
) -> None:
    sequences: set[int] = set()
    call_ids: set[str] = set()
    for event in events:
        seq = int(event["sequence"])
        call_id = str(event.get("call_id") or "")
        assert seq not in sequences, f"{label}: duplicate sequence {seq}"
        assert call_id, f"{label}: missing call_id on seq={seq}"
        assert call_id not in call_ids, f"{label}: duplicate call_id {call_id!r}"
        sequences.add(seq)
        call_ids.add(call_id)


def _assert_three_target_categories(
    events: list[dict],
    child_id: str,
    worker_session_id: str,
    reviewer_session_id: str,
) -> dict[str, list[dict]]:
    targets = _filter_target_events(
        events, child_id, worker_session_id, reviewer_session_id
    )
    by_kind: dict[str, list[dict]] = {
        "worker_report": [],
        "review_started": [],
        "verdict": [],
    }
    for event in targets:
        kind = _classify_target_event(
            event, child_id, worker_session_id, reviewer_session_id
        )
        assert kind is not None
        by_kind[kind].append(event)
        _assert_target_event_fields(event, kind)
    missing = [kind for kind, rows in by_kind.items() if not rows]
    assert (
        not missing
    ), f"parent subtree missing target categories: {missing}; events={targets!r}"
    return by_kind


def _get_parent_events_api(
    workspace_id: str,
    parent_id: str,
    *,
    since_sequence: int = 0,
) -> list[dict]:
    _, payload = http(
        "GET",
        f"/api/workspaces/{workspace_id}/tasks/{parent_id}/events",
        query={"subtree": "true", "since_sequence": str(since_sequence)},
    )
    return payload if isinstance(payload, list) else []


def _target_snapshots(events: list[dict]) -> list[dict]:
    return [_target_event_snapshot(event) for event in events]


def _reload_extra_record(event: dict) -> dict:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return {
        "sequence": event.get("sequence"),
        "call_id": event.get("call_id"),
        "type": event.get("type"),
        "task_id": event.get("task_id"),
        "snapshot": _target_event_snapshot(event),
        "payload": payload,
        "report_id": event.get("report_id") or payload.get("report_id"),
        "actor_session_id": event.get("actor_session_id"),
        "actor_role": event.get("actor_role"),
    }


def _original_target_registry(events: list[dict]) -> dict[str, Any]:
    by_call_id: dict[str, dict] = {}
    by_sequence: dict[int, str] = {}
    for event in events:
        call_id = event.get("call_id")
        sequence = int(event["sequence"])
        assert call_id, f"original target missing call_id: {event!r}"
        assert (
            call_id not in by_call_id
        ), f"duplicate original call_id before reload: {call_id!r}"
        by_call_id[str(call_id)] = _target_event_snapshot(event)
        by_sequence[sequence] = str(call_id)
    max_sequence = max(by_sequence) if by_sequence else 0
    return {
        "by_call_id": by_call_id,
        "by_sequence": {str(k): v for k, v in by_sequence.items()},
        "max_sequence": max_sequence,
        "snapshots": _target_snapshots(events),
        "records": [_reload_extra_record(event) for event in events],
    }


def _assert_original_targets_persisted(
    registry: dict[str, Any],
    after_events: list[dict],
    *,
    label: str,
) -> list[dict]:
    """Each pre-reload target call_id appears exactly once with an identical snapshot."""

    original_call_ids = set(registry["by_call_id"])
    seen_original: set[str] = set()
    extras: list[dict] = []

    for event in after_events:
        call_id = str(event.get("call_id") or "")
        snap = _target_event_snapshot(event)
        if call_id in original_call_ids:
            assert (
                call_id not in seen_original
            ), f"{label}: duplicate original call_id after reload: {call_id!r}"
            seen_original.add(call_id)
            expected = registry["by_call_id"][call_id]
            assert snap == expected, (
                f"{label}: original call_id {call_id!r} snapshot changed: "
                f"before={expected!r} after={snap!r}"
            )
        else:
            extras.append(_reload_extra_record(event))

    missing = original_call_ids - seen_original
    assert (
        not missing
    ), f"{label}: original call_ids missing after reload: {sorted(missing)!r}"
    return extras


def _lookup_report_by_call_id(
    workspace_id: str,
    task_id: str,
    call_id: str,
) -> dict | None:
    _, reports = http("GET", f"/api/workspaces/{workspace_id}/tasks/{task_id}/reports")
    for report in reports if isinstance(reports, list) else []:
        if str(report.get("call_id") or "") == call_id:
            return report
    return None


def _confirm_reload_extra_sources(
    workspace_id: str,
    child_id: str,
    extras: list[dict],
) -> list[dict]:
    confirmed: list[dict] = []
    for extra in extras:
        call_id = str(extra.get("call_id") or "")
        source: dict[str, Any] = {"call_id": call_id, "sequence": extra.get("sequence")}
        if call_id.startswith("report:"):
            report = _lookup_report_by_call_id(workspace_id, child_id, call_id)
            if report:
                source["report_lookup"] = {
                    "id": report.get("id"),
                    "state": report.get("state"),
                    "session_id": report.get("session_id"),
                    "review_decision": report.get("review_decision"),
                    "message_prefix": (report.get("message") or "")[:120],
                }
            else:
                source["report_lookup"] = None
        confirmed.append({**extra, "source_confirmation": source})
    return confirmed


def _is_terminal_reviewer_report(report: dict | None) -> bool:
    if report is None:
        return False
    state = str(report.get("state") or "").lower()
    if state in {"review_started"}:
        return False
    return state in _TERMINAL_REPORT_STATES


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


def _reviewer_report(
    workspace_id: str, child_id: str, reviewer_session_id: str
) -> dict | None:
    """Return the latest reviewer report only when state is a terminal verdict."""

    _, reports = http("GET", f"/api/workspaces/{workspace_id}/tasks/{child_id}/reports")
    owned = [
        r
        for r in (reports if isinstance(reports, list) else [])
        if r.get("session_id") == reviewer_session_id
        and (r.get("call_id") or "").strip()
    ]
    terminal = [
        r for r in owned if str(r.get("state") or "").lower() in _TERMINAL_REPORT_STATES
    ]
    return terminal[-1] if terminal else None


def _wait_until_target_categories(
    workspace_id: str,
    parent_id: str,
    cursor: int,
    child_id: str,
    worker_session_id: str,
    reviewer_session_id: str,
) -> tuple[list[dict], dict[str, list[dict]]] | None:
    all_events = _get_parent_events_api(workspace_id, parent_id, since_sequence=cursor)
    targets = _filter_target_events(
        all_events,
        child_id,
        worker_session_id,
        reviewer_session_id,
    )
    try:
        by_kind = _assert_three_target_categories(
            targets,
            child_id,
            worker_session_id,
            reviewer_session_id,
        )
    except AssertionError:
        return None
    return targets, by_kind


def _compat_run_id(task: dict) -> str:
    return task.get("agent_run_id") or task["id"]


def _cold_restart(
    backend: subprocess.Popen | None,
    launch_env: dict[str, str],
) -> subprocess.Popen:
    base.stop_backend(backend)
    time.sleep(1)
    proc = base.start_backend(base.E2E_ROOT, launch_env)
    base.wait_health()
    return proc


def _init_e2e_repo() -> None:
    subprocess.run(["git", "init"], cwd=base.REPO, check=True, capture_output=True)
    (base.REPO / "README.md").write_text("isolated task-graph e2e\n")
    (base.REPO / "CLAUDE.md").write_text(
        "Throwaway Task Graph E2E repo.\n"
        "Worker: POST one ready_for_review report with message E2E_CHILD_REPORT, then stop.\n"
        "Reviewer: POST review_started, then immediately POST review_passed with "
        "message E2E_REVIEW_PASSED, then stop. Do not edit files.\n"
    )
    subprocess.run(
        ["git", "add", "README.md", "CLAUDE.md"],
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


def main() -> int:
    sys.path.insert(0, str(BACKEND))
    from claude_hub.models import WorkspaceTask  # noqa: WPS433
    from claude_hub.services import task_graph as tg  # noqa: WPS433

    gp.forbid_git_provenance_env_overrides()
    git_pre = gp.read_git_provenance(gp.delivery_source_root())
    gp.require_clean_provenance(git_pre)

    evidence: dict = {
        "port": base.PORT,
        "home": str(base.E2E_ROOT),
        "repo": str(base.REPO),
        "suite_root": str(_SUITE_ROOT),
        "evidence_path": str(EVIDENCE),
        "branch": git_pre["branch"],
        "git_sha": git_pre["git_sha"],
        "dirty": git_pre["dirty"],
        "source_root": git_pre["source_root"],
        "git_provenance_pre": git_pre,
        "driver": "claude-hub task + workspace tasks API",
        "harness_injected_report": False,
        "forbidden_surfaces": [
            "/api/agent-tree/spawn",
            "/api/agent-tree/wait",
            "/api/agent-tree/ack",
            "/api/agent-tree/interrupt",
            "AgentRun assertions",
        ],
    }
    backend = None
    workspace_id = None
    known_tmux: list[str] = []
    launch_env: dict[str, str] = {}
    try:
        if base.PORT == 8173:
            raise RuntimeError("Task Graph E2E must not use live port 8173")
        base.E2E_ROOT.mkdir(parents=True, exist_ok=True)
        base.REPO.mkdir(parents=True, exist_ok=True)
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
            launch_env.get("ANTHROPIC_AUTH_TOKEN")
            or launch_env.get("ANTHROPIC_API_KEY")
        )

        if not (base.REPO / ".git").exists():
            _init_e2e_repo()

        backend = base.start_backend(base.E2E_ROOT, launch_env)
        base.wait_health()
        evidence["backend_pid"] = backend.pid

        _, workspace = http(
            "POST",
            "/api/workspaces",
            {
                "name": "task-graph-e2e",
                "path": str(base.REPO),
                "session_prefix": "e2e",
                "resident_agent_enabled": False,
            },
        )
        workspace_id = workspace["id"]
        evidence["workspace_id"] = workspace_id

        parent = task_cli(
            "create",
            workspace_id,
            "--title",
            "e2e parent",
            "--prompt",
            "Parent supervisor task for Task Graph E2E.",
            "--agent-type",
            "claude",
            "--task-mode",
            "reviewed",
        )
        parent_id = parent["id"]
        evidence["parent_task_id"] = parent_id
        parent_consumer_key = tg.make_task_consumer_key(parent_id)
        evidence["parent_consumer_key"] = parent_consumer_key
        assert parent_consumer_key == f"task:{parent_id}"

        worker_call_id = "e2e-worker-report-1"
        child = task_cli(
            "create",
            workspace_id,
            "--title",
            "e2e child",
            "--prompt",
            (
                "Throwaway Task Graph E2E worker. Do not edit repo files. "
                f"Immediately POST one report to http://127.0.0.1:{base.PORT}/api/workspaces/sessions/"
                "<your-session-id>/reports with task_id set, call_id "
                f"{worker_call_id!r}, state ready_for_review, and message/message_en/message_zh "
                "exactly E2E_CHILD_REPORT. Then stop."
            ),
            "--agent-type",
            "claude",
            "--task-mode",
            "reviewed",
            "--parent-task-id",
            parent_id,
        )
        child_id = child["id"]
        evidence["child_task_id"] = child_id
        child_consumer_key = tg.make_task_consumer_key(child_id)
        evidence["child_consumer_key"] = child_consumer_key
        assert child_consumer_key == f"task:{child_id}"
        assert child.get("parent_task_id") == parent_id

        started = task_cli(
            "start",
            child_id,
            "--payload-json",
            json.dumps({"dispatch_reason": "task-graph-e2e"}),
            timeout=180,
        )
        evidence["child_dispatch_cli"] = "task start"
        evidence["child_start_status"] = started.get("status")

        def _worker_bound():
            task = _board_task(workspace_id, child_id)
            if not task or not task.get("session_id"):
                return None
            session_id = task["session_id"]
            _, board = http("GET", f"/api/workspaces/{workspace_id}/board")
            session = next(
                (s for s in board.get("sessions", []) if s["id"] == session_id), None
            )
            if not session:
                return None
            return {"task": task, "session": session}

        bound = wait_until("child worker session", _worker_bound, timeout=90)
        worker_session = bound["session"]
        worker_session_id = worker_session["id"]
        evidence["worker_session_id"] = worker_session_id
        evidence["worker_tmux"] = worker_session.get("tmux_session")
        if worker_session.get("tmux_session"):
            known_tmux.append(worker_session["tmux_session"])

        def _worker_report():
            _, reports = http(
                "GET", f"/api/workspaces/{workspace_id}/tasks/{child_id}/reports"
            )
            report = _session_report(child_id, worker_session_id, reports)
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
            session = next(
                (s for s in board.get("sessions", []) if s["id"] == reviewer_id), None
            )
            if not session:
                return None
            return {"task": task, "session": session}

        reviewer_bound = wait_until("reviewer session", _reviewer_bound, timeout=120)
        reviewer_session = reviewer_bound["session"]
        reviewer_session_id = reviewer_session["id"]
        evidence["reviewer_session_id"] = reviewer_session_id
        if reviewer_session.get("tmux_session"):
            known_tmux.append(reviewer_session["tmux_session"])
        evidence["known_tmux"] = list(dict.fromkeys(n for n in known_tmux if n))

        subtree = task_cli("tree", workspace_id, parent_id)
        evidence["parent_subtree_ids"] = [item["id"] for item in subtree]
        assert child_id in evidence["parent_subtree_ids"]

        parent_board = _board_task(workspace_id, parent_id)
        child_board = _board_task(workspace_id, child_id)
        cursor = int(parent_board.get("consumer_ack_sequence") or 0)
        evidence["parent_consumer_ack_cursor"] = cursor
        evidence["child_consumer_ack_cursor"] = int(
            (child_board or {}).get("consumer_ack_sequence") or 0
        )
        evidence["consumer_key_source"] = {
            "parent": "tg.make_task_consumer_key(parent_task_id)",
            "child": "tg.make_task_consumer_key(child_task_id)",
            "wait_ack_cursor_task": parent_id,
            "note": "TaskEvent API does not expose consumer_key; cursors bind task:<task_id>",
        }

        target_events: list[dict] = []
        target_by_kind: dict[str, list[dict]] = {}
        reviewer_report = None
        wait_deadline = time.time() + REPORT_WAIT_SECONDS
        while time.time() < wait_deadline:
            ready = _wait_until_target_categories(
                workspace_id,
                parent_id,
                cursor,
                child_id,
                worker_session_id,
                reviewer_session_id,
            )
            reviewer_report = _reviewer_report(
                workspace_id, child_id, reviewer_session_id
            )
            if ready is not None and _is_terminal_reviewer_report(reviewer_report):
                target_events, target_by_kind = ready
                verdict_event = next(
                    e
                    for e in target_events
                    if _is_verdict_task_event(e, child_id, reviewer_session_id)
                )
                report_state = str(reviewer_report.get("state") or "").lower()
                assert _event_type(verdict_event) == report_state, (
                    f"verdict TaskEvent type {_event_type(verdict_event)!r} "
                    f"!= reviewer report state {report_state!r}"
                )
                break
            time.sleep(2.0)
        else:
            latest_events = _get_parent_events_api(
                workspace_id, parent_id, since_sequence=cursor
            )
            evidence["mailbox_events_at_timeout"] = _target_snapshots(latest_events)
            latest_report = _reviewer_report(
                workspace_id, child_id, reviewer_session_id
            )
            if latest_report:
                evidence["reviewer_report_at_timeout"] = {
                    "id": latest_report.get("id"),
                    "state": latest_report.get("state"),
                    "review_decision": latest_report.get("review_decision"),
                }
            else:
                _, reports_payload = http(
                    "GET",
                    f"/api/workspaces/{workspace_id}/tasks/{child_id}/reports",
                )
                reports_list = (
                    reports_payload if isinstance(reports_payload, list) else []
                )
                non_terminal = _session_report(
                    child_id, reviewer_session_id, reports_list
                )
                if non_terminal:
                    evidence["reviewer_non_terminal_report_at_timeout"] = {
                        "id": non_terminal.get("id"),
                        "state": non_terminal.get("state"),
                        "review_decision": non_terminal.get("review_decision"),
                    }
            raise TimeoutError(
                "timed out waiting for terminal reviewer report AND verdict TaskEvent "
                "in parent subtree (review_started/auto is not a verdict)"
            )

        evidence["mailbox_target_categories"] = {
            kind: [_target_event_snapshot(event) for event in events]
            for kind, events in target_by_kind.items()
        }

        _assert_unique_target_sequences_and_call_ids(
            target_events, label="pre-reload targets"
        )
        _assert_no_same_cycle_review_started_after_verdict(
            target_events,
            child_id,
            reviewer_session_id,
        )

        evidence["reviewer_report_id"] = reviewer_report["id"]
        evidence["reviewer_report_state"] = reviewer_report.get("state")
        evidence["reviewer_report_decision"] = reviewer_report.get("review_decision")
        assert (
            str(evidence["reviewer_report_state"] or "").lower()
            in _TERMINAL_REPORT_STATES
        )
        assert str(evidence["reviewer_report_state"] or "").lower() != "review_started"

        waited = task_cli(
            "wait",
            workspace_id,
            parent_id,
            "--subtree",
            "--since-sequence",
            str(cursor),
            "--timeout-seconds",
            "10",
            timeout=20,
        )
        waited = waited if isinstance(waited, list) else []
        assert waited, "parent subtree wait returned no TaskMailbox events"
        evidence["parent_wait_count"] = len(waited)
        target_by_kind = _assert_three_target_categories(
            _filter_target_events(
                waited,
                child_id,
                worker_session_id,
                reviewer_session_id,
            ),
            child_id,
            worker_session_id,
            reviewer_session_id,
        )
        evidence["parent_wait_target_categories"] = {
            kind: [_target_event_snapshot(event) for event in events]
            for kind, events in target_by_kind.items()
        }

        parent_detail = task_cli(
            "get", parent_id, "--workspace-id", workspace_id, "--no-reports"
        )
        child_detail = task_cli(
            "get", child_id, "--workspace-id", workspace_id, "--no-reports"
        )
        evidence["parent_compat_run_id_before_reload"] = _compat_run_id(parent_detail)
        evidence["child_compat_run_id_before_reload"] = _compat_run_id(child_detail)
        parent_model = WorkspaceTask.model_validate(parent_detail)
        child_model = WorkspaceTask.model_validate(child_detail)
        assert evidence[
            "parent_compat_run_id_before_reload"
        ] == tg.compat_run_id_for_task(parent_model)
        assert evidence[
            "child_compat_run_id_before_reload"
        ] == tg.compat_run_id_for_task(child_model)

        # Freeze the original target set at verdict time (seq 1..3). Do not re-fetch
        # before reload — late mailbox rows (e.g. reaper follow-ups) may arrive in between.
        original_registry = _original_target_registry(target_events)
        evidence["original_target_registry"] = {
            "by_call_id": original_registry["by_call_id"],
            "by_sequence": original_registry["by_sequence"],
            "max_sequence": original_registry["max_sequence"],
            "snapshots": original_registry["snapshots"],
            "records": original_registry["records"],
        }
        evidence["target_events_before_reload"] = original_registry["snapshots"]

        all_events_before_reload = _get_parent_events_api(
            workspace_id, parent_id, since_sequence=0
        )
        target_before_reload = _filter_target_events(
            all_events_before_reload,
            child_id,
            worker_session_id,
            reviewer_session_id,
        )
        evidence["target_events_observed_before_reload"] = _target_snapshots(
            target_before_reload
        )
        _assert_unique_target_sequences_and_call_ids(
            target_before_reload,
            label="all pre-reload subtree targets",
        )
        _assert_no_same_cycle_review_started_after_verdict(
            target_before_reload,
            child_id,
            reviewer_session_id,
        )

        state_file = (
            base.E2E_ROOT / ".claude_hub" / "workspaces" / workspace_id / "state.json"
        )
        snapshot = json.loads(state_file.read_text())
        evidence["task_event_count_before_reload"] = len(
            snapshot.get("task_events") or []
        )

        backend = _cold_restart(backend, launch_env)
        evidence["backend_pid_after_reload"] = backend.pid

        all_events_after_reload = _get_parent_events_api(
            workspace_id, parent_id, since_sequence=0
        )
        target_after_reload = _filter_target_events(
            all_events_after_reload,
            child_id,
            worker_session_id,
            reviewer_session_id,
        )
        _assert_unique_target_sequences_and_call_ids(
            target_after_reload,
            label="first-cold-reload-targets",
        )
        reload_extras = _assert_original_targets_persisted(
            original_registry,
            target_after_reload,
            label="first cold reload",
        )
        evidence["original_targets_after_first_cold_reload"] = [
            _target_event_snapshot(event)
            for event in target_after_reload
            if str(event.get("call_id") or "") in original_registry["by_call_id"]
        ]
        evidence["reload_extras_after_first_cold"] = reload_extras
        evidence["reload_extras_after_first_cold_sources"] = (
            _confirm_reload_extra_sources(
                workspace_id,
                child_id,
                reload_extras,
            )
        )
        assert not reload_extras, (
            "unexpected TaskMailbox extras after first cold reload: "
            f"{reload_extras!r}"
        )
        evidence["target_events_after_reload"] = _target_snapshots(target_after_reload)

        parent_after = task_cli(
            "get", parent_id, "--workspace-id", workspace_id, "--no-reports"
        )
        child_after = task_cli(
            "get", child_id, "--workspace-id", workspace_id, "--no-reports"
        )
        evidence["parent_compat_run_id_after_reload"] = _compat_run_id(parent_after)
        evidence["child_compat_run_id_after_reload"] = _compat_run_id(child_after)
        assert parent_after.get("id") == parent_id
        assert child_after.get("id") == child_id
        assert (
            evidence["parent_compat_run_id_after_reload"]
            == evidence["parent_compat_run_id_before_reload"]
        )
        assert (
            evidence["child_compat_run_id_after_reload"]
            == evidence["child_compat_run_id_before_reload"]
        )

        ack_seq = int(original_registry["max_sequence"])
        acked = task_cli("ack", workspace_id, parent_id, str(ack_seq))
        evidence["parent_ack_sequence"] = acked.get("consumer_ack_sequence")
        assert acked.get("consumer_ack_sequence") == ack_seq
        evidence["original_ack_max_sequence"] = ack_seq

        backend = _cold_restart(backend, launch_env)
        evidence["backend_pid_after_ack_reload"] = backend.pid

        parent_after_ack = task_cli(
            "get", parent_id, "--workspace-id", workspace_id, "--no-reports"
        )
        child_after_ack = task_cli(
            "get", child_id, "--workspace-id", workspace_id, "--no-reports"
        )
        evidence["parent_ack_after_reload"] = parent_after_ack.get(
            "consumer_ack_sequence"
        )
        evidence["child_ack_after_reload"] = child_after_ack.get(
            "consumer_ack_sequence"
        )
        evidence["parent_compat_run_id_after_ack_reload"] = _compat_run_id(
            parent_after_ack
        )
        evidence["child_compat_run_id_after_ack_reload"] = _compat_run_id(
            child_after_ack
        )
        assert parent_after_ack.get("consumer_ack_sequence") == ack_seq
        assert (
            evidence["parent_compat_run_id_after_ack_reload"]
            == evidence["parent_compat_run_id_before_reload"]
        )
        assert (
            evidence["child_compat_run_id_after_ack_reload"]
            == evidence["child_compat_run_id_before_reload"]
        )

        replay_wait = task_cli(
            "wait",
            workspace_id,
            parent_id,
            "--subtree",
            "--since-sequence",
            str(ack_seq),
            "--timeout-seconds",
            "3",
            timeout=20,
        )
        replay_wait = replay_wait if isinstance(replay_wait, list) else []
        original_call_ids = set(original_registry["by_call_id"])
        replay_records = [_reload_extra_record(event) for event in replay_wait]
        redelivered_original: list[dict] = []
        post_ack_extras: list[dict] = []
        for event in replay_wait:
            seq = int(event["sequence"])
            call_id = str(event.get("call_id") or "")
            if seq <= ack_seq:
                raise AssertionError(
                    f"re-delivered event at or before ack cursor: seq={seq} ack={ack_seq} "
                    f"call_id={call_id!r}"
                )
            if call_id in original_call_ids:
                redelivered_original.append(_reload_extra_record(event))
            else:
                post_ack_extras.append(_reload_extra_record(event))

        assert not redelivered_original, (
            "re-delivered original target call_id after second cold reload: "
            f"{redelivered_original!r}"
        )

        evidence["wait_after_ack_reload"] = replay_records
        evidence["wait_after_ack_reload_extras"] = post_ack_extras
        if post_ack_extras:
            evidence["wait_after_ack_reload_extra_sources"] = (
                _confirm_reload_extra_sources(
                    workspace_id,
                    child_id,
                    post_ack_extras,
                )
            )
        assert not post_ack_extras, (
            "unexpected TaskMailbox extras after ack cold reload wait: "
            f"{post_ack_extras!r}"
        )
        evidence["wait_after_reload"] = [
            {"call_id": item.get("call_id"), "sequence": item.get("sequence")}
            for item in replay_records
        ]

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
            evidence["killed_tmux"] = base.kill_e2e_tmux(
                known_tmux or evidence.get("known_tmux")
            )
            home_path = base.E2E_ROOT
            repo_path = base.REPO
            evidence["HOME_EXISTS_before_cleanup"] = home_path.exists()
            evidence["REPO_EXISTS_before_cleanup"] = repo_path.exists()
            if home_path.exists():
                evidence["credential_cleanup"] = base.unlink_credential_overlay(
                    home_path
                )
                evidence["remaining_credential_artifacts"] = (
                    base.remaining_credential_artifacts(home_path)
                )
                shutil.rmtree(home_path, ignore_errors=True)
            if repo_path.exists():
                shutil.rmtree(repo_path, ignore_errors=True)
            evidence["HOME_EXISTS_after_cleanup"] = home_path.exists()
            evidence["REPO_EXISTS_after_cleanup"] = repo_path.exists()
            evidence["home_removed"] = not home_path.exists()
            evidence["repo_removed"] = not repo_path.exists()
            git_post = gp.read_git_provenance(gp.delivery_source_root())
            evidence.update(gp.finalize_git_provenance_evidence(git_pre, git_post))
            _SUITE_ROOT.mkdir(parents=True, exist_ok=True)
            EVIDENCE.write_text(json.dumps(evidence, indent=2, default=str))
            print(json.dumps(evidence, indent=2, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
