"""Agent Workspace provisioning and managed-Chat dispatch for Feishu ``/ai``.

Extracted from ``feishu_ai_command.py`` to keep that service under the
repository's source-size ceiling while isolating the managed-workspace concern.

Production wiring drives the singleton workspace manager and the shared managed
Chat tailer. The service layer keeps thin delegating methods (and their test
injection seams) that call the functions in this module.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from claude_hub.models import AgentType, SessionKind
from claude_hub.services.feishu_ai_command_models import (
    DEFAULT_AI_COMMAND_WORKSPACE_NAME,
    FeishuAiCommandConfig,
    ParsedAiCommand,
)
from claude_hub.services.feishu_user_client import FeishuUserMessage

logger = logging.getLogger(__name__)


def channel_key(message: FeishuUserMessage, parsed: ParsedAiCommand) -> str:
    return f"{message.chat_id}:{message.thread_id or 'main'}:{parsed.provider.value}"


def resolve_cwd(cfg: FeishuAiCommandConfig) -> str:
    cwd = cfg.cwd.strip() or str(Path.home())
    if not os.path.isdir(cwd):
        cwd = str(Path.home())
    return cwd


def normalize_channel_record(raw: Any) -> dict[str, str]:
    """Normalize a persisted channel_tabs entry (dict or legacy plain string)."""
    if isinstance(raw, Mapping):
        return {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v}
    if isinstance(raw, str) and raw.strip():
        return {"tab_id": raw.strip()}
    return {}


def recover_completed_stream_answer(
    tab_id: str,
    turn_id: str,
    *,
    managed_workspace_id: str | None = None,
    managed_session_id: str | None = None,
) -> tuple[str, str | None]:
    """Inspect durable AgentStreamStore evidence without starting a runtime.

    Managed workspace Chat sessions persist events under ``(workspace_id, session_id)``;
    legacy standalone tabs use the ``terminal-tabs`` namespace. Both are checked so an
    interrupted command recovers regardless of where its tab was created.
    """
    if not turn_id:
        return "absent", None
    try:
        from claude_hub.models import AgentStreamEventType
        from claude_hub.services.agent_stream.store import AgentStreamStore

        candidates: list[AgentStreamStore] = []
        if managed_workspace_id and managed_session_id:
            candidates.append(AgentStreamStore(managed_workspace_id, managed_session_id))
        if tab_id:
            candidates.append(AgentStreamStore("terminal-tabs", f"terminal-tab-{tab_id}"))
        if not candidates:
            return "absent", None
        events = []
        for store in candidates:
            events.extend(store._read_rows(-1, None, turn_id))
    except Exception:
        return "unknown", None
    started = False
    for event in events:
        if event.turn_id != turn_id:
            continue
        if event.type == AgentStreamEventType.TURN_STARTED:
            started = True
        elif event.type == AgentStreamEventType.TURN_COMPLETED:
            if event.payload.get("delivery_rejected"):
                started = True
                continue
            status = event.payload.get("status")
            if status in {None, "completed", "success", "ok"}:
                text = str(
                    event.payload.get("final_text") or event.payload.get("assistant_text") or ""
                ).strip()
                return "completed", (text or None)
            return ("cancelled" if status in {"cancelled", "canceled"} else "failed"), None
    return ("started" if started else "absent"), None


def provider_supports_managed_chat(agent_type: AgentType) -> bool:
    """True when ``agent_type`` is admitted to the managed workspace Chat surface.

    Direct-Chat-only providers (e.g. TraeX) cannot run as managed sessions; the
    caller keeps them on the standalone-tab path.
    """
    from claude_hub.services.agent_stream.providers.spec import Surface, get_spec

    return get_spec(agent_type).runs_on(Surface.MANAGED_CHAT)


async def ensure_execution_tab(service: Any, message: Any, parsed: Any, cfg: Any) -> str:
    """Resolve (reusing or creating) the Chat tab that owns one /ai command.

    Channels already mapped to a live, non-archived tab reuse it (hot-switching
    env when the model/effort changed). Otherwise a managed workspace Chat is
    provisioned, except for the legacy standalone seam or direct-Chat-only
    providers, which keep an ungrouped standalone tab. All state access goes
    through ``service`` so its test injection seams stay authoritative.
    """
    from claude_hub.services.feishu_ai_command_models import resolve_ai_command_agent_env

    channel_key = service._channel_key(message, parsed)
    with service._lock:
        existing_record = dict(service._load_state().get("channel_tabs") or {}).get(channel_key)
    existing = service._normalize_channel_record(existing_record)

    target_env = parsed.effective_env
    if existing and existing.get("tab_id"):
        tab = service._get_tab(str(existing["tab_id"]))
        if tab is not None and not getattr(tab, "archived", False):
            current_env = dict(getattr(tab, "env", {}) or {})
            merged_env = resolve_ai_command_agent_env(
                parsed.provider,
                parsed.model,
                parsed.reasoning_effort,
                base_env=current_env,
            )
            if merged_env != current_env:
                if existing.get("session_id") and not service._uses_legacy_tabs():
                    await service._switch_managed_env(existing["session_id"], merged_env)
                else:
                    await service._switch_tab_env(str(existing["tab_id"]), merged_env, tab=tab)
            return str(existing["tab_id"])

    if service._uses_legacy_tabs() or not provider_supports_managed_chat(parsed.provider):
        tab_id = str(await service._create_legacy_tab(message, parsed, cfg, target_env))
        service._store_channel_record(channel_key, {"tab_id": tab_id})
        return tab_id

    # Default path: provision the Chat tab inside an Agent Workspace so it
    # renders under that workspace rather than the bare Terminal/cwd list.
    cwd = resolve_cwd(cfg)
    workspace = service._resolve_workspace(cfg, cwd)
    short_target = (message.thread_id or message.chat_id)[-6:]
    session = await service._provide_managed_session(
        workspace_id=str(workspace.id),
        agent_type=parsed.provider,
        env=target_env,
        title=f"Feishu /ai · {short_target}"[:64],
    )
    tab_id = str(getattr(session, "tab_id", "") or "").strip()
    session_id = str(getattr(session, "id", "") or "").strip()
    if not tab_id:
        raise RuntimeError("Failed to create a workspace Chat tab for Feishu /ai command")
    service._store_channel_record(
        channel_key,
        {
            "tab_id": tab_id,
            "session_id": session_id,
            "workspace_id": str(workspace.id),
        },
    )
    return tab_id


def resolve_workspace(
    cfg: FeishuAiCommandConfig,
    cwd: str,
    *,
    resolver_seam: Callable[[FeishuAiCommandConfig, str], Any] | None = None,
) -> Any:
    """Return the workspace that owns /ai Chat tabs, creating it on demand.

    Uses the injected resolver seam when provided; otherwise drives the
    singleton workspace manager. A configured but missing workspace id never
    fails silently: it falls back to the dedicated default workspace.
    """
    if resolver_seam is not None:
        return resolver_seam(cfg, cwd)

    from claude_hub.models import WorkspaceCreate
    from claude_hub.services.workspace_manager import workspace_manager

    requested = cfg.workspace_id.strip()
    if requested:
        workspace = workspace_manager.get_workspace(requested)
        if workspace is not None:
            return workspace
        logger.warning(
            "Feishu /ai configured workspace_id=%s is missing; falling back to default workspace",
            requested,
        )

    for workspace in workspace_manager.list_workspaces():
        if workspace.name == DEFAULT_AI_COMMAND_WORKSPACE_NAME:
            return workspace

    return workspace_manager.create_workspace(
        WorkspaceCreate(name=DEFAULT_AI_COMMAND_WORKSPACE_NAME, path=cwd)
    )


async def provide_managed_session(
    *,
    workspace_id: str,
    agent_type: AgentType,
    env: Mapping[str, str],
    title: str,
    provider_seam: Callable[..., Any] | None = None,
) -> Any:
    """Provision (or reuse) a managed WORKER Chat session in ``workspace_id``."""
    if provider_seam is not None:
        res = provider_seam(
            workspace_id=workspace_id,
            agent_type=agent_type,
            env=dict(env),
            title=title,
        )
        return await res if inspect.isawaitable(res) else res

    from claude_hub.models import EnsureWorkspaceAgentRequest, WorkspaceSessionRole
    from claude_hub.services.workspace_manager import workspace_manager

    return await workspace_manager.ensure_workspace_agent(
        workspace_id,
        EnsureWorkspaceAgentRequest(
            agent_type=agent_type,
            session_kind=SessionKind.CHAT,
            role=WorkspaceSessionRole.WORKER,
            title=title,
            solo_mode=True,
            reuse_existing=True,
            env=dict(env),
        ),
        bootstrap=False,
    )


def get_managed_session(session_id: str, *, getter_seam: Callable[[str], Any] | None) -> Any:
    """Return a live, non-archived managed session, or ``None``."""
    if getter_seam is not None:
        session = getter_seam(session_id)
    else:
        from claude_hub.services.workspace_manager import workspace_manager

        session = workspace_manager.sessions.get(session_id)
    if session is None or getattr(session, "archived", False):
        return None
    return session


async def switch_managed_env(session_id: str, env: Mapping[str, str]) -> None:
    """Hot-swap a managed workspace Chat session's launch env.

    Mirrors the HTTP switch-env route's managed branch without its web layer:
    update the backing tab, persist the session configuration, then sync the
    live managed tailer so the next turn uses the new model/effort.
    """
    from claude_hub.api.agent_stream import get_managed_tailer_manager
    from claude_hub.services import ttyd_manager
    from claude_hub.services.workspace_manager import workspace_manager

    session = workspace_manager.sessions.get(session_id)
    if session is None:
        raise KeyError(session_id)
    tab = await ttyd_manager.switch_env(session.tab_id, dict(env), solo_mode=session.solo_mode)
    updated = workspace_manager.update_managed_chat_configuration(
        session_id,
        tab_id=tab.id,
        env=dict(tab.env),
        solo_mode=tab.solo_mode,
    )
    get_managed_tailer_manager().set_env(updated, dict(updated.env))


async def dispatch_managed_turn(
    session: Any,
    prompt: str,
    turn_id: str,
    visible_text: str,
    turn_metadata: Mapping[str, Any],
    *,
    dispatcher_seam: Callable[..., Any] | None = None,
) -> str:
    """Send one turn to a managed Chat session and await its final answer."""
    if dispatcher_seam is not None:
        res = dispatcher_seam(session, prompt, turn_id, visible_text, dict(turn_metadata))
        return str(await res if inspect.isawaitable(res) else res)

    from claude_hub.api.agent_stream import get_managed_tailer_manager
    from claude_hub.models import AgentStreamEventType
    from claude_hub.services.agent_stream.base import is_substantive_answer

    manager = get_managed_tailer_manager()
    queue: asyncio.Queue[Any] | None = None
    last_error = ""
    try:
        queue = await manager.subscribe_completion(session)
        await manager.send_message(
            session,
            prompt,
            [],
            turn_id,
            visible_text=visible_text,
            turn_metadata=dict(turn_metadata),
            allow_question_answer=False,
        )
        while True:
            event = await queue.get()
            if event.turn_id != turn_id:
                continue
            if event.type == AgentStreamEventType.APPROVAL_REQUIRED:
                retirement = await manager.retire_external_turn(session, expected_turn_id=turn_id)
                raise RuntimeError(
                    "Feishu /ai turn requires Web Chat interaction "
                    f"(retirement={retirement.value})"
                )
            if event.type == AgentStreamEventType.ERROR:
                message = event.payload.get("message")
                if isinstance(message, str):
                    last_error = message
                continue
            if event.type != AgentStreamEventType.TURN_COMPLETED:
                continue
            status_value = event.payload.get("status")
            if status_value not in {None, "completed", "success", "ok"}:
                error_message = event.payload.get("error_message")
                raise RuntimeError(
                    last_error
                    or (error_message if isinstance(error_message, str) else "")
                    or f"Chat turn ended with status {status_value}"
                )
            assistant_text = event.payload.get("assistant_text")
            if not isinstance(assistant_text, str) or not is_substantive_answer(assistant_text):
                raise RuntimeError(last_error or "Chat turn completed without assistant text")
            final_text = event.payload.get("final_text")
            if isinstance(final_text, str) and is_substantive_answer(final_text):
                return final_text.strip()
            return assistant_text.strip()
    except asyncio.CancelledError:
        # Retire the matching native turn before the dedup claim is released,
        # mirroring the tab dispatch path so a provider turn is not left running
        # against a tailer that lifespan teardown is about to stop.
        try:
            await asyncio.shield(manager.retire_external_turn(session, expected_turn_id=turn_id))
        except Exception:
            logger.warning(
                "Failed to retire Feishu /ai managed turn %s on cancellation",
                turn_id,
                exc_info=True,
            )
        raise
    finally:
        if queue is not None:
            manager.unsubscribe_completion(session.id, queue)


async def dispatch_turn(
    service: Any,
    tab_id: str,
    prompt: str,
    turn_id: str,
    visible_text: str,
    turn_metadata: Mapping[str, Any],
) -> str:
    """Route one /ai turn to its managed session or the standalone tab path."""
    if service._turn_dispatcher is not None:
        return str(
            await service._turn_dispatcher(tab_id, prompt, turn_id, visible_text, turn_metadata)
        )

    record = service._channel_record_for_tab(tab_id)
    session_id = record.get("session_id")
    if session_id and not service._uses_legacy_tabs():
        session = service._get_managed_session(session_id)
        if session is not None:
            return str(
                await service._dispatch_managed_turn(
                    session,
                    prompt,
                    turn_id,
                    visible_text,
                    turn_metadata,
                )
            )
        logger.warning(
            "Feishu /ai managed session %s for tab %s is gone; falling back to tab dispatch",
            session_id,
            tab_id,
        )

    from claude_hub.api.agent_stream import dispatch_tab_chat_and_wait

    return await dispatch_tab_chat_and_wait(
        tab_id,
        prompt,
        turn_id,
        visible_text=visible_text,
        turn_metadata=dict(turn_metadata),
    )
