"""Managed Task worker/reviewer session validation and launch helpers."""

from __future__ import annotations

from typing import Optional

from claude_hub.models.schemas import AgentType, ExecutionTarget, ManagedSession
from claude_hub.models.task_session import ExecutorCapabilities, ManagedExecutorConfig


def launch_env(config: ManagedExecutorConfig) -> dict[str, str]:
    """Return the exact persisted launch environment for ``config``."""

    if config.agent_type == AgentType.TERMINAL:
        raise ValueError("managed sessions do not support the terminal executor")
    if config.agent_type not in {AgentType.CLAUDE, AgentType.CODEX, AgentType.CURSOR}:
        raise ValueError(f"Unsupported managed session agent_type: {config.agent_type.value}")

    result = dict(config.env)
    model = config.model.strip() if config.model else None
    if config.model is not None and not model:
        raise ValueError("executor_config.model must not be blank")
    if not model:
        return result

    if config.agent_type == AgentType.CLAUDE:
        key = "ANTHROPIC_MODEL"
    elif config.agent_type == AgentType.CODEX:
        key = "CODEX_MODEL"
    else:
        raise ValueError("Cursor executor does not support an explicit model override")
    configured = result.get(key)
    if configured is not None and configured != model:
        raise ValueError(f"executor_config.model conflicts with executor_config.env[{key!r}]")
    result[key] = model
    return result


def config_from_session(session: ManagedSession) -> ManagedExecutorConfig:
    """Recover launch config from an existing managed session."""

    env = dict(session.env)
    model = None
    if session.agent_type == AgentType.CLAUDE:
        model = env.pop("ANTHROPIC_MODEL", None)
    elif session.agent_type == AgentType.CODEX:
        model = env.pop("CODEX_MODEL", None)
    return ManagedExecutorConfig(
        agent_type=session.agent_type,
        model=model,
        env=env,
        solo_mode=session.solo_mode,
        target=session.target,
        cwd=(session.workspace_path if session.target == ExecutionTarget.LOCAL else None),
        remote_profile_id=session.remote_profile_id,
        remote_cwd=session.remote_cwd,
        remote_reconnect=session.remote_reconnect,
    )


def resolve_config_for_workspace(
    config: ManagedExecutorConfig,
    *,
    workspace_id: str,
    workspace_target: ExecutionTarget,
    remote_profile_id: str | None,
    remote_cwd: str | None,
    remote_reconnect: bool | None,
) -> ManagedExecutorConfig:
    """Fill target/remote defaults from the workspace record."""

    target = config.target or workspace_target
    updates: dict[str, object] = {"target": target}
    if target == ExecutionTarget.REMOTE:
        updates.update(
            {
                "remote_profile_id": config.remote_profile_id or remote_profile_id,
                "remote_cwd": config.remote_cwd or remote_cwd,
                "remote_reconnect": (
                    config.remote_reconnect
                    if config.remote_reconnect is not None
                    else remote_reconnect
                ),
            }
        )
    return config.model_copy(update=updates)


def validate_session_matches_config(
    session: ManagedSession,
    config: ManagedExecutorConfig,
    *,
    workspace_id: str,
) -> None:
    """Reject a session that does not match the requested launch spec."""

    expected_env = launch_env(config)
    mismatches: list[str] = []
    if session.workspace_id != workspace_id:
        mismatches.append("workspace_id")
    if session.agent_type != config.agent_type:
        mismatches.append("agent_type")
    if session.solo_mode != config.solo_mode:
        mismatches.append("solo_mode")
    if session.target != config.target:
        mismatches.append("target")
    if session.env != expected_env:
        mismatches.append("env/model")
    if config.cwd is not None and session.workspace_path != config.cwd:
        mismatches.append("cwd")
    if config.target == ExecutionTarget.REMOTE:
        if session.remote_profile_id != config.remote_profile_id:
            mismatches.append("remote_profile_id")
        if session.remote_cwd != config.remote_cwd:
            mismatches.append("remote_cwd")
        if session.remote_reconnect != config.remote_reconnect:
            mismatches.append("remote_reconnect")
    if mismatches:
        raise ValueError(
            f"Session {session.id} does not match executor_config: " + ", ".join(mismatches)
        )


def managed_executor_capabilities() -> ExecutorCapabilities:
    return ExecutorCapabilities(
        available=True,
        supports_spawn=True,
        supports_send=True,
        supports_followup=True,
        supports_interrupt=True,
        durable_status=True,
        supported_agent_types=[
            AgentType.CLAUDE,
            AgentType.CODEX,
            AgentType.CURSOR,
        ],
        model_configurable_agent_types=[AgentType.CLAUDE, AgentType.CODEX],
    )
