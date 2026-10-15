"""One attempted continuation per eligible Chat interrupted by a planned restart.

The existing restart mailbox owns intent and claims. Provider acceptance cannot
be made atomic with that mailbox: a claimed attempt is never automatically
replayed, including after a crash. No original user prompt or image is resent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from ..models import TERMINAL_GOAL_STATUSES, AgentType, SessionKind
from . import service_restart as restart
from .service_restart import ChatContinuation, ChatRecovery

logger = logging.getLogger(__name__)
CONTINUATION_TEXT = "Please continue."
MAX_RECOVERY_AGE_SECONDS = 300


class ContinuationSkipped(RuntimeError):
    pass


def _recovery(operation: Any) -> ChatRecovery | None:
    if not isinstance(operation, dict):
        return None
    try:
        return ChatRecovery.model_validate(operation.get("chat_recovery"))
    except ValidationError:
        return None


def _operation_matches(operation: Any, *, capturing: bool) -> bool:
    if not isinstance(operation, dict) or operation.get("resume_running_chats") is not True:
        return False
    if not isinstance(operation.get("instance_id"), str) or not operation["instance_id"]:
        return False
    if not isinstance(operation.get("id"), str) or not operation["id"]:
        return False
    status = operation.get("status")
    if not isinstance(status, str) or status not in (
        {"restarting"} if capturing else {"restarting", "succeeded"}
    ):
        return False
    same_instance = operation.get("instance_id") == restart.INSTANCE_ID
    return same_instance if capturing else not same_instance


def _config_digest(tab: Any) -> str:
    # Include launch identity/configuration, never mutable timestamps, display
    # names or live status. Only its digest is retained, never credentials.
    values = {
        name: getattr(tab, name, None)
        for name in (
            "agent_type",
            "session_kind",
            "chat_mode",
            "cwd",
            "env",
            "target",
            "remote_profile_id",
            "remote_cwd",
            "solo_mode",
            "cursor_transport",
            "cursor_data_dir",
            "cursor_cli_version",
        )
    }
    effective_env = {**os.environ, **tab.env}
    values["provider_environment"] = {
        key: value
        for key, value in effective_env.items()
        if (
            key.startswith(
                (
                    "ANTHROPIC_",
                    "CLAUDE_",
                    "CODEX_",
                    "OPENAI_",
                    "CURSOR_",
                    "TRAE_",
                    "MASO_",
                )
            )
            and not key.startswith("CLAUDE_HUB_")
        )
        or key
        in {
            "HOME",
            "XDG_CONFIG_HOME",
            "MODEL",
            "API_KEY",
            "API_BASE_URL",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        }
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def _cursor_database_exists(transport: Any) -> bool:
    """Cursor's constructive --resume needs an authoritative existing DB."""
    from ._cursor_verify import _cwd_hash, _read_agent_id_from_db

    env = transport._build_env()
    home = Path(env.get("HOME") or str(Path.home()))
    root = Path(env.get("CURSOR_HOME") or str(home / ".cursor"))
    if str(root).startswith("~/"):
        root = home / str(root)[2:]
    if not root.is_absolute():
        root = Path(transport._cwd) / root
    data_dir = env.get("CURSOR_DATA_DIR")
    if data_dir and Path(data_dir).resolve() != root.resolve():
        return False
    bucket = root / "chats" / _cwd_hash(transport._cwd)
    matches = 0
    try:
        if bucket.is_symlink():
            return False
        for index, chat in enumerate(bucket.iterdir()):
            if index >= 1000:
                return False
            db = chat / "store.db"
            if chat.is_symlink() or db.is_symlink() or not db.is_file():
                continue
            if db.resolve().parent.parent != bucket.resolve():
                continue
            if _read_agent_id_from_db(db) == transport._conversation_id:
                matches += 1
        return matches == 1
    except OSError:
        return False


def _eligible_tab(tab_id: str) -> Any:
    from . import ttyd_manager, workspace_manager
    from .goal_run import get_goal_manager

    tab = ttyd_manager.get_tab(tab_id)
    if (
        tab is None
        or getattr(tab, "archived", False)
        or tab.session_kind != SessionKind.CHAT
        or getattr(tab, "workspace_id", None)
        or getattr(tab, "workspace_role", None)
        or str(getattr(tab, "chat_mode", "default")) in {"plan", "ChatMode.PLAN"}
        or not getattr(tab, "agent_session_id_verified", False)
        or not tab.agent_session_id
    ):
        raise ContinuationSkipped("Chat is no longer eligible for automatic continuation")
    if any(session.tab_id == tab_id for session in workspace_manager.sessions.values()):
        raise ContinuationSkipped("Managed work retains its own recovery owner")
    goal = get_goal_manager().current(tab_id)
    if goal is not None and (
        goal.status not in TERMINAL_GOAL_STATUSES or goal.dispatch_state.value != "idle"
    ):
        raise ContinuationSkipped("The Goal retains its own recovery controls")
    if any(
        run.tab_id == tab_id and workspace_manager._chat_run_active(run)
        for run in workspace_manager.scheduled_task_runs.values()
    ):
        raise ContinuationSkipped("Scheduled work retains its own recovery owner")
    return tab


async def capture_interrupted_chats() -> None:
    """Called after send admission closes, before tailers terminalize turns."""
    from ..api import agent_stream as api

    state = restart.get_store().read()
    operation = state.get("operation") or {}
    launcher_id = os.environ.get("CLAUDE_HUB_LAUNCHER_ID")
    if (
        not _operation_matches(operation, capturing=True)
        or not launcher_id
        or state.get("launcher_id") != launcher_id
        or "chat_recovery" in operation
    ):
        return
    targets: list[dict[str, Any]] = []
    manager = api._tab_tailer_manager
    for tailer in manager.tailers() if manager is not None else []:
        async with tailer._send_lock:
            transport = tailer.native_transport
            turn_id = tailer._active_turn_id
            if (
                transport is None
                or not transport.turn_in_flight
                or not turn_id
                or tailer.hard_failed
                or tailer.native_error
                or transport.last_error
                or tailer._turn_teardown_pending
                or tailer._turn_completed_seen
                or tailer._pending_approvals
                or tailer._blocking_approval_call_ids
                or tailer._waiting_for_model_capacity
                or tailer._goal_protocol_turn
                or getattr(transport, "_delivery_uncertain", False)
            ):
                continue
            try:
                tab = _eligible_tab(transport.session.tab_id)
                if transport._conversation_id != tab.agent_session_id:
                    continue
                turn = await tailer.store.find_turn_with_metadata(turn_id)
                if turn is None or turn[4].get("origin", "web") not in {
                    "web",
                    "restart_continuation",
                }:
                    continue
                target = ChatContinuation(
                    tab_id=tab.id,
                    turn_id=turn_id,
                    start_sequence=turn[0],
                    conversation_id=tab.agent_session_id,
                    config_digest=_config_digest(tab),
                    continuation_id=str(
                        uuid5(
                            NAMESPACE_URL,
                            f"claude-hub:restart:{operation['id']}:{tab.id}:{turn_id}",
                        )
                    ),
                )
                targets.append(target.model_dump())
            except ContinuationSkipped:
                continue
    with restart.get_store().locked() as current:
        live = current.get("operation")
        if (
            isinstance(live, dict)
            and _operation_matches(live, capturing=True)
            and live["id"] == operation["id"]
        ):
            live.setdefault(
                "chat_recovery",
                ChatRecovery(
                    launcher_id=launcher_id,
                    captured_at=time.time(),
                    targets=[ChatContinuation.model_validate(row) for row in targets],
                ).model_dump(),
            )


def _change_target(
    operation_id: str, target: ChatContinuation, state: str, reason: str = ""
) -> bool:
    with restart.get_store().locked() as current:
        operation = current.get("operation") or {}
        if _recovery(operation) is None or operation.get("id") != operation_id:
            return False
        for row in operation.get("chat_recovery", {}).get("targets", []):
            if row.get("continuation_id") == target.continuation_id:
                expected = "pending" if state == "claimed" else "claimed"
                if row.get("state") != expected:
                    return False
                row.update(state=state, reason=reason)
                if state == "claimed":
                    row["claimed_by_instance_id"] = restart.INSTANCE_ID
                return True
    return False


def _assert_claim(operation_id: str, target: ChatContinuation) -> None:
    state = restart.get_store().read()
    operation = state.get("operation") or {}
    recovery = _recovery(operation)
    if recovery is None or not 0 <= time.time() - recovery.captured_at <= MAX_RECOVERY_AGE_SECONDS:
        raise ContinuationSkipped(
            "Automatic continuation expired; check the Chat before continuing"
        )
    if (
        recovery.launcher_id == os.environ.get("CLAUDE_HUB_LAUNCHER_ID")
        and state.get("launcher_id") == recovery.launcher_id
        and operation.get("id") == operation_id
        and _operation_matches(operation, capturing=False)
    ):
        for row in operation.get("chat_recovery", {}).get("targets", []):
            if (
                row.get("continuation_id") == target.continuation_id
                and row.get("state") == "claimed"
                and row.get("claimed_by_instance_id") == restart.INSTANCE_ID
            ):
                return
    raise ContinuationSkipped("Automatic continuation was cancelled or superseded")


async def _verify_interruption(store: Any, target: ChatContinuation) -> None:
    from .agent_stream.store import TurnLifecycleState
    from .agent_stream.tailer import _RUNTIME_INTERRUPTED_MESSAGE

    snapshot = await store.inspect_turn_lifecycle(target.continuation_id)
    if snapshot.state != TurnLifecycleState.NOT_STARTED or snapshot.latest_unfinished is not None:
        raise ContinuationSkipped("A turn has already started; automatic replay is disabled")
    sequence = target.start_sequence - 1
    latest_start = None
    interrupted = False
    cancelled = False
    while True:
        page = await store.read_since(sequence, 500, compact=False)
        for event in page.events:
            if event.type == "turn_started":
                latest_start = event
            if event.turn_id == target.turn_id:
                if (
                    event.type == "error"
                    and event.payload.get("message") == _RUNTIME_INTERRUPTED_MESSAGE
                ):
                    interrupted = True
                if event.type == "turn_completed":
                    cancelled = event.payload.get("status") == "cancelled"
        if not page.has_more:
            break
        sequence = page.next_sequence
    if (
        latest_start is None
        or latest_start.turn_id != target.turn_id
        or latest_start.stream_sequence != target.start_sequence
        or not interrupted
        or not cancelled
    ):
        raise ContinuationSkipped("The original turn was completed, stopped, or superseded")


async def _continue_chat(operation_id: str, target: ChatContinuation) -> None:
    from ..api import agent_stream as api
    from .agent_stream.store import AgentStreamStore
    from .goal_run import get_goal_admission_lock

    transport = None
    try:
        async with get_goal_admission_lock(target.tab_id):

            def validate_identity() -> None:
                _assert_claim(operation_id, target)
                tab = _eligible_tab(target.tab_id)
                if (
                    tab.agent_session_id != target.conversation_id
                    or _config_digest(tab) != target.config_digest
                ):
                    raise ContinuationSkipped("Chat configuration or conversation changed")

            async def validate() -> None:
                validate_identity()
                await _verify_interruption(store, target)
                if transport is not None and session.agent_type == AgentType.CURSOR:
                    if not await asyncio.to_thread(_cursor_database_exists, transport):
                        raise ContinuationSkipped(
                            "The original Cursor conversation store could not be verified"
                        )
                _assert_claim(operation_id, target)

            session = api._terminal_tab_session_or_404(target.tab_id)
            store = AgentStreamStore(session.workspace_id, session.id)
            await validate()
            manager = api._get_tab_tailer_manager()
            tailer = await manager.ensure_started(
                session, required_conversation_id=target.conversation_id
            )
            transport = tailer.native_transport
            if transport is None:
                raise ContinuationSkipped("Native provider is unavailable")
            await transport.start()
            transport.check_resumed_conversation()
            await manager.send_message(
                session,
                CONTINUATION_TEXT,
                [],
                target.continuation_id,
                before_send=validate,
                before_delivery=validate_identity,
                turn_metadata={
                    "origin": "restart_continuation",
                    "restart_id": operation_id,
                    "interrupted_turn_id": target.turn_id,
                },
            )
        _change_target(operation_id, target, "started")
    except ContinuationSkipped as exc:
        _change_target(operation_id, target, "skipped", str(exc))
    except asyncio.CancelledError:
        # A claimed attempt may have reached the provider. Never replay it.
        _change_target(
            operation_id,
            target,
            "failed",
            "Continuation interrupted; check the Chat before continuing",
        )
        raise
    except Exception:
        logger.exception("Automatic restart continuation needs attention for tab %s", target.tab_id)
        _change_target(
            operation_id,
            target,
            "failed",
            "Automatic continuation failed; check the Chat before continuing",
        )
    finally:
        if transport is not None:
            transport.require_resumed_conversation(None)


async def continue_interrupted_chats() -> None:
    """Consume only this launcher's valid planned-restart intent, at most once."""
    state = restart.get_store().read()
    operation = state.get("operation") or {}
    if not _operation_matches(operation, capturing=False):
        return
    recovery = _recovery(operation)
    if recovery is None:
        return
    if (
        recovery.launcher_id != os.environ.get("CLAUDE_HUB_LAUNCHER_ID")
        or state.get("launcher_id") != recovery.launcher_id
    ):
        return
    for target in recovery.targets:
        if target.state == "claimed" and target.claimed_by_instance_id != restart.INSTANCE_ID:
            _change_target(
                operation["id"],
                target,
                "failed",
                "A prior continuation attempt may have been sent; check the Chat before continuing",
            )
    if not 0 <= time.time() - recovery.captured_at <= MAX_RECOVERY_AGE_SECONDS:
        for target in recovery.targets:
            if target.state == "pending" and _change_target(operation["id"], target, "claimed"):
                _change_target(
                    operation["id"],
                    target,
                    "failed",
                    "Automatic continuation expired; check the Chat before continuing",
                )
        return
    semaphore = asyncio.Semaphore(4)

    async def attempt(target: ChatContinuation) -> None:
        async with semaphore:
            if _change_target(operation["id"], target, "claimed"):
                await _continue_chat(operation["id"], target)

    targets = [target for target in recovery.targets if target.state == "pending"]
    await asyncio.gather(*(attempt(target) for target in targets))


def cancel_pending_continuation(tab_id: str, expected_turn_id: str | None) -> str | None:
    """A human Stop also retires the corresponding queued/started continuation."""
    with restart.get_store().locked() as current:
        operation = current.get("operation") or {}
        if not _operation_matches(operation, capturing=False) or _recovery(operation) is None:
            return None
        for row in operation.get("chat_recovery", {}).get("targets", []):
            if row.get("tab_id") == tab_id and expected_turn_id is None:
                if row.get("state") in {"pending", "claimed"}:
                    row.update(state="skipped", reason="Stopped by the user")
                # Generic Stop must still stop the current turn, including a
                # newer manual turn after an earlier continuation completed.
                return None
            if (
                row.get("tab_id") == tab_id
                and row.get("state") in {"pending", "claimed", "started"}
                and expected_turn_id in {None, row.get("turn_id"), row.get("continuation_id")}
            ):
                row.update(state="skipped", reason="Stopped by the user")
                return str(row["continuation_id"])
    return None
