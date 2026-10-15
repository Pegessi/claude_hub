"""Managed Chat control-plane state updates."""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from ...models import ChatMode, ManagedSession, SessionKind
from .. import managed_chat
from ._constants import _now


class _ChatControlsMixin:
    @asynccontextmanager
    async def managed_chat_interaction_guard(
        self,
        expected: ManagedSession,
        *,
        require_chat: bool = True,
    ) -> AsyncIterator[ManagedSession]:
        """Fence one managed incarnation against deletion and friendly-id reuse."""
        async with self.workspace_mutation_lock(expected.workspace_id):
            lock = self._pump_locks.setdefault(expected.id, asyncio.Lock())
            async with lock:
                live = self.sessions.get(expected.id)
                if (
                    live is None
                    or live.workspace_id != expected.workspace_id
                    or live.tab_id != expected.tab_id
                    or live.created_at != expected.created_at
                ):
                    raise KeyError(expected.id)
                if require_chat and live.session_kind != SessionKind.CHAT:
                    raise ValueError("Managed Chat interaction requires a Chat session")
                yield live

    @asynccontextmanager
    async def managed_chat_configuration_guard(
        self, session_id: str
    ) -> AsyncIterator[ManagedSession]:
        """Fence configuration updates from mailbox dispatch and active turns."""
        lock = self._pump_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            session = self.sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            if session.session_kind != SessionKind.CHAT:
                raise ValueError("Managed Chat configuration requires a Chat session")
            if (
                session.pending_call_ids
                or session.processing_call_ids
                or session.uncertain_call_ids
                or managed_chat.is_turn_running(session)
            ):
                raise RuntimeError(
                    "Wait for managed Chat delivery to become idle before changing configuration"
                )
            yield session

    def update_managed_chat_configuration(
        self,
        session_id: str,
        *,
        tab_id: str,
        env: dict[str, str],
        solo_mode: bool,
    ) -> ManagedSession:
        """Persist configuration already validated by the session's headless tab."""
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        if session.session_kind != SessionKind.CHAT:
            raise ValueError("Managed Chat configuration requires a Chat session")
        if session.tab_id != tab_id:
            raise ValueError("Managed Chat tab does not belong to the requested session")

        updated = session.model_copy(
            update={
                "env": dict(env),
                "solo_mode": solo_mode,
                "updated_at": _now(),
            }
        )
        self.sessions[session_id] = updated
        try:
            self._save_workspace_state(session.workspace_id)
        except Exception:
            self.sessions[session_id] = session
            raise
        return updated

    def set_managed_chat_mode(self, session_id: str, mode: str) -> None:
        """Persist the only Task-controlled mode supported by managed Chat."""
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        parsed = ChatMode(mode)
        if session.session_kind != SessionKind.CHAT:
            raise ValueError("Managed Chat mode requires a Chat session")
        if parsed != ChatMode.DEFAULT:
            raise ValueError("Managed Chat mode is controlled by task execution")

        updated = session.model_copy(update={"chat_mode": parsed, "updated_at": _now()})
        self.sessions[session_id] = updated
        try:
            self._save_workspace_state(session.workspace_id)
        except Exception:
            self.sessions[session_id] = session
            raise
