"""Manager responsibilities for structured streams."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Set

from ....models import AgentStreamEvent, AgentType, ManagedSession, SessionKind
from ..base import NativeInputNotSubmittedError
from ..native import ProviderSession
from ..store import AgentStreamStore
from .edit_resend import EditResendMixin
from .values import ExternalTurnRetirement, StructuredSourceUnavailable

if TYPE_CHECKING:
    from ..tailer import SessionTailer

logger = logging.getLogger("claude_hub.services.agent_stream.tailer")


class TailerManager(EditResendMixin):
    """Manager behavior on the sole manager owner."""

    @asynccontextmanager
    async def _retirement_guard(self, session_id: str) -> AsyncIterator[None]:
        """Wait for edit/rollback ownership without holding the manager/tab lock."""
        lock = self._lifecycle_locks.setdefault(session_id, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), timeout=self._deps._STOP_JOIN_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise StructuredSourceUnavailable(
                "structured runtime mutation is still pending; retry retirement"
            ) from None
        try:
            yield
        finally:
            lock.release()

    def session_is_current(self, session: ManagedSession) -> bool:
        """Whether ``session`` is still the manager's exact durable owner."""
        live = self._session_getter(session.id)
        return bool(
            live is not None
            and live.workspace_id == session.workspace_id
            and live.tab_id == session.tab_id
            and live.created_at == session.created_at
        )

    def quiesce(self) -> None:
        """Fence new dispatches before shutdown observers can schedule more work."""
        self._accepting_sends = False

    async def _get_or_create(
        self, session: ManagedSession, *, required_conversation_id: Optional[str] = None
    ) -> SessionTailer:
        existing: Optional[SessionTailer] = None
        async with self._lock:
            if not self.session_is_current(session):
                raise StructuredSourceUnavailable("managed session was deleted or replaced")
            if session.id in self._deps._DELETION_FENCED_SESSION_IDS:
                raise StructuredSourceUnavailable("structured runtime is fenced for deletion")
            if not self._accepting_sends:
                raise NativeInputNotSubmittedError(
                    "service is shutting down; try again after it reconnects"
                )
            tailer = self._tailers.get(session.id)
            if tailer is not None:
                if not tailer.matches_session(session):
                    raise StructuredSourceUnavailable(
                        "structured runtime belongs to a replaced session incarnation"
                    )
                existing = tailer
            else:
                from ..registry import get_adapter_for_session

                adapter = get_adapter_for_session(session)
                if adapter is None:
                    raise ValueError(f"no structured adapter for agent_type={session.agent_type}")

                # Chat sessions use the native provider transport as their
                # real-time source. Terminal sessions keep the transcript-file
                # tailer.
                #
                # Fail-closed: if the native transport cannot be created for an
                # agent session, surface the error and do NOT silently fall
                # back to the transcript. The only exception is Cursor's
                # explicit ``terminal_transcript`` compatibility mode.
                native_transport: Optional[ProviderSession] = None
                native_error: Optional[str] = None
                is_chat = session.session_kind == SessionKind.CHAT
                cursor_transcript_fallback = (
                    session.agent_type == AgentType.CURSOR
                    and session.cursor_transport == "terminal_transcript"
                )

                if is_chat and not cursor_transcript_fallback:
                    # A freshly forked Chat tab carries a pending one-shot
                    # history seed in a sidecar next to its event store. Load
                    # it so the new provider's first turn actually contains the
                    # forked Q/A context (the fork copies UI history but starts
                    # a zero-history provider conversation). The sidecar is
                    # deleted once the seed is prepended to an accepted turn.
                    seed_body = self._deps.read_seed_sidecar(session.workspace_id, session.id)

                    async def _drop_seed_sidecar(
                        workspace_id: str = session.workspace_id,
                        stream_session_id: str = session.id,
                    ) -> None:
                        self._deps.discard_seed_sidecar(workspace_id, stream_session_id)

                    try:
                        native_transport = self._deps.create_native_session(
                            session,
                            conversation_id_persist=lambda cid: self._persist_session_id(
                                session.id, cid
                            ),
                            seed_history=seed_body,
                            on_seed_consumed=_drop_seed_sidecar,
                        )
                    except ValueError as exc:
                        native_error = (
                            f"native transport unavailable for {session.agent_type.value}: {exc}"
                        )

                tailer = self._tailer_factory(
                    workspace_id=session.workspace_id,
                    session_id=session.id,
                    adapter=adapter,
                    session_getter=lambda: self._session_getter(session.id),
                    native_transport=native_transport,
                    native_error=native_error,
                    post_persist_observers=self._post_persist_observers,
                    task_activity_observer=self._task_activity_observer,
                    send_admission=lambda: self._accepting_sends,
                )
                self._tailers[session.id] = tailer
            if required_conversation_id is not None:
                transport = tailer.native_transport
                if transport is None:
                    raise RuntimeError("native provider is unavailable")
                transport.require_resumed_conversation(required_conversation_id)
        if existing is not None:
            if existing.hard_failed:
                await existing.poll_once()
                if existing.hard_failed:
                    raise StructuredSourceUnavailable(
                        existing.last_error or "structured transcript source unavailable"
                    )
            if not existing.is_running():
                await existing.start()
            return existing
        try:
            await tailer.poll_once()
        except Exception:
            logger.exception("agent_stream: first-touch backfill failed for session %s", session.id)
        await tailer.start()
        return tailer

    async def subscribe(self, session: ManagedSession) -> "asyncio.Queue[AgentStreamEvent]":
        tailer = await self._get_or_create(session)
        return await tailer.subscribe()

    async def subscribe_completion(
        self, session: ManagedSession
    ) -> "asyncio.Queue[AgentStreamEvent]":
        tailer = await self._get_or_create(session)
        return await tailer.subscribe_completion()

    async def send_message(
        self,
        session: ManagedSession,
        text: str,
        images: List[bytes],
        client_turn_id: str,
        *,
        previews: Optional[List[Optional[bytes]]] = None,
        delivery: str = "normal",
        visible_text: Optional[str] = None,
        turn_metadata: Optional[Dict[str, Any]] = None,
        before_send: Optional[Callable[[], Awaitable[None]]] = None,
        before_delivery: Optional[Callable[[], None]] = None,
    ) -> None:
        """Atomically deliver a user turn (text + images) for ``session``.

        The composer's single input path for CHAT sessions. Delegates to the
        tailer's native transport so the same ``ProviderSession`` owns both
        the stream and the turn. ``client_turn_id`` is the frontend-generated
        stable turn id; the tailer publishes an authoritative ``turn_started``
        with it before the provider runs.

        ``images`` are the original bytes sent to the provider; ``previews``
        are the bounded display previews persisted to the attachment cache.
        """
        tailer = await self._get_or_create(session)
        await tailer.send_message(
            text,
            images,
            client_turn_id,
            previews=previews,
            delivery=delivery,
            visible_text=visible_text,
            turn_metadata=turn_metadata,
            before_send=before_send,
            before_delivery=before_delivery,
        )

    async def cancel_turn(
        self, session: ManagedSession, expected_turn_id: Optional[str] = None
    ) -> bool:
        """Cancel the active native turn for ``session``, if any."""
        tailer = await self._get_or_create(session)
        return await tailer.cancel_turn(expected_turn_id)

    async def retire_external_turn(
        self, session: ManagedSession, expected_turn_id: str
    ) -> ExternalTurnRetirement:
        """Retire an external-channel turn with retry-safe classification."""
        tailer = await self._get_or_create(session)
        return await tailer.retire_external_turn(expected_turn_id)

    async def active_turn_id(self, session: ManagedSession) -> Optional[str]:
        """Observe the attached provider-owned turn without creating an owner."""
        if not self.session_is_current(session):
            return None
        tailer = self.get_tailer(session.id)
        if tailer is None or not tailer.matches_session(session):
            return None
        transport = tailer._native_transport
        if transport is None or not transport.turn_in_flight:
            return None
        return tailer._active_turn_id

    async def answer_pending_question(
        self,
        session: ManagedSession,
        text: str,
        expected_turn_id: str,
        *,
        expected_call_id: Optional[str] = None,
    ) -> bool:
        tailer = self.get_tailer(session.id) or await self._get_or_create(session)
        return await tailer.answer_pending_question(
            text,
            expected_turn_id,
            expected_call_id=expected_call_id,
        )

    async def turn_in_flight(self, session: ManagedSession) -> bool:
        """Return the authoritative native transport turn guard."""
        tailer = await self._get_or_create(session)
        transport = tailer._native_transport
        return bool(transport is not None and transport.turn_in_flight)

    async def accepts_question_followup(
        self,
        session: ManagedSession,
        text: str,
        expected_turn_id: str,
        *,
        expected_call_id: Optional[str] = None,
    ) -> bool:
        tailer = self.get_tailer(session.id) or await self._get_or_create(session)
        return tailer.accepts_question_followup(
            text,
            expected_turn_id,
            expected_call_id=expected_call_id,
        )

    async def resolve_answered_questions(
        self,
        session: ManagedSession,
        text: str,
        *,
        expected_turn_id: Optional[str] = None,
        expected_call_id: Optional[str] = None,
    ) -> None:
        tailer = self.get_tailer(session.id) or await self._get_or_create(session)
        await tailer.resolve_answered_questions(
            text,
            expected_turn_id=expected_turn_id,
            expected_call_id=expected_call_id,
        )

    async def set_mode(self, session: ManagedSession, mode: str) -> None:
        """Set the existing native owner's mode for subsequent turns."""

        tailer = await self._get_or_create(session)
        transport = tailer.native_transport
        if transport is None or tailer.native_error is not None:
            raise RuntimeError(tailer.native_error or "native provider transport unavailable")
        await transport.set_mode(mode)
        if not self.session_is_current(session) or not tailer.matches_session(session):
            raise StructuredSourceUnavailable("managed session was deleted or replaced")
        if self._persist_mode_cb is not None:
            self._persist_mode_cb(session.id, mode)

    def set_env(self, session: ManagedSession, env: Dict[str, str]) -> None:
        """Update the native owner's session env for subsequent turns."""

        tailer = self._tailers.get(session.id)
        if (
            tailer is None
            or not self.session_is_current(session)
            or not tailer.matches_session(session)
        ):
            return
        transport = tailer.native_transport
        if transport is None or tailer.native_error is not None:
            return
        transport.update_env(env)
        transport.session.solo_mode = session.solo_mode

    async def ensure_started(
        self, session: ManagedSession, *, required_conversation_id: Optional[str] = None
    ) -> SessionTailer:
        return await self._get_or_create(session, required_conversation_id=required_conversation_id)

    @staticmethod
    def _is_reusable(tailer: SessionTailer) -> bool:
        """True if ``tailer`` is healthy and actively running a turn.

        A manual Retry after a transient reconnect must not stop such a
        tailer: stopping it would cancel the in-flight turn and emit a false
        runtime-interruption. Only a hard-failed, stopped, or idle tailer is
        replaced (AC2).
        """
        if tailer._retirement_pending or tailer.hard_failed or not tailer.is_running():
            return False
        transport = tailer.native_transport
        if transport is None or not transport.turn_in_flight:
            return False
        return True

    async def retry(self, session: ManagedSession) -> SessionTailer:
        """Replace a failed/stopped tailer and resume from durable provider id.

        A hard-failed native tailer must not be revived in place: its provider
        queues and reader tasks belong to the failed process generation.  Drop
        the whole in-memory owner, stop any surviving subprocess, then let the
        normal constructor create a fresh adapter/transport.  The persisted
        ``agent_session_id`` on ``session`` preserves the conversation.

        A healthy tailer that is actively running a turn is preserved instead
        of replaced: a manual Retry after a transient network failure must not
        cancel an in-flight turn (AC2). Re-register it under the lock and
        return it; the durable conversation id is unchanged.
        """

        async with self._retirement_guard(session.id):
            async with self._lock:
                if (
                    not self.session_is_current(session)
                    or session.id in self._deps._DELETION_FENCED_SESSION_IDS
                ):
                    raise StructuredSourceUnavailable("managed session was deleted or replaced")
                previous = self._tailers.get(session.id)
                if previous is not None and not previous.matches_session(session):
                    raise StructuredSourceUnavailable(
                        "structured runtime belongs to a replaced session incarnation"
                    )
                if previous is not None and self._is_reusable(previous):
                    self._deps._HARD_FAILED_SESSION_IDS.discard(session.id)
                    return previous
            if previous is not None:
                await previous.stop(raise_errors=True)
                async with self._lock:
                    if self._tailers.get(session.id) is previous:
                        self._tailers.pop(session.id)
            self._deps._HARD_FAILED_SESSION_IDS.discard(session.id)
            self._deps.invalidate_source(session.id)
            return await self._get_or_create(session)

    def get_tailer(self, session_id: str) -> Optional[SessionTailer]:
        return self._tailers.get(session_id)

    def tailers(self) -> List[SessionTailer]:
        """Return a stable snapshot for read-only runtime inspection."""

        return list(self._tailers.values())

    def unsubscribe(self, session_id: str, queue: "asyncio.Queue[AgentStreamEvent]") -> None:
        tailer = self._tailers.get(session_id)
        if tailer is not None:
            tailer.unsubscribe(queue)

    def unsubscribe_completion(
        self, session_id: str, queue: "asyncio.Queue[AgentStreamEvent]"
    ) -> None:
        tailer = self._tailers.get(session_id)
        if tailer is not None:
            tailer.unsubscribe_completion(queue)

    def hard_failed(self, session_id: str) -> bool:
        tailer = self._tailers.get(session_id)
        return tailer is not None and tailer.hard_failed

    def is_structured_available(self, session: ManagedSession) -> bool:
        from ..registry import get_adapter_for_session

        if get_adapter_for_session(session) is None:
            return False
        tailer = self._tailers.get(session.id)
        return tailer is None or not tailer.hard_failed

    def get_store(self, workspace_id: str, session_id: str) -> AgentStreamStore:
        tailer = self._tailers.get(session_id)
        if tailer is not None and tailer.workspace_id == workspace_id:
            return tailer.store
        return AgentStreamStore(workspace_id, session_id)

    def _persist_session_id(self, session_id: str, conversation_id: str) -> None:
        """Persist the provider conversation id back to the managed session.

        This lets a cold restart resume the same provider conversation via
        ``--resume`` (Claude/Cursor) or ``thread/resume`` (Codex).

        This is only invoked after the provider has emitted the conversation id
        in a system/init record, so ``agent_session_id_verified`` is set to
        True alongside the id. A cold restart seeds ``_conversation_id_verified``
        from this flag, never from the mere presence of a UUID.

        If a durable persistence callback was supplied at construction, it is
        invoked so the id is written to disk (ttyd tab state for direct tabs,
        workspace session state for workspace sessions). Otherwise the id is
        set on the in-memory ManagedSession only (test mode).
        """
        session = self._session_getter(session_id)
        if session is None:
            return
        try:
            session.agent_session_id = conversation_id
            session.agent_session_id_verified = True
        except Exception:
            logger.exception(
                "failed to set conversation id %s on session %s",
                conversation_id,
                session_id,
            )
            return
        if self._persist_session_id_cb is not None:
            try:
                self._persist_session_id_cb(session_id, conversation_id)
            except Exception:
                logger.exception(
                    "failed to durably persist conversation id %s for session %s",
                    conversation_id,
                    session_id,
                )

    async def fence_session(self, session_id: str) -> bool:
        """Strictly stop and detach an owner without releasing its delete fence."""
        return await self.forget_session(session_id)

    async def forget_session(self, session_id: str) -> bool:
        """Detach an owner only after its runtime has stopped safely.

        Every removal path, including archive, preserves the owner on failure.
        Returns whether this manager owned a runtime that was stopped.
        """
        async with self._retirement_guard(session_id):
            async with self._lock:
                tailer = self._tailers.get(session_id)
            if tailer is not None:
                await tailer.stop(raise_errors=True)
                async with self._lock:
                    if self._tailers.get(session_id) is tailer:
                        self._tailers.pop(session_id)
            self._deps._HARD_FAILED_SESSION_IDS.discard(session_id)
            self._deps.invalidate_source(session_id)
            return tailer is not None

    async def stop_all(self) -> None:
        async with self._lock:
            session_ids = set(self._tailers) | {
                session_id for session_id, lock in self._lifecycle_locks.items() if lock.locked()
            }
        # Do not hold the manager lock while waiting for edits or observers.
        # Strict retirement keeps a failed provider (or detached rollback) owned.
        results = await asyncio.gather(
            *(self.forget_session(session_id) for session_id in session_ids),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
