#!/usr/bin/env python3
"""Isolated legacy raw-state cold-start migration E2E.

Seeds pre-unification state.json (agent_runs/agent_events/resident_root +
agent_run_id) on disk, then performs two backend cold starts against an
isolated CLAUDE_HUB_E2E_HOME. Verifies Task Graph + TaskMailbox output,
legacy key removal, and idempotent second load (no duplicate events).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

E2E_HELPERS = Path(__file__).resolve().parents[1] / "agent-tree-e2e"
_HARNESS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_HELPERS))

import run_e2e as base  # noqa: E402

import importlib.util

_gp_spec = importlib.util.spec_from_file_location(
    "task_graph_git_provenance",
    _HARNESS_DIR / "git_provenance.py",
)
assert _gp_spec and _gp_spec.loader
gp = importlib.util.module_from_spec(_gp_spec)
_gp_spec.loader.exec_module(gp)

_SUITE_ROOT = Path(
    os.environ.get(
        "CLAUDE_HUB_E2E_SUITE_ROOT",
        str(Path(os.environ["CLAUDE_HUB_E2E_HOME"]).parent),
    )
)
EVIDENCE = _SUITE_ROOT / "legacy_migration_evidence.json"
BACKEND = Path(os.environ["CLAUDE_HUB_E2E_BACKEND"])

WORKSPACE_ID = "e2e-legacy-migration-ws"
TASK_PARENT = "task-linked-parent"
TASK_CHILD = "task-linked-child"
TASK_ORDINARY = "task-ordinary"
SESSION_WORKER = "sess-worker"
REPORT_BRIDGED = "rep-bridged"
REPORT_ORDINARY = "rep-ordinary"

LEGACY_DISK_KEYS = frozenset({"agent_runs", "agent_events"})
LEGACY_TASK_KEYS = frozenset({"agent_run_id"})
LEGACY_EVENT_KEYS = frozenset({"compat_run_id"})
LEGACY_INDEX_KEYS = frozenset({"resident_ack_sequence"})


def http(method: str, path: str, query: dict | None = None, timeout: int = 60) -> Any:
    url = base.BASE + path
    if query:
        from urllib.parse import urlencode

        url += "?" + urlencode(query)
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode()) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()
        raise RuntimeError(f"{method} {url} -> {exc.code}: {detail}") from exc


def _raw_task(**kwargs: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "prompt": "legacy task",
        "agent_type": "claude",
        "status": "todo",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "pending_call_ids": [],
        "processing_call_ids": [],
        "uncertain_call_ids": [],
    }
    defaults.update(kwargs)
    return defaults


def _raw_run(**kwargs: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "executor_kind": "managed_task",
        "status": "running",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }
    defaults.update(kwargs)
    return defaults


def _raw_agent_event(**kwargs: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "sequence": 1,
        "call_id": "legacy-call",
        "agent_run_id": "run-child",
        "type": "started",
        "author": "run-child",
        "recipient": "run-parent",
        "action": "spawn:started",
        "target": "run-child",
        "fingerprint": "legacy-fp",
        "payload": {},
        "created_at": "2026-01-01T00:00:00",
    }
    defaults.update(kwargs)
    return defaults


def build_legacy_state_payload() -> dict[str, Any]:
    return {
        "tasks": [
            _raw_task(
                id=TASK_PARENT,
                workspace_id=WORKSPACE_ID,
                title="linked-parent",
                agent_run_id="run-parent",
                pending_call_ids=["p1"],
            ),
            _raw_task(
                id=TASK_CHILD,
                workspace_id=WORKSPACE_ID,
                title="linked-child",
                agent_run_id="run-child",
                pending_call_ids=["p2"],
                processing_call_ids=["proc-1"],
                uncertain_call_ids=["unc-1"],
            ),
            _raw_task(
                id=TASK_ORDINARY,
                workspace_id=WORKSPACE_ID,
                title="ordinary-root",
            ),
        ],
        "sessions": [
            {
                "id": SESSION_WORKER,
                "workspace_id": WORKSPACE_ID,
                "tab_id": "tab-worker",
                "role": "worker",
                "agent_type": "claude",
                "status": "working",
                "title": "worker",
                "workspace_path": str(base.REPO),
                "tmux_session": "tmux-worker",
                "target": "local",
                "solo_mode": True,
                "env": {},
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        ],
        "reports": [
            {
                "id": REPORT_BRIDGED,
                "workspace_id": WORKSPACE_ID,
                "task_id": TASK_CHILD,
                "session_id": SESSION_WORKER,
                "state": "working",
                "message": "already bridged",
                "review_cycle": 1,
                "created_at": "2026-01-01T00:30:00",
            },
            {
                "id": REPORT_ORDINARY,
                "workspace_id": WORKSPACE_ID,
                "task_id": TASK_ORDINARY,
                "session_id": "sess-gone",
                "state": "ready_for_review",
                "message": "ordinary report",
                "review_cycle": 2,
                "created_at": "2026-01-01T01:00:00",
            },
        ],
        "agent_runs": [
            _raw_run(
                id="run-resident",
                workspace_id=WORKSPACE_ID,
                parent_id=None,
                path="run-resident",
                executor_kind="resident_root",
                ack_sequence=11,
            ),
            _raw_run(
                id="run-parent",
                workspace_id=WORKSPACE_ID,
                parent_id="run-resident",
                path="run-resident/run-parent",
                supervisor_id="run-resident",
                context_ref=TASK_PARENT,
                ack_sequence=4,
            ),
            _raw_run(
                id="run-child",
                workspace_id=WORKSPACE_ID,
                parent_id="run-parent",
                path="run-resident/run-parent/run-child",
                supervisor_id="run-parent",
                context_ref=TASK_CHILD,
                ack_sequence=7,
            ),
        ],
        "agent_events": [
            _raw_agent_event(
                sequence=4,
                call_id="legacy-started",
                fingerprint="legacy-started-fp",
                payload={"task_id": TASK_CHILD},
            ),
            _raw_agent_event(
                sequence=5,
                call_id=f"report:{REPORT_BRIDGED}",
                type="progress",
                action="report",
                fingerprint="bridged-fp",
                payload={"report_id": REPORT_BRIDGED, "task_id": TASK_CHILD},
            ),
        ],
    }


def seed_legacy_disk(home: Path, repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text("legacy migration e2e\n", encoding="utf-8")
    state_root = home / ".claude_hub" / "workspaces"
    ws_dir = state_root / WORKSPACE_ID
    ws_dir.mkdir(parents=True, exist_ok=True)
    index = {
        "workspaces": [
            {
                "id": WORKSPACE_ID,
                "name": "Legacy Migration E2E",
                "path": str(repo),
                "default_branch": "main",
                "session_prefix": "lm",
                "target": "local",
                "resident_agent_enabled": False,
                "resident_agent_paused": False,
                "resident_agent_interval_minutes": 60,
                "resident_agent_type": "claude",
                "resident_agent_solo_mode": True,
                "resident_agent_master_mode": False,
                "resident_agent_target": "local",
                "resident_agent_remote_reconnect": True,
                "remote_reconnect": True,
                "resident_ack_sequence": 11,
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        ]
    }
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    (ws_dir / "state.json").write_text(
        json.dumps(build_legacy_state_payload(), indent=2),
        encoding="utf-8",
    )


def disk_state(home: Path) -> dict[str, Any]:
    path = home / ".claude_hub" / "workspaces" / WORKSPACE_ID / "state.json"
    return json.loads(path.read_text(encoding="utf-8"))


def disk_index(home: Path) -> dict[str, Any]:
    path = home / ".claude_hub" / "workspaces" / "index.json"
    return json.loads(path.read_text(encoding="utf-8"))


def assert_no_legacy_keys_on_disk(home: Path) -> dict[str, Any]:
    disk = disk_state(home)
    for key in LEGACY_DISK_KEYS:
        if key in disk:
            raise RuntimeError(f"legacy disk key still present after migration: {key}")
    for task in disk.get("tasks") or []:
        for key in LEGACY_TASK_KEYS:
            if key in task:
                raise RuntimeError(f"legacy task key {key!r} on task {task.get('id')}")
    for event in disk.get("task_events") or []:
        for key in LEGACY_EVENT_KEYS:
            if key in event:
                raise RuntimeError(f"legacy event key {key!r} on seq {event.get('sequence')}")
    index = disk_index(home)
    for item in index.get("workspaces") or []:
        if item.get("id") == WORKSPACE_ID:
            for key in LEGACY_INDEX_KEYS:
                if key in item:
                    raise RuntimeError(f"legacy index key still present: {key}")
    return disk


def fetch_migration_snapshot() -> dict[str, Any]:
    tree = http("GET", f"/api/workspaces/{WORKSPACE_ID}/tasks/tree")
    tree = tree if isinstance(tree, list) else []
    by_id = {item["id"]: item for item in tree if isinstance(item, dict) and item.get("id")}
    if TASK_CHILD not in by_id or TASK_PARENT not in by_id:
        raise RuntimeError(f"expected tasks missing from tree: {sorted(by_id)}")
    child = by_id[TASK_CHILD]
    parent = by_id[TASK_PARENT]
    events = http(
        "GET",
        f"/api/workspaces/{WORKSPACE_ID}/tasks/{TASK_PARENT}/events",
        query={"since_sequence": "0"},
    )
    events = events if isinstance(events, list) else []
    return {
        "parent": parent,
        "child": child,
        "events": events,
        "event_call_ids": [item.get("call_id") for item in events],
        "event_sequences": [item.get("sequence") for item in events],
        "parent_consumer_ack_sequence": parent.get("consumer_ack_sequence"),
        "child_consumer_ack_sequence": child.get("consumer_ack_sequence"),
        "child_parent_task_id": child.get("parent_task_id"),
        "child_path": child.get("path"),
    }


def assert_agent_tree_routes_gone() -> None:
    for path in (
        "/api/agent-tree/runs",
        "/api/agent-tree/spawn",
    ):
        url = base.BASE + path
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raise RuntimeError(f"expected agent-tree route gone, got {resp.status} for {path}")
        except urllib.error.HTTPError as exc:
            if exc.code not in {404, 405}:
                raise RuntimeError(f"unexpected agent-tree status {exc.code} for {path}") from exc


def _cold_restart(
    backend: subprocess.Popen | None,
    launch_env: dict[str, str],
) -> subprocess.Popen:
    base.stop_backend(backend)
    time.sleep(1)
    proc = base.start_backend(base.E2E_ROOT, launch_env)
    base.wait_health()
    return proc


def main() -> int:
    if base.PORT == 8173:
        raise RuntimeError("Legacy migration E2E must not use live port 8173")

    gp.forbid_git_provenance_env_overrides()
    git_pre = gp.read_git_provenance(gp.delivery_source_root())
    gp.require_clean_provenance(git_pre)

    evidence: dict[str, Any] = {
        "harness": "legacy raw-state cold-start migration E2E",
        "workspace_id": WORKSPACE_ID,
        "task_ids": [TASK_PARENT, TASK_CHILD, TASK_ORDINARY],
        "port": base.PORT,
        "home": str(base.E2E_ROOT),
        "repo": str(base.REPO),
        "suite_root": str(_SUITE_ROOT),
        "evidence_path": str(EVIDENCE),
        "git_sha": git_pre["git_sha"],
        "branch": git_pre["branch"],
        "dirty": git_pre["dirty"],
        "source_root": git_pre["source_root"],
        "git_provenance_pre": git_pre,
        "seed_contains_resident_root": True,
        "cold_starts": [],
    }
    backend: subprocess.Popen | None = None
    try:
        base.E2E_ROOT.mkdir(parents=True, exist_ok=True)
        base.REPO.mkdir(parents=True, exist_ok=True)
        evidence["pre_killed_tmux"] = base.kill_e2e_tmux()
        _, launch_env = base.install_isolated_launch_env(base.E2E_ROOT)
        seed_legacy_disk(base.E2E_ROOT, base.REPO)
        evidence["seed_legacy_keys"] = sorted(
            LEGACY_DISK_KEYS | LEGACY_TASK_KEYS | LEGACY_INDEX_KEYS
        )

        backend = base.start_backend(base.E2E_ROOT, launch_env)
        base.wait_health()
        assert_agent_tree_routes_gone()
        snap1 = fetch_migration_snapshot()
        disk1 = assert_no_legacy_keys_on_disk(base.E2E_ROOT)
        evidence["cold_starts"].append(
            {
                "label": "first_cold_start",
                "backend_pid": backend.pid,
                "snapshot": snap1,
                "task_event_count": len(disk1.get("task_events") or []),
                "backup_exists": (
                    base.E2E_ROOT
                    / ".claude_hub"
                    / "workspaces"
                    / WORKSPACE_ID
                    / "state.json.pre-migration-backup"
                ).exists(),
            }
        )

        if snap1["child_parent_task_id"] != TASK_PARENT:
            raise RuntimeError("child parent_task_id not migrated correctly")
        if snap1["event_call_ids"] != [
            "legacy-started",
            f"report:{REPORT_BRIDGED}",
            f"report:{REPORT_ORDINARY}",
        ]:
            raise RuntimeError(f"unexpected mailbox call_ids: {snap1['event_call_ids']}")
        if snap1["parent_consumer_ack_sequence"] != 11:
            raise RuntimeError("parent consumer_ack_sequence not migrated from resident run")
        if snap1["child_consumer_ack_sequence"] != 7:
            raise RuntimeError("child consumer_ack_sequence not migrated from linked run")

        backend = _cold_restart(backend, launch_env)
        snap2 = fetch_migration_snapshot()
        disk2 = assert_no_legacy_keys_on_disk(base.E2E_ROOT)
        evidence["cold_starts"].append(
            {
                "label": "second_cold_start",
                "backend_pid": backend.pid,
                "snapshot": snap2,
                "task_event_count": len(disk2.get("task_events") or []),
            }
        )

        if snap2["event_call_ids"] != snap1["event_call_ids"]:
            raise RuntimeError(
                "duplicate migration on second cold start: "
                f"before={snap1['event_call_ids']} after={snap2['event_call_ids']}"
            )
        if snap2["event_sequences"] != snap1["event_sequences"]:
            raise RuntimeError("event sequences changed on second cold start")
        if len(disk2.get("task_events") or []) != len(disk1.get("task_events") or []):
            raise RuntimeError("task_events count changed on second cold start")

        git_post = gp.read_git_provenance(gp.delivery_source_root())
        evidence.update(gp.finalize_git_provenance_evidence(git_pre, git_post))
        evidence["cleanup"] = {
            "stopped_backend": True,
            "post_killed_tmux": base.kill_e2e_tmux(),
        }
        EVIDENCE.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(json.dumps({"ok": True, "evidence": str(EVIDENCE)}, indent=2))
        return 0
    except Exception as exc:
        evidence["error"] = str(exc)
        EVIDENCE.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        raise
    finally:
        base.stop_backend(backend)


if __name__ == "__main__":
    raise SystemExit(main())
