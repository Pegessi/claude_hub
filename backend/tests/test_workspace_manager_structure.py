"""Structural contracts for the responsibility-split workspace manager mixins."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from claude_hub.models import SessionKind
from claude_hub.services.workspace_manager import WorkspaceManager, workspace_manager
from claude_hub.services.workspace_manager._messaging import _MessagingMixin
from claude_hub.services.workspace_manager._workspaces import (
    _WorkspacesMixin,
    build_resident_agent_prompt,
)
from claude_hub.services.workspace_manager.messaging.delivery import _MessageDeliveryMixin
from claude_hub.services.workspace_manager.workspaces.prompts import (
    build_resident_agent_prompt as responsibility_resident_prompt,
)

_MANAGER_ROOT = Path(__file__).parents[1] / "claude_hub" / "services" / "workspace_manager"
_OWNED_LEGACY_MODULES = (
    "_messaging.py",
    "_tmux_queries.py",
    "_monitor.py",
    "_dispatch.py",
    "_workspaces.py",
    "_sessions.py",
)
_OWNED_RESPONSIBILITY_DIRECTORIES = (
    "messaging",
    "tmux",
    "monitoring",
    "dispatching",
    "workspaces",
    "sessions",
)
_COMPATIBILITY_LOOKUPS = (
    (
        "_messaging.py",
        "_capture_tmux_output",
        "claude_hub.services.runtime_isolation",
        ("tmux_command",),
    ),
    (
        "_messaging.py",
        "_ensure_session_ready_for_send",
        "claude_hub.services.ttyd_manager",
        ("_tmux_session_exists_async",),
    ),
    (
        "messaging/recovery.py",
        "_emit_delivery_uncertain",
        "claude_hub.models.task_mailbox",
        ("TaskActorRole", "TaskEventType"),
    ),
    (
        "messaging/recovery.py",
        "_emit_delivery_uncertain",
        "claude_hub.services.task_graph",
        ("make_task_consumer_key",),
    ),
    (
        "messaging/retry.py",
        "_emit_delivery_retry_requested",
        "claude_hub.models.task_mailbox",
        ("TaskActorRole", "TaskEventType"),
    ),
    (
        "messaging/retry.py",
        "_emit_delivery_retry_requested",
        "claude_hub.services.task_graph",
        ("task_inbox_consumer_key",),
    ),
    (
        "sessions/lifecycle.py",
        "_cleanup_deleted_session",
        "claude_hub.services.agent_stream",
        ("discard_session_stream",),
    ),
    (
        "sessions/lifecycle.py",
        "_delete_session_locked",
        "claude_hub.services.agent_stream",
        ("fence_session_stream",),
    ),
    (
        "sessions/lifecycle.py",
        "delete_task",
        "claude_hub.services.workspace_manager._task_execution",
        ("TaskExecutionConflict",),
    ),
    (
        "sessions/provisioning.py",
        "_create_managed_session",
        "claude_hub.services.ttyd_manager",
        (
            "CURSOR_TRANSCRIPT_SCHEMA",
            "SUPPORTED_CURSOR_TRANSCRIPT_VERSIONS",
            "cursor_cli_version_from_executable",
            "cursor_data_dir_for_env",
            "cursor_terminal_transcript_path",
        ),
    ),
    (
        "sessions/provisioning.py",
        "_resolve_remote_cwd",
        "claude_hub.services.workspace_identity",
        ("normalize_remote_cwd",),
    ),
    (
        "sessions/provisioning.py",
        "ensure_workspace_agent",
        "claude_hub.services.env_preset_resolver",
        ("EnvPresetNotFoundError", "EnvPresetParseError", "merge_env_with_preset"),
    ),
    (
        "sessions/provisioning.py",
        "ensure_workspace_agent",
        "claude_hub.services.workspace_identity",
        ("InvalidLocalAgentCwdError", "validate_local_agent_cwd_for_workspace"),
    ),
    (
        "tmux/board.py",
        "get_board",
        "claude_hub.services.board_pagination",
        ("paginate_board_tasks",),
    ),
    (
        "tmux/inventory.py",
        "_find_compatible_workspace_agent",
        "claude_hub.services.env_preset_resolver",
        ("effective_launch_envs_match",),
    ),
    (
        "tmux/inventory.py",
        "_find_compatible_workspace_agent",
        "claude_hub.services.workspace_identity",
        (
            "effective_local_agent_cwd",
            "local_agent_cwd_allowed_for_workspace",
            "normalize_remote_cwd",
            "validate_local_agent_cwd_for_workspace",
        ),
    ),
    (
        "tmux/review_reaper.py",
        "_first_available_reviewer",
        "claude_hub.services.workspace_identity",
        ("normalize_remote_cwd", "resolve_path"),
    ),
    ("tmux/transport.py", "_run_tmux", "claude_hub.services.runtime_isolation", ("tmux_command",)),
    (
        "tmux/transport.py",
        "_run_tmux_capture",
        "claude_hub.services.runtime_isolation",
        ("tmux_command",),
    ),
    (
        "workspaces/cleanup.py",
        "delete_workspace",
        "claude_hub.services.agent_stream",
        ("fence_session_stream",),
    ),
    (
        "workspaces/lifecycle.py",
        "_assert_no_duplicate_identity",
        "claude_hub.services.workspace_identity",
        ("DuplicateWorkspaceError", "workspace_identity_for_fields"),
    ),
    (
        "workspaces/lifecycle.py",
        "_workspaces_for_identity_key",
        "claude_hub.services.workspace_identity",
        ("workspace_identity",),
    ),
    (
        "workspaces/lifecycle.py",
        "create_workspace",
        "claude_hub.services.workspace_identity",
        ("canonical_workspace_path", "normalize_remote_cwd", "reject_relative_local_path"),
    ),
    (
        "workspaces/lifecycle.py",
        "ensure_workspace",
        "claude_hub.services.workspace_identity",
        (
            "default_workspace_create_name",
            "reject_relative_local_path",
            "select_workspace_candidate",
            "workspace_identity_for_fields",
        ),
    ),
    (
        "workspaces/lifecycle.py",
        "update_workspace",
        "claude_hub.services.workspace_identity",
        ("DuplicateWorkspaceError", "workspace_identity_for_fields"),
    ),
    (
        "workspaces/lifecycle.py",
        "update_workspace",
        "claude_hub.services.workspace_identity",
        ("canonical_workspace_path", "reject_relative_local_path"),
    ),
    (
        "workspaces/lifecycle.py",
        "update_workspace",
        "claude_hub.services.workspace_identity",
        ("normalize_remote_cwd",),
    ),
)


def _owned_sources() -> list[Path]:
    sources = [_MANAGER_ROOT / name for name in _OWNED_LEGACY_MODULES]
    for directory in _OWNED_RESPONSIBILITY_DIRECTORIES:
        sources.extend(sorted((_MANAGER_ROOT / directory).glob("*.py")))
    return sources


def test_owned_workspace_manager_sources_stay_bounded() -> None:
    oversized = {
        path.relative_to(_MANAGER_ROOT).as_posix(): len(path.read_text().splitlines())
        for path in _owned_sources()
        if len(path.read_text().splitlines()) > 1500
    }

    assert oversized == {}


def test_owned_workspace_manager_sources_have_no_reverse_or_delayed_imports() -> None:
    violations: list[str] = []
    for path in _owned_sources():
        source = path.read_text()
        if "import claude_hub.services.workspace_manager as _wm" in source:
            violations.append(f"{path.name}: reverse package lookup")
        tree = ast.parse(source)
        relative_path = path.relative_to(_MANAGER_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for child in ast.walk(node):
                if not isinstance(child, (ast.Import, ast.ImportFrom)):
                    continue
                compatibility_lookup = (
                    relative_path,
                    node.name,
                    child.module if isinstance(child, ast.ImportFrom) else "",
                    tuple(alias.name for alias in child.names),
                )
                if compatibility_lookup not in _COMPATIBILITY_LOOKUPS:
                    violations.append(f"{path.name}:{child.lineno}: delayed import")

    assert violations == []


def test_legacy_mixins_and_prompt_helpers_remain_compatible_exports() -> None:
    messaging_module = importlib.import_module("claude_hub.services.workspace_manager._messaging")
    workspaces_module = importlib.import_module("claude_hub.services.workspace_manager._workspaces")

    assert issubclass(_MessagingMixin, _MessageDeliveryMixin)
    assert WorkspaceManager.send_session_message is _MessageDeliveryMixin.send_session_message
    assert _MessagingMixin._wait_for_agent_prompt.__module__ == messaging_module.__name__
    assert _WorkspacesMixin._background_monitor_loop.__module__ == workspaces_module.__name__
    assert build_resident_agent_prompt is responsibility_resident_prompt
    assert WorkspaceManager._PROMPT_PREFIX_REVIEW == "Review workspace task."
    assert WorkspaceManager._CLEANUP_ALLOWED_SESSION_ROLES
    assert WorkspaceManager._TERMINAL_TASK_STATUSES_FOR_REUSE
    assert WorkspaceManager.__abstractmethods__ == frozenset()


def test_legacy_facades_reexport_historical_constants() -> None:
    constants = importlib.import_module("claude_hub.services.workspace_manager._constants")
    facade_names = (
        "_messaging",
        "_tmux_queries",
        "_monitor",
        "_dispatch",
        "_workspaces",
        "_sessions",
    )
    for facade_name in facade_names:
        facade = importlib.import_module(f"claude_hub.services.workspace_manager.{facade_name}")
        missing = [name for name in constants.__all__ if not hasattr(facade, name)]
        assert missing == [], facade_name
        # The suite intentionally patches this legacy facade value to shorten prompt waits.
        identity_exceptions = (
            {"AGENT_PROMPT_WAIT_SECONDS"} if facade_name == "_messaging" else set()
        )
        mismatched = [
            name
            for name in constants.__all__
            if name not in identity_exceptions
            and getattr(facade, name) is not getattr(constants, name)
        ]
        assert mismatched == [], facade_name

    dispatch = importlib.import_module("claude_hub.services.workspace_manager._dispatch")
    queue = importlib.import_module("claude_hub.services.workspace_manager.dispatching.queue")
    assert dispatch._NON_MIGRATABLE_DISPATCH_REASONS is queue._NON_MIGRATABLE_DISPATCH_REASONS


@pytest.mark.asyncio
async def test_runtime_lookups_observe_public_source_monkeypatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messaging_module = importlib.import_module("claude_hub.services.workspace_manager._messaging")
    runtime_isolation = importlib.import_module("claude_hub.services.runtime_isolation")
    ttyd_module = importlib.import_module("claude_hub.services.ttyd_manager")

    session: Any = SimpleNamespace(
        id="compat-session",
        tab_id="compat-tab",
        tmux_session="compat-tmux",
        session_kind=SessionKind.TERMINAL,
    )
    monkeypatch.setattr(workspace_manager, "_assert_session_seat", lambda _session: "seat")
    monkeypatch.setattr(ttyd_module.ttyd_manager, "processes", {session.tab_id: object()})
    session_exists = AsyncMock(return_value=True)
    monkeypatch.setattr(ttyd_module, "_tmux_session_exists_async", session_exists)

    await workspace_manager._ensure_session_ready_for_send(session)

    session_exists.assert_awaited_once_with(session.tmux_session)

    fake_process = SimpleNamespace(
        communicate=AsyncMock(return_value=(b"captured", b"")),
        returncode=0,
    )
    create_subprocess = AsyncMock(return_value=fake_process)
    monkeypatch.setattr(messaging_module.asyncio, "create_subprocess_exec", create_subprocess)
    monkeypatch.setattr(
        runtime_isolation,
        "tmux_command",
        lambda *args: ["patched-tmux", *args],
    )

    assert await workspace_manager._capture_tmux_output(session.tmux_session) == "captured"
    assert create_subprocess.await_args is not None
    assert create_subprocess.await_args.args[:2] == ("patched-tmux", "capture-pane")
