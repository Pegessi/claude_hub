"""Managed Task worker/reviewer session validation and launch helpers."""

from __future__ import annotations

from claude_hub.models.schemas import (
    CHAT_REMOTE_UNSUPPORTED,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    SessionKind,
)
from claude_hub.models.task_session import ExecutorCapabilities, ManagedExecutorConfig

from .agent_stream.providers.spec import (
    Surface,
    all_specs,
    get_spec,
    providers_for_surface,
)


def validate_config_admission(config: ManagedExecutorConfig) -> None:
    """Validate the provider, surface, and execution target combination."""

    spec = get_spec(config.agent_type)
    if config.session_kind == SessionKind.CHAT:
        if not spec.runs_on(Surface.MANAGED_CHAT):
            # Preserve the historical terminal/traex-specific rejection text.
            if config.agent_type in {AgentType.TERMINAL, AgentType.TRAEX}:
                raise ValueError(
                    f"Unsupported managed session agent_type: {config.agent_type.value}"
                )
            raise ValueError(
                "Managed Chat supports only local Claude, Codex, Cursor, MASO, or Antigravity agents"
            )
        if config.target == ExecutionTarget.REMOTE:
            raise ValueError(CHAT_REMOTE_UNSUPPORTED)
    elif not spec.runs_on(Surface.MANAGED_TERMINAL):
        if config.agent_type == AgentType.MASO:
            raise ValueError("MASO is an HTTP Chat provider; select a Chat session")
        if config.agent_type == AgentType.ANTIGRAVITY:
            raise ValueError("Antigravity is a Chat provider in workspaces; select a Chat session")
        raise ValueError(f"Unsupported managed session agent_type: {config.agent_type.value}")


def launch_env(config: ManagedExecutorConfig) -> dict[str, str]:
    """Return the exact persisted launch environment for ``config``."""

    validate_config_admission(config)

    result = dict(config.env)
    model = config.model.strip() if config.model else None
    if config.model is not None and not model:
        raise ValueError("executor_config.model must not be blank")
    if not model:
        return result

    spec = get_spec(config.agent_type)
    if not spec.model_configurable_in_managed or spec.model_env_key is None:
        raise ValueError(
            f"{config.agent_type.value.capitalize()} executor does not support an explicit model override"
        )
    key = spec.model_env_key
    configured = result.get(key)
    if configured is not None and configured != model:
        raise ValueError(f"executor_config.model conflicts with executor_config.env[{key!r}]")
    result[key] = model
    return result


def config_from_session(session: ManagedSession) -> ManagedExecutorConfig:
    """Recover launch config from an existing managed session."""

    env = dict(session.env)
    model = None
    spec = get_spec(session.agent_type)
    if spec.model_configurable_in_managed and spec.model_env_key is not None:
        model = env.pop(spec.model_env_key, None)
    return ManagedExecutorConfig(
        agent_type=session.agent_type,
        session_kind=session.session_kind,
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
    resolved = config.model_copy(update=updates)
    validate_config_admission(resolved)
    return resolved


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
    if session.session_kind != config.session_kind:
        mismatches.append("session_kind")
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
    chat_agents = providers_for_surface(Surface.MANAGED_CHAT)
    return ExecutorCapabilities(
        available=True,
        supports_spawn=True,
        supports_send=True,
        supports_followup=True,
        supports_interrupt=True,
        durable_status=True,
        supported_agent_types=list(
            dict.fromkeys([*chat_agents, *providers_for_surface(Surface.MANAGED_TERMINAL)])
        ),
        supported_session_kinds=[SessionKind.TERMINAL, SessionKind.CHAT],
        chat_supported_agent_types=list(chat_agents),
        model_configurable_agent_types=[
            agent_type
            for agent_type, spec in all_specs().items()
            if spec.model_configurable_in_managed
        ],
    )
