"""Focused executor-selection tests for Agent Tree managed children."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_hub.models.agent_tree import (
    AgentEventType,
    AgentRun,
    ExecutorKind,
    ManagedExecutorConfig,
    SpawnRequest,
    WaitRequest,
)
from claude_hub.models.schemas import (
    AgentRuntimeStatus,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskStatus,
)
from claude_hub.services.agent_tree import AgentTreeManager
from claude_hub.services.agent_tree_adapters import (
    ExecutorUnavailableError,
    ExternalJobAdapter,
    ManagedTaskAdapter,
    NativeSubagentAdapter,
)
from claude_hub.services.ttyd_manager import TTYDProcess


class _FakeWorkspaceManager:
    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = workspace_path
        self.workspaces = {
            "workspace-1": SimpleNamespace(
                target=ExecutionTarget.LOCAL,
                remote_profile_id=None,
                remote_cwd=None,
                remote_reconnect=True,
            )
        }
        self.tasks: dict[str, WorkspaceTask] = {}
        self.sessions: dict[str, ManagedSession] = {}
        self.ensure_payloads = []
        self.start_payloads = []
        self.save_calls = 0

    def _save_state(self) -> None:
        self.save_calls += 1

    @asynccontextmanager
    async def workspace_mutation_lock(self, workspace_id: str) -> AsyncIterator[None]:
        yield

    async def ensure_workspace_agent(self, workspace_id, payload):
        self.ensure_payloads.append(payload)
        ordinal = len(self.ensure_payloads)
        session_id = f"session-{payload.agent_type.value}-{ordinal}"
        now = datetime.utcnow()
        session = ManagedSession(
            id=session_id,
            workspace_id=workspace_id,
            task_id=None,
            tab_id=f"tab-{ordinal}",
            role=payload.role,
            agent_type=payload.agent_type,
            status=ManagedSessionStatus.IDLE,
            runtime_status=AgentRuntimeStatus.IDLE,
            current_task_id=None,
            title=payload.title or session_id,
            workspace_path=payload.cwd or str(self.workspace_path),
            tmux_session=f"tmux-{ordinal}",
            target=payload.target or ExecutionTarget.LOCAL,
            remote_profile_id=payload.remote_profile_id,
            remote_cwd=payload.remote_cwd,
            remote_reconnect=(
                payload.remote_reconnect if payload.remote_reconnect is not None else True
            ),
            solo_mode=payload.solo_mode,
            ephemeral=payload.ephemeral,
            env=payload.env,
            created_at=now,
            updated_at=now,
        )
        self.sessions[session.id] = session
        return session

    def create_task(self, workspace_id, payload):
        ordinal = len(self.tasks) + 1
        task_id = f"task-{ordinal}"
        now = datetime.utcnow()
        task = WorkspaceTask(
            id=task_id,
            workspace_id=workspace_id,
            title=payload.title,
            prompt=payload.prompt,
            agent_type=payload.agent_type,
            task_mode=payload.task_mode,
            status=WorkspaceTaskStatus.TODO,
            session_id=payload.session_id,
            agent_run_id=payload.agent_run_id,
            created_at=now,
            updated_at=now,
        )
        self.tasks[task.id] = task
        return task

    async def start_task(self, task_id, payload):
        self.start_payloads.append(payload)
        task = self.tasks[task_id]
        self.tasks[task_id] = task.model_copy(
            update={
                "status": WorkspaceTaskStatus.WORKING,
                "session_id": payload.target_session_id,
                "updated_at": datetime.utcnow(),
            }
        )
        return self.tasks[task_id]


def _run(run_id: str, config: ManagedExecutorConfig | None = None) -> AgentRun:
    payload = dict(
        id=run_id,
        workspace_id="workspace-1",
        parent_id="root",
        path=f"root/{run_id}",
        supervisor_id="root",
        executor_kind=ExecutorKind.MANAGED_TASK,
        title=run_id,
    )
    if config is not None:
        payload["executor_config"] = config
    return AgentRun(**payload)


@pytest.mark.asyncio
async def test_managed_adapter_routes_distinct_cli_and_model_configs_to_real_sessions(
    tmp_path: Path,
) -> None:
    wm = _FakeWorkspaceManager(tmp_path)
    adapter = ManagedTaskAdapter(wm)  # type: ignore[arg-type]
    claude_run = _run(
        "claude-child",
        ManagedExecutorConfig(agent_type=AgentType.CLAUDE, model="claude-sonnet-4-5"),
    )
    codex_run = _run(
        "codex-child",
        ManagedExecutorConfig(agent_type=AgentType.CODEX, model="gpt-5.6-codex"),
    )

    claude_task_id = await adapter.spawn(claude_run, "implement with Claude")
    codex_task_id = await adapter.spawn(codex_run, "implement with Codex")

    assert claude_task_id != codex_task_id
    claude_task = wm.tasks[claude_task_id]
    codex_task = wm.tasks[codex_task_id]
    assert claude_task.agent_type == AgentType.CLAUDE
    assert codex_task.agent_type == AgentType.CODEX
    assert claude_task.session_id != codex_task.session_id
    assert wm.sessions[claude_task.session_id].env["ANTHROPIC_MODEL"] == "claude-sonnet-4-5"
    assert wm.sessions[codex_task.session_id].env["CODEX_MODEL"] == "gpt-5.6-codex"
    assert [item.agent_type for item in wm.start_payloads] == [
        AgentType.CLAUDE,
        AgentType.CODEX,
    ]
    assert all(item.target_session_id for item in wm.start_payloads)

    # Config and adapter capability state are part of the persisted run, not
    # transient adapter dictionaries.  A cold reload therefore preserves the
    # concrete CLI/model contract and its durable-status claim.
    for run in (claude_run, codex_run):
        reloaded = AgentRun.model_validate_json(run.model_dump_json())
        assert reloaded.executor_config == run.executor_config
        assert reloaded.executor_capabilities == run.executor_capabilities
        assert reloaded.executor_capabilities is not None
        assert reloaded.executor_capabilities.available is True
        assert reloaded.executor_capabilities.durable_status is True


@pytest.mark.asyncio
async def test_compatible_session_is_reused_but_different_model_gets_new_session(
    tmp_path: Path,
) -> None:
    wm = _FakeWorkspaceManager(tmp_path)
    adapter = ManagedTaskAdapter(wm)  # type: ignore[arg-type]
    first = _run(
        "codex-a",
        ManagedExecutorConfig(agent_type=AgentType.CODEX, model="gpt-5.6-codex"),
    )
    same = _run(
        "codex-b",
        ManagedExecutorConfig(agent_type=AgentType.CODEX, model="gpt-5.6-codex"),
    )
    different = _run(
        "codex-c",
        ManagedExecutorConfig(agent_type=AgentType.CODEX, model="gpt-5.5-codex"),
    )

    await adapter.spawn(first, "one")
    await adapter.spawn(same, "two")
    await adapter.spawn(different, "three")

    assert len(wm.sessions) == 2
    assert wm.tasks["task-1"].session_id == wm.tasks["task-2"].session_id
    assert wm.tasks["task-3"].session_id != wm.tasks["task-1"].session_id


@pytest.mark.asyncio
async def test_remote_reconnect_mismatch_is_rejected_and_not_reused(tmp_path: Path) -> None:
    wm = _FakeWorkspaceManager(tmp_path)
    wm.workspaces["workspace-1"].target = ExecutionTarget.REMOTE
    wm.workspaces["workspace-1"].remote_profile_id = "remote-profile"
    wm.workspaces["workspace-1"].remote_cwd = "/remote/repo"
    wm.workspaces["workspace-1"].remote_reconnect = True
    now = datetime.utcnow()
    incompatible = ManagedSession(
        id="remote-no-reconnect",
        workspace_id="workspace-1",
        task_id=None,
        tab_id="tab-remote-no-reconnect",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CODEX,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        title="Remote Codex without reconnect",
        workspace_path=str(tmp_path),
        tmux_session="tmux-remote-no-reconnect",
        target=ExecutionTarget.REMOTE,
        remote_profile_id="remote-profile",
        remote_cwd="/remote/repo",
        remote_reconnect=False,
        solo_mode=False,
        env={"CODEX_MODEL": "gpt-5.6-codex"},
        created_at=now,
        updated_at=now,
    )
    wm.sessions[incompatible.id] = incompatible
    run = _run(
        "remote-reconnect-required",
        ManagedExecutorConfig(
            agent_type=AgentType.CODEX,
            model="gpt-5.6-codex",
            solo_mode=False,
            target=ExecutionTarget.REMOTE,
            remote_profile_id="remote-profile",
            remote_cwd="/remote/repo",
            remote_reconnect=True,
        ),
    )
    adapter = ManagedTaskAdapter(wm)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="remote_reconnect"):
        adapter.validate_session(run, incompatible)

    task_id = await adapter.spawn(run, "must use reconnecting remote session")

    assert len(wm.ensure_payloads) == 1
    assert wm.ensure_payloads[0].remote_reconnect is True
    replacement_id = wm.tasks[task_id].session_id
    assert replacement_id is not None
    assert replacement_id != incompatible.id
    replacement = wm.sessions[replacement_id]
    assert replacement.remote_reconnect is True


def test_legacy_spawn_and_run_remain_implicit_until_adapter_resolution() -> None:
    request = SpawnRequest(
        workspace_id="workspace-1",
        parent_id="root",
        executor_kind=ExecutorKind.MANAGED_TASK,
        initial_message="legacy",
        call_id="legacy-1",
    )
    run = _run("legacy-child")

    assert request.executor_config is None
    assert run.executor_config is None


def test_legacy_managed_run_resolves_claude_and_workspace_target(tmp_path: Path) -> None:
    wm = _FakeWorkspaceManager(tmp_path)
    wm.workspaces["workspace-1"].target = ExecutionTarget.REMOTE
    wm.workspaces["workspace-1"].remote_profile_id = "remote-1"
    wm.workspaces["workspace-1"].remote_cwd = "/remote/repo"
    run = _run("legacy-child")

    ManagedTaskAdapter(wm).prepare_run(run)  # type: ignore[arg-type]

    assert run.executor_config is not None
    assert run.executor_config.agent_type == AgentType.CLAUDE
    assert run.executor_config.target == ExecutionTarget.REMOTE
    assert run.executor_config.remote_profile_id == "remote-1"
    assert run.executor_config.remote_cwd == "/remote/repo"


def test_legacy_explicit_session_recovers_actual_codex_config(tmp_path: Path) -> None:
    wm = _FakeWorkspaceManager(tmp_path)
    now = datetime.utcnow()
    session = ManagedSession(
        id="existing-codex",
        workspace_id="workspace-1",
        task_id=None,
        tab_id="tab-existing",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CODEX,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        title="Existing Codex",
        workspace_path=str(tmp_path),
        tmux_session="tmux-existing",
        target=ExecutionTarget.LOCAL,
        solo_mode=True,
        env={"CODEX_MODEL": "gpt-5.6-codex", "FEATURE_FLAG": "1"},
        created_at=now,
        updated_at=now,
    )
    config = ManagedTaskAdapter.config_from_session(session)
    run = _run("legacy-explicit", config)
    adapter = ManagedTaskAdapter(wm)  # type: ignore[arg-type]

    adapter.validate_session(run, session)

    assert run.executor_config is not None
    assert run.executor_config.agent_type == AgentType.CODEX
    assert run.executor_config.model == "gpt-5.6-codex"
    assert run.executor_config.env == {"FEATURE_FLAG": "1"}

    incompatible = session.model_copy(update={"agent_type": AgentType.CLAUDE})
    with pytest.raises(ValueError, match="agent_type"):
        adapter.validate_session(run, incompatible)


def test_placeholder_adapters_are_explicitly_unavailable() -> None:
    for adapter in (NativeSubagentAdapter(), ExternalJobAdapter()):
        capability = adapter.capabilities()
        assert capability.available is False
        assert capability.supports_spawn is False
        assert capability.unavailable_reason
        with pytest.raises(ExecutorUnavailableError, match="runtime"):
            adapter.require_available()


def test_cursor_model_override_is_rejected_instead_of_claimed() -> None:
    config = ManagedExecutorConfig(agent_type=AgentType.CURSOR, model="unverified-model")
    with pytest.raises(ValueError, match="does not support"):
        ManagedTaskAdapter._launch_env(config)


def test_codex_model_is_in_real_cli_launch_command(tmp_path: Path) -> None:
    process = TTYDProcess(
        tab_id="codex-model-tab",
        port=19991,
        name="Codex model executor",
        cwd=str(tmp_path),
        solo_mode=True,
        agent_type=AgentType.CODEX,
        env={"CODEX_MODEL": "gpt-5.6-codex"},
    )

    command = process._codex_launch_command(recover=False)
    assert command == (
        "codex --ask-for-approval never --sandbox danger-full-access " "--model gpt-5.6-codex"
    )


@pytest.mark.asyncio
async def test_manager_spawn_persists_and_reloads_real_executor_contract(
    tmp_path: Path,
) -> None:
    wm = _FakeWorkspaceManager(tmp_path)
    manager = AgentTreeManager(wm)  # type: ignore[arg-type]
    root = manager.create_root_run(
        workspace_id="workspace-1",
        executor_kind=ExecutorKind.RESIDENT_ROOT,
        title="resident",
    )
    assert root.executor_capabilities is not None
    assert root.executor_capabilities.available is True

    child = await manager.spawn(
        SpawnRequest(
            workspace_id="workspace-1",
            parent_id=root.id,
            executor_kind=ExecutorKind.MANAGED_TASK,
            executor_config=ManagedExecutorConfig(
                agent_type=AgentType.CODEX,
                model="gpt-5.6-codex",
            ),
            initial_message="implement with Codex",
            call_id="spawn-codex-child-1",
        )
    )

    assert child.executor_config is not None
    assert child.executor_config.agent_type == AgentType.CODEX
    assert child.executor_config.model == "gpt-5.6-codex"
    assert child.executor_config.target == ExecutionTarget.LOCAL
    assert child.executor_capabilities is not None
    assert child.executor_capabilities.available is True
    assert child.context_ref is not None
    task = wm.tasks[child.context_ref]
    assert task.agent_type == AgentType.CODEX
    assert task.session_id is not None
    assert wm.sessions[task.session_id].env["CODEX_MODEL"] == "gpt-5.6-codex"

    initial_events = await manager.wait(
        WaitRequest(
            workspace_id="workspace-1",
            recipient_id=root.id,
            since_sequence=0,
            subtree=True,
            timeout_seconds=0,
        )
    )
    assert any(
        event.type == AgentEventType.STARTED and event.author == child.id
        for event in initial_events
    )
    cursor = max(event.sequence for event in initial_events)
    manager.emit_event(
        workspace_id="workspace-1",
        agent_run_id=child.id,
        event_type=AgentEventType.PROGRESS,
        author=child.id,
        recipient=root.id,
        call_id="codex-child-progress-1",
        payload={"message": "managed child made progress"},
    )
    waited = await manager.wait(
        WaitRequest(
            workspace_id="workspace-1",
            recipient_id=root.id,
            since_sequence=cursor,
            subtree=True,
            timeout_seconds=0,
        )
    )
    assert [event.call_id for event in waited] == ["codex-child-progress-1"]

    persisted = manager.to_dict("workspace-1")
    restored_wm = _FakeWorkspaceManager(tmp_path)
    restored = AgentTreeManager(restored_wm)  # type: ignore[arg-type]
    restored.load_from_dict("workspace-1", persisted)
    restored_child = restored.get_run(child.id)

    assert restored_child is not None
    assert restored_child.executor_config == child.executor_config
    assert restored_child.executor_capabilities == child.executor_capabilities
    assert "codex-child-progress-1" in {
        event.call_id
        for event in restored.get_events("workspace-1", root.id, since_sequence=0, subtree=True)
    }
