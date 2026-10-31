"""Dot execution configuration and recoverable replacement of its Chat tab."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import AsyncExitStack
from typing import Any

from claude_hub.models import AgentType, SessionKind
from claude_hub.services.agent_stream.providers.spec import Surface, providers_for_surface
from claude_hub.services.dot.runtime.session import DotSessionService
from claude_hub.services.goal_run import get_goal_admission_lock, get_goal_manager
from claude_hub.services.ttyd_manager import ttyd_manager
from claude_hub.services.workspace_manager import workspace_manager

PROVIDERS = providers_for_surface(Surface.DOT)
_SCHEDULE_KEYS = ("wake_schedule_id", "tidy_schedule_id")
_ACTIVE_RUNS = {"queued", "waiting", "dispatching", "running", "uncertain"}


class DotExecutionConflict(RuntimeError):
    """Execution cannot change without discarding or misrouting existing work."""


def execution_view(service: DotSessionService) -> dict[str, Any]:
    """Read configuration separately from status: launch env may contain secrets."""
    tab_id = service.tab_id()
    tab = ttyd_manager.get_tab(tab_id) if tab_id else None
    value: dict[str, Any] = {
        "tab_id": tab.id if tab else None,
        "agent_type": tab.agent_type.value if tab else None,
        "env": dict(tab.env) if tab else {},
    }
    revision_input = {**value, "stored_tab_id": tab_id}
    value["revision"] = hashlib.sha256(
        json.dumps(revision_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    value["supported_agent_types"] = [provider.value for provider in PROVIDERS]
    return value


def execution_pending_for(service: DotSessionService, tab_id: str) -> bool:
    pending = service._peek_state().get("pending_execution")
    return isinstance(pending, dict) and tab_id in (
        pending.get("previous_tab_id"),
        pending.get("candidate_tab_id"),
    )


def _check_switchable(service: DotSessionService, tab_id: str) -> None:
    # Pairing generations and queued Feishu deliveries belong to the binding owner.
    # This operation never rewrites them as a side effect of changing provider.
    from claude_hub.api.feishu_bot import _pool

    if any(bot.binding and bot.binding.tab_id == tab_id for bot in _pool.snapshot().bots):
        raise DotExecutionConflict(
            "Unbind the Feishu Bot before switching Dot's backend, then bind it to the new Dot chat."
        )
    goal = get_goal_manager().current(tab_id)
    if goal is not None and (
        goal.status.value in {"active", "paused", "blocked"} or goal.dispatch_state.value != "idle"
    ):
        raise DotExecutionConflict("Finish or stop Dot's current Goal before switching backend.")
    owned = {service._peek_state().get(key) for key in _SCHEDULE_KEYS}
    if any(
        task.tab_id == tab_id and task.enabled and task.id not in owned
        for task in workspace_manager.scheduled_tasks.values()
    ):
        raise DotExecutionConflict(
            "Disable or retarget other schedules for this chat before switching Dot's backend."
        )
    if any(
        run.tab_id == tab_id and run.status.value in _ACTIVE_RUNS
        for run in workspace_manager.scheduled_task_runs.values()
    ):
        raise DotExecutionConflict(
            "Wait for Dot's pending scheduled turns before switching backend."
        )


async def _require_idle(service: DotSessionService, tab_id: str) -> None:
    from claude_hub.api.agent_stream import _get_tab_tailer_manager
    from claude_hub.services.agent_stream.store import AgentStreamStore

    tailer = _get_tab_tailer_manager().get_tailer(f"terminal-tab-{tab_id}")
    if tailer is not None and tailer.native_transport is not None:
        if tailer.native_transport.turn_in_flight:
            raise DotExecutionConflict(
                "Dot is responding. Wait for the current turn before switching backend."
            )
    stream = AgentStreamStore("terminal-tabs", f"terminal-tab-{tab_id}")
    # An uncertain MASO POST is not made safe by dropping the local transport.
    if stream.path.with_suffix(".maso-delivery.json").exists():
        raise DotExecutionConflict("Resolve Dot's pending MASO delivery before switching backend.")
    await service.sync_journal()
    if await stream.latest_unfinished_turn() is not None:
        raise DotExecutionConflict(
            "Recover or finish Dot's unfinished turn before switching backend."
        )
    _check_switchable(service, tab_id)


def _schedule_snapshot(service: DotSessionService) -> dict[str, Any]:
    state = service._peek_state()
    result: dict[str, Any] = {}
    for key in _SCHEDULE_KEYS:
        schedule_id = state.get(key)
        task = workspace_manager.scheduled_tasks.get(schedule_id or "")
        result[key] = {
            "id": schedule_id,
            "tab_id": task.tab_id if task else None,
            "enabled": task.enabled if task else None,
            "agent_type": task.agent_type.value if task and task.agent_type else None,
        }
    return result


async def _lock_tab_admission(stack: AsyncExitStack, tab_id: str) -> None:
    # Both entry modes use this inbox pump lock before Goal admission. Keep
    # rollback serialized with all native delivery, including direct messages.
    await stack.enter_async_context(
        workspace_manager._delivery_inbox()._pump_locks.setdefault(tab_id, asyncio.Lock())
    )
    await stack.enter_async_context(get_goal_admission_lock(tab_id))


async def recover_execution_locked(service: DotSessionService) -> None:
    """Roll back an uncommitted switch. Caller owns Dot's setup lock.

    The marker and final Dot identity use one atomic state-file replacement.
    An interrupted operation therefore either needs this rollback or has already
    committed the complete new identity; no credentials enter Dot's Git history.
    """
    pending = service._peek_state().get("pending_execution")
    if not isinstance(pending, dict):
        return
    candidate = str(pending["candidate_tab_id"])
    previous = pending.get("previous_tab_id")
    tab_ids = {candidate}
    if isinstance(previous, str):
        tab_ids.add(previous)
    async with AsyncExitStack() as stack:
        for tab_id in sorted(tab_ids):
            await _lock_tab_admission(stack, tab_id)
        await _rollback_execution(service, pending)


async def _rollback_execution(service: DotSessionService, pending: dict[str, Any]) -> None:
    candidate = str(pending["candidate_tab_id"])
    previous = pending.get("previous_tab_id")
    state = service._peek_state()
    restored: dict[str, Any] = {}
    for key, before in pending["schedules"].items():
        current_id = state.get(key)
        if current_id and current_id != before["id"]:
            workspace_manager.delete_scheduled_task(current_id)
        schedule_id = before["id"]
        if (
            schedule_id in workspace_manager.scheduled_tasks
            and before["tab_id"]
            and before["agent_type"]
        ):
            workspace_manager.restore_scheduled_chat_task_target(
                schedule_id,
                tab_id=before["tab_id"],
                enabled=bool(before["enabled"]),
                agent_type=AgentType(before["agent_type"]),
            )
        restored[key] = schedule_id
    # Keep the marker until runtime cleanup succeeds. A retry cannot create a
    # second candidate while cleanup of the first one is still uncertain.
    await ttyd_manager.archive_tab(candidate)
    old = ttyd_manager.get_tab(previous) if previous else None
    if old and old.archived and not pending["previous_archived"]:
        await ttyd_manager.unarchive_tab(old.id)
    for schedule_id in restored.values():
        if schedule_id:
            workspace_manager.drop_pending_scheduled_task_runs(
                schedule_id, "Dot backend replacement was rolled back", tab_id=candidate
            )
    service.store.update_state(**restored, pending_execution=None)


async def switch_execution(
    service: DotSessionService,
    *,
    expected_revision: str,
    agent_type: AgentType,
    env: dict[str, str],
) -> dict[str, Any]:
    if agent_type not in PROVIDERS:
        raise ValueError("Unsupported Dot execution backend")
    async with service.setup_lock():
        await recover_execution_locked(service)
        current = execution_view(service)
        if current["revision"] != expected_revision:
            raise DotExecutionConflict(
                "Dot's configuration changed. Reload it before applying your settings."
            )
        old_id = service.tab_id()
        old = ttyd_manager.get_tab(old_id) if old_id else None
        if old and old.agent_type == agent_type and dict(old.env) == env:
            return current
        async with AsyncExitStack() as stack:
            if old_id:
                await _lock_tab_admission(stack, old_id)
                # Deleting the tab does not delete its delivery/automation owners.
                _check_switchable(service, old_id)
                await _require_idle(service, old_id)
            service.store.ensure()
            created = await ttyd_manager.create_tab(
                name=service.store.read_identity()["name"],
                cwd=str(service.store.home),
                solo_mode=True,
                session_kind=SessionKind.CHAT,
                agent_type=agent_type,
                env=env,
            )
            await _lock_tab_admission(stack, created.id)
            pending = {
                "candidate_tab_id": created.id,
                "previous_tab_id": old_id,
                "previous_archived": bool(old and old.archived),
                "schedules": _schedule_snapshot(service),
            }
            try:
                service.store.update_state(pending_execution=pending)
                if old_id:
                    from claude_hub.api.agent_stream import _get_tab_tailer_manager

                    # Admission is blocked; stop also drains the final observer so
                    # the last exchange reaches memory before ownership changes.
                    await _require_idle(service, old_id)
                    await _get_tab_tailer_manager().forget_session(f"terminal-tab-{old_id}")
                    await service.sync_journal()
                    _check_switchable(service, old_id)
                    if old:
                        await ttyd_manager.archive_tab(old_id)
                    _check_switchable(service, old_id)
                if execution_view(service)["revision"] != expected_revision:
                    raise DotExecutionConflict(
                        "Dot's configuration changed. Reload it before applying your settings."
                    )
                # No await between schedule retargeting and the identity commit.
                # Disabled schedules and user-customized timing/text stay unchanged.
                service.ensure_schedules(created.id, migrate=True)
                service.store.update_state(
                    tab_id=created.id,
                    execution_agent_type=agent_type.value,
                    session_conversation_id=None,
                    session_prepared_generation=None,
                    session_turns=0,
                    session_started_at=None,
                    session_pack=None,
                    sent_turns=[],
                    journal_seq=-1,
                    rotate_requested=None,
                    pending_execution=None,
                )
            except BaseException:
                cleanup = asyncio.create_task(_rollback_execution(service, pending))
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                await cleanup
                raise
        # Configuration is already durable. Git history is secondary evidence;
        # its failure must not turn a committed switch into a misleading 503.
        try:
            await asyncio.to_thread(service.store.commit, "dot: execution backend")
        except Exception:
            logging.getLogger(__name__).exception("Dot execution saved; memory Git commit failed")
        return execution_view(service)
