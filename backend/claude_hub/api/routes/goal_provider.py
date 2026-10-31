"""Bind Goal user operations to the current direct Chat provider thread."""

from __future__ import annotations

from ...models import GoalRun
from ...services.agent_stream.providers.contracts import ProviderGoal, ProviderGoalSnapshot
from ...services.goal_run.images import pending_goal_images
from ...services.goal_run.native import NativeGoalConnection, NativeGoalError


async def resolve_goal_provider(tab_id: str) -> NativeGoalConnection | None:
    from ...services.agent_stream.providers.spec import NativeGoals, get_spec
    from ..agent_stream import (
        _get_tab_tailer_manager,
        _is_chat_native,
        _terminal_tab_session_or_404,
    )

    session = _terminal_tab_session_or_404(tab_id)
    if not _is_chat_native(session) or get_spec(session.agent_type).goals.api == NativeGoals.NONE:
        return None
    manager = _get_tab_tailer_manager()
    tailer = await manager.ensure_started(session)
    transport = tailer.native_transport
    if transport is None or tailer.native_error:
        raise NativeGoalError(tailer.native_error or "Native Chat transport is unavailable")

    def require_current() -> None:
        if not manager.session_is_current(session) or not tailer.matches_session(session):
            raise NativeGoalError("Chat provider changed during the Goal operation")

    async def read() -> ProviderGoalSnapshot | None:
        require_current()
        snapshot = await transport.goal_snapshot()
        require_current()
        return snapshot

    require_current()
    # ensure_started owns the consumer task; the provider handshake may still
    # be in progress when a cold Chat's Goal GET arrives beside capabilities.
    await transport.start()
    require_current()
    snapshot = await read()
    if snapshot is None:
        return None
    thread_id = snapshot["thread_id"]

    def require_thread() -> None:
        require_current()
        if transport.active_thread_id != thread_id:
            raise NativeGoalError("Chat provider thread changed during the Goal operation")

    async def set_goal(goal: ProviderGoal) -> ProviderGoal:
        require_thread()
        result = await transport.goal_set(goal, origin="user")
        require_thread()
        return result

    async def clear_goal() -> bool:
        require_thread()
        result = await transport.goal_clear(origin="user")
        require_thread()
        return result

    async def active_turn_id() -> str | None:
        require_thread()
        return await manager.active_turn_id(session)

    async def stop_turn() -> None:
        require_thread()
        turn_id = await manager.active_turn_id(session)
        if turn_id is not None:
            await manager.cancel_turn(session, expected_turn_id=turn_id)
        require_thread()
        # A native automatic start may already be queued before the consumer
        # has assigned its Hub turn ID. The provider fences that queue too.
        await transport.cancel_active_turn()
        require_thread()
        if transport.turn_in_flight:
            raise NativeGoalError("Provider Goal paused, but the running turn has not stopped")

    async def send_input(goal: GoalRun, text: str, turn_id: str) -> None:
        from ..agent_stream import AgentStreamSendRequest, _send_to_native

        require_thread()
        await _send_to_native(
            session,
            AgentStreamSendRequest(
                text=text,
                attachments=pending_goal_images(goal),
                client_turn_id=turn_id,
            ),
            manager,
            turn_metadata={"origin": "web", "goal_execution_owner": "provider_native"},
        )
        require_thread()

    return NativeGoalConnection(
        snapshot=snapshot,
        read=read,
        set_goal=set_goal,
        clear_goal=clear_goal,
        stop_turn=stop_turn,
        active_turn_id=active_turn_id,
        send_input=send_input,
        turn_in_flight=lambda: transport.turn_in_flight,
    )
