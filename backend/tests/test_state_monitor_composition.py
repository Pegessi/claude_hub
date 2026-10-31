"""Composition regression for state-safety x monitor-isolation integration.

The two reviewed branches both extend the workspace manager:

- state-safety adds the durable-load/quarantine surface
  (``_load_durable_json``/``state_recovery_warnings`` and friends);
- monitor-isolation adds the per-scope monitor refresh surface
  (``_monitor_refresh_sessions``/``_refresh_one_session_status`` and friends).

A single concrete ``WorkspaceManager`` must implement *both* abstract sets.
Leaving either side abstract would raise ``TypeError`` on instantiation; this
test pins that the two families coexist and the clean-state recovery view is
available while the background monitor loop is wired.
"""

from __future__ import annotations

import inspect
from importlib import import_module
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from claude_hub.services.workspace_manager import WorkspaceManager
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase

_wm = import_module("claude_hub.services.workspace_manager")

STATE_METHODS = (
    "_backup_path_for",
    "_atomic_save_state_text",
    "_load_durable_json",
    "state_recovery_warnings",
)
MONITOR_METHODS = (
    "_monitor_refresh_sessions",
    "_collect_session_agent_statuses",
    "_refresh_one_session_status",
    "_mark_runtime_task_failures",
    "_background_monitor_loop",
)


@pytest.fixture()
def isolated_state_root(monkeypatch: MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir(parents=True, exist_ok=True)
    index_file = root / "index.json"
    sched_file = root / "scheduled_tasks.json"
    monkeypatch.setattr(_wm, "STATE_ROOT", root)
    monkeypatch.setattr(_wm, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm, "SCHEDULED_TASKS_FILE", sched_file)
    monkeypatch.setattr(_wm._persistence, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._state, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._scheduling, "SCHEDULED_TASKS_FILE", sched_file)
    return root


def _assert_concretely_overridden(name: str) -> None:
    """The manager provides a real implementation, not an abstract stub."""
    impl = getattr(WorkspaceManager, name)
    assert callable(impl), f"{name} missing on concrete manager"
    base_impl = getattr(_WorkspaceManagerBase, name, None)
    if base_impl is not None and getattr(base_impl, "__isabstractmethod__", False):
        # Declared abstract on the base: a real override differs from the stub.
        assert impl is not base_impl, f"{name} is still the abstract base stub"


def test_manager_implements_state_and_monitor_abstracts(
    isolated_state_root: Path,
) -> None:
    # Instantiation fails with TypeError if ANY abstractmethod from either
    # branch is left unimplemented by a one-sided merge.
    manager = WorkspaceManager()

    for name in (*STATE_METHODS, *MONITOR_METHODS):
        _assert_concretely_overridden(name)
        assert callable(getattr(manager, name))

    # Both behaviors are live on the same instance: clean durable state reports
    # no recovery warnings while the monitor loop is the monitor-isolation one.
    assert manager.state_recovery_warnings() == []
    assert inspect.iscoroutinefunction(manager._background_monitor_loop)
