"""Runtime-owner isolation and late compatibility lookup contracts."""

from __future__ import annotations

import ast
import asyncio
import importlib
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from claude_hub.models import AgentType
from claude_hub.services.terminal_runtime import bindings, records
from claude_hub.services.terminal_runtime.manager.host import ManagerHost
from claude_hub.services.terminal_runtime.process.host import ProcessHost

TM = importlib.import_module("claude_hub.services.ttyd_manager")


@pytest.fixture
def runtime_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(TM, "STATE_FILE", tmp_path / "tabs.json")
    monkeypatch.setattr(TM, "ORDER_FILE", tmp_path / "order.json")
    monkeypatch.setattr(TM, "LAUNCH_ENV_DIR", tmp_path / "launch")
    return tmp_path


def test_two_managers_keep_mutable_state_and_locks_separate(runtime_paths: Path) -> None:
    first, second = TM.TTYDManager(), TM.TTYDManager()
    for name in (
        "processes",
        "_start_locks",
        "_status_cache",
        "_status_snapshots",
        "_tab_order",
        "_pending_stream_cleanup",
        "_cleanup_tasks",
        "_cleanup_locks",
    ):
        assert getattr(first, name) is not getattr(second, name)
    lock = first._fork_lock("source")
    assert lock is first._fork_lock("source")
    assert lock is not second._fork_lock("source")
    first._tab_order.append("one")
    first._cleanup_retry_offset = 3
    assert second._tab_order == []
    assert second._cleanup_retry_offset == 0
    assert first._runtime is second._runtime
    assert first._runtime.GLOBAL_CODEX_LAUNCH_LOCK is TM.GLOBAL_CODEX_LAUNCH_LOCK


def test_new_only_manager_retains_lazy_cleanup_contract() -> None:
    first = TM.TTYDManager.__new__(TM.TTYDManager)
    second = TM.TTYDManager.__new__(TM.TTYDManager)
    assert first._pending_stream_cleanup is not second._pending_stream_cleanup
    assert first._cleanup_tasks is not second._cleanup_tasks
    assert first._cleanup_locks is not second._cleanup_locks
    assert first._fork_lock("source") is not second._fork_lock("source")


def test_two_processes_keep_environment_and_session_state_separate(runtime_paths: Path) -> None:
    source_env = {"TERM": "xterm-256color"}
    first = TM.TTYDProcess("first", 8001, "First", agent_type=AgentType.TERMINAL, env=source_env)
    second = TM.TTYDProcess("second", 8002, "Second", agent_type=AgentType.TERMINAL, env=source_env)
    first.env["LOCAL"] = "one"
    first._launch_wall = 1.0
    assert "LOCAL" not in second.env
    assert "LOCAL" not in source_env
    assert second._launch_wall is None
    assert first._child_env()["CLAUDE_HUB_TAB_ID"] == "first"
    assert second._child_env()["CLAUDE_HUB_TAB_ID"] == "second"
    assert first._tunnel_registry is second._tunnel_registry is TM.TTYDProcess._tunnel_registry


def test_existing_process_observes_new_launch_path_and_writer_patch(
    runtime_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = TM.TTYDProcess("one", 8001, "One", env={"TERM": "xterm"})
    writer = Mock()
    changed_path = runtime_paths / "new-launch"
    monkeypatch.setattr(TM, "LAUNCH_ENV_DIR", changed_path)
    monkeypatch.setattr(TM, "_write_private_launch_file", writer)
    result = process._write_launch_settings_file()
    assert result == changed_path / "one.settings.json"
    assert writer.call_args.args[0] == result


async def test_existing_process_observes_tmux_patch_and_subprocess_module_patch(
    runtime_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = TM.TTYDProcess("one", 8001, "One", agent_type=AgentType.TERMINAL)
    exists = AsyncMock(return_value=False)
    spawn = AsyncMock()
    monkeypatch.setattr(TM, "_tmux_session_exists_async", exists)
    monkeypatch.setattr(TM.asyncio, "create_subprocess_exec", spawn)
    assert await process.capture_history() == ""
    exists.assert_awaited_once_with(process.tmux_session)
    spawn.assert_not_awaited()
    child = Mock(returncode=0)
    child.communicate = AsyncMock(return_value=(b"captured\r\n", b""))
    spawn.return_value = child
    monkeypatch.setattr(TM, "_tmux_session_exists_async", AsyncMock(return_value=True))
    assert await process.capture_history() == "captured\r\n"
    spawn.assert_awaited_once()


def test_existing_manager_observes_snapshot_path_parser_and_writer_patches(
    runtime_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = TM.TTYDManager()
    parse = Mock(wraps=TM._parse_tabs_snapshot)
    write = Mock()
    monkeypatch.setattr(TM, "_parse_tabs_snapshot", parse)
    monkeypatch.setattr(TM, "_write_tabs_snapshot", write)
    manager._persist_state()
    parse.assert_called_once()
    write.assert_called_once_with(parse.call_args.args[0])
    assert manager._state_confirmed is True
    changed_order = runtime_paths / "changed" / "order.json"
    monkeypatch.setattr(TM, "ORDER_FILE", changed_order)
    manager._save_order()
    assert changed_order.read_text() == "[]"


def test_shared_launch_lock_lookup_is_late_and_bindings_are_frozen(
    runtime_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = TM.TTYDManager()
    replacement = asyncio.Lock()
    monkeypatch.setattr(TM, "GLOBAL_CODEX_LAUNCH_LOCK", replacement)
    assert manager._runtime.GLOBAL_CODEX_LAUNCH_LOCK is replacement
    with pytest.raises(FrozenInstanceError):
        setattr(manager._runtime, "read_STATE_FILE", lambda: runtime_paths)
    assert isinstance(manager._runtime, bindings.RuntimeBindings)


def test_owner_contracts_remain_checked_and_values_have_one_definition() -> None:
    assert issubclass(TM.TTYDManager, ManagerHost)
    assert issubclass(TM.TTYDProcess, ProcessHost)
    assert not TM.TTYDManager.__abstractmethods__
    assert not TM.TTYDProcess.__abstractmethods__
    assert TM.ScanEntry is records.ScanEntry
    assert TM.TabStateError is records.TabStateError
    assert TM._PendingTabCleanup is records._PendingTabCleanup


def test_runtime_modules_do_not_import_the_compatibility_owner() -> None:
    package = Path(bindings.__file__).parent
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        class RuntimeImportVisitor(ast.NodeVisitor):
            def visit_If(self, node: ast.If) -> None:
                if isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
                    for statement in node.orelse:
                        self.visit(statement)
                    return
                self.generic_visit(node)

            def visit_Import(self, node: ast.Import) -> None:
                assert all(not item.name.endswith("ttyd_manager") for item in node.names), path

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                assert not (node.module or "").endswith("ttyd_manager"), path
                assert all(item.name != "ttyd_manager" for item in node.names), path

        RuntimeImportVisitor().visit(tree)
