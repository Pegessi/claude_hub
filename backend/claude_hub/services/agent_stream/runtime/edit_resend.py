"""Edit resend responsibilities for structured streams."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ....models import AgentType, ManagedSession
from ..attachments import AgentStreamAttachmentStore
from ..providers.spec import get_spec
from ..transcript_fork import TranscriptForkError
from .manager_state import ManagerState
from .values import EditResendTurnInFlightError, StructuredSourceUnavailable

if TYPE_CHECKING:
    from ..tailer import SessionTailer

logger = logging.getLogger("claude_hub.services.agent_stream.tailer")

# Turns whose provider text is authored by the Hub, not typed by the user.
# Editing one is refused: the trusted wrapper (Goal protocol, workspace
# dispatch, restart continuation, question answer, scheduled task) is part of
# the provider contract, and resending it as an ordinary user message would
# drop that wrapper and rewrite server-owned history.  A missing origin means
# a legacy web turn and stays editable, as do the user-authored origins
# (``web``, ``feishu``, ``hub_message``).
HUB_AUTHORED_TURN_ORIGINS = frozenset(
    {"goal", "workspace", "restart_continuation", "question_answer", "scheduled_task"}
)
_HUB_AUTHORED_EDIT_REFUSAL = (
    "This turn was written by the Goal/Hub, not by you. Only your own messages can be edited."
)


class EditResendMixin(ManagerState):
    """Edit resend behavior on the sole manager owner."""

    async def edit_resend(
        self,
        session: ManagedSession,
        text: str,
        client_turn_id: str,
        turn_id: str,
        *,
        turn_metadata: Optional[Dict[str, Any]] = None,
        added_images: Optional[List[bytes]] = None,
        added_previews: Optional[List[Optional[bytes]]] = None,
    ) -> int:
        """Edit a previously sent message and rerun the conversation from there.

        Truncates both planes at the turn identified by ``turn_id``:

        1. **Hub event store** — events from the turn's ``turn_started``
           onward are removed.
        2. **Provider transcript** — the JSONL file is forked so the records
           before the turn's user message are preserved.

        The native transport is then restarted (it reads the forked
        transcript and resumes via ``--resume`` / ``thread/resume``) and the
        edited ``text`` is delivered as the new turn.

        Images: the original turn's attachment previews are re-sent to the
        provider and its attachment ids are re-referenced in the new turn.
        ``added_images`` are NEW images the user attached while editing.  They
        are delivered as images past the reused prefix with their own preview
        slots, and ``send_message`` persists their previews and appends their
        metadata after the reused ones, so ``images`` (reused previews then
        added originals) always lines up one-to-one with the turn's metadata.

        Concurrency: a per-session lock (``self._lifecycle_locks``) is held across
        the entire sequence.  A concurrent ``edit_resend`` for the same
        session waits for the in-flight one to complete; it does not fail
        fast.  This prevents two edits from interleaving truncate/fork/send
        operations. Retirement also waits on this lock, including while the
        edit has no registered tailer and is restoring its snapshot.

        Failure recovery: before truncating either plane, the Hub store is
        snapshotted in SQLite and the provider file to a ``.edit-bak`` sidecar.  If any step after the
        snapshot fails (truncate, fork, restart, or send), both files are
        restored from their snapshots so the conversation is byte-identical
        to before the edit was attempted, any tailer created during the
        failed attempt is discarded, and a fresh tailer is started on the
        restored state.  The original error is then re-raised.  Previews
        persisted for ``added_images`` are owned by ``send_message`` (it deletes
        them when the turn cannot be published), so a rolled-back edit never
        leaves them behind; reused previews belong to the original turn and are
        never deleted.

        Args:
            session: The managed session.
            text: The edited message text.
            client_turn_id: Frontend-generated stable id for the new turn.
            turn_id: The ``turn_id`` of the original turn being edited.
            turn_metadata: Optional metadata for the new turn.
            added_images: Original bytes of images newly attached to the
                edited turn (appended after the original turn's images).
            added_previews: Display previews for ``added_images``; when
                supplied it must hold exactly one slot per added image and a
                ``None`` slot means "not persisted, placeholder metadata".

        Returns:
            The number of Hub turns after the edited turn that the truncation
            removed (``0`` when the edited turn was the last one).

        Raises:
            EditResendTurnInFlightError: If a turn is currently running (mapped
                to 409 by the API layer).
            ValueError: If the turn is not found in the event store, the
                added-preview count does not match, or the turn was authored
                by the Hub (Goal/workspace/...) rather than the user.
            TranscriptForkError: If the provider transcript cannot be forked
                or the turn cannot be mapped to a provider user message.
            RuntimeError: If no structured adapter exists for the session.
        """
        if not get_spec(session.agent_type).edit_resend:
            if session.agent_type == AgentType.MASO:
                raise TranscriptForkError(
                    "Editing past turns is unavailable for MASO HTTP sessions"
                )
            raise TranscriptForkError(
                f"Editing past turns is unavailable for {session.agent_type.value} sessions"
            )
        adapter = self._deps.get_adapter_for_session(session)
        if adapter is None:
            raise RuntimeError("no structured adapter for session")

        # Per-session lock: serialize concurrent edit-resend attempts.  The
        # second caller waits (no fail-fast).
        edit_lock = self._lifecycle_locks.setdefault(session.id, asyncio.Lock())
        async with edit_lock:
            if (
                not self.session_is_current(session)
                or session.id in self._deps._DELETION_FENCED_SESSION_IDS
            ):
                raise StructuredSourceUnavailable("managed session was deleted or replaced")
            return await self._edit_resend_locked(
                session,
                text,
                client_turn_id,
                turn_id,
                adapter,
                turn_metadata=turn_metadata,
                added_images=added_images,
                added_previews=added_previews,
            )

    async def _stop_tailer_for_edit(self, tailer: SessionTailer) -> None:
        """Stop ``tailer`` for an edit-resend, refusing if a turn is running.

        Race-free design: the busy check and the stop both run under the
        tailer's ``_send_lock``.  ``send_message`` holds ``_send_lock`` across
        its own busy-check and ``_begin_turn``, so acquiring it here gives a
        consistent view — either the turn is already in flight (we raise) or
        no new turn can begin until we release the lock.  This closes the
        window where a send could start a turn after the check but before the
        stop.

        Calling ``stop()`` while holding ``_send_lock`` is safe because
        ``stop()`` only re-acquires ``_send_lock`` when ``turn_in_flight=True``
        (to terminalize the active turn).  We hold the lock and just confirmed
        ``turn_in_flight=False`` (and only ``_begin_turn`` under ``_send_lock``
        can set it True), so that branch cannot run.

        Raises:
            EditResendTurnInFlightError: If the tailer's transport has a turn
                in flight.  The tailer is left running so the turn is not
                killed; the caller restores it to ``self._tailers``.
        """
        async with tailer._send_lock:
            transport = tailer.native_transport
            if transport is not None and transport.turn_in_flight:
                raise EditResendTurnInFlightError(
                    "A turn is currently running; wait for it to finish before editing"
                )
            await tailer.stop(raise_errors=True)

    async def _edit_resend_locked(
        self,
        session: ManagedSession,
        text: str,
        client_turn_id: str,
        turn_id: str,
        adapter: Any,
        *,
        turn_metadata: Optional[Dict[str, Any]] = None,
        added_images: Optional[List[bytes]] = None,
        added_previews: Optional[List[Optional[bytes]]] = None,
    ) -> int:
        """Inner edit-resend logic, executed under the per-session edit lock."""
        store = self.get_store(session.workspace_id, session.id)
        turn_info = await store.find_turn_with_metadata(turn_id)
        if turn_info is None:
            raise ValueError(f"turn {turn_id!r} not found in event store")
        stream_seq, turn_index, turn_text, attachment_metas, original_metadata = turn_info

        # Authoritative and cheap: refuse Hub-authored turns before any
        # attachment work, tailer stop, snapshot or truncation.  Their provider
        # text is not the user's message, so resending it as one would rewrite
        # server-owned history (and falsify the Goal/workspace protocol).
        if original_metadata.get("origin") in HUB_AUTHORED_TURN_ORIGINS:
            raise TranscriptForkError(_HUB_AUTHORED_EDIT_REFUSAL)

        recorded_digest = original_metadata.get("provider_text_sha256")
        digest_arg = (
            recorded_digest if isinstance(recorded_digest, str) and recorded_digest else None
        )

        added = list(added_images or [])
        if added_previews is not None and len(added_previews) != len(added):
            raise ValueError(
                f"preview count ({len(added_previews)}) must match added image count "
                f"({len(added)})"
            )
        added_preview_slots: List[Optional[bytes]] = (
            list(added_previews) if added_previews is not None else [None] * len(added)
        )

        # How many Hub turns the truncation will discard.  Computed from the
        # store before truncating so the API can report it to the composer.
        total_turns = await store.count_turns()
        removed_turn_count = max(0, total_turns - (turn_index + 1))

        # Read the original turn's attachment preview bytes BEFORE truncating
        # so they can be re-sent to the provider.  The durable cache stores
        # the uploaded display image (the original when it fit the cache cap),
        # so the preview is what we re-send; the attachment IDs are re-referenced
        # in the new turn (via ``reuse_attachments`` below) so the same
        # thumbnails render.  A preview that was evicted/expired is skipped:
        # neither its id nor its bytes can be re-sent, so it is dropped from the
        # new turn to keep ``images`` aligned one-to-one with the turn's
        # attachment metadata (the id-based alignment the provider/cache
        # contract requires).
        att_store = AgentStreamAttachmentStore(session.workspace_id, session.id)
        images: List[bytes] = []
        turn_metas: List[Dict[str, Any]] = []
        for meta in attachment_metas:
            att_id = meta.get("id") if isinstance(meta, dict) else None
            if not isinstance(att_id, str) or not att_id:
                continue
            try:
                data, _mime = await att_store.read(att_id)
            except KeyError:
                continue
            images.append(data)
            turn_metas.append(dict(meta))

        # The NEW images are handed to ``send_message`` as extra images past the
        # reused prefix: that one implementation owns persisting their previews,
        # building their metadata and cleaning them up if the turn cannot be
        # published.  Reusing it keeps a single writer for the merge rule, so
        # the resent turn's image order and its metadata cannot drift apart.
        all_images = images + added

        # Keep a failed/busy/timed-out owner registered; the lifecycle lock
        # reserves detached edit work until rollback and snapshot cleanup finish.
        async with self._lock:
            previous = self._tailers.get(session.id)
        if previous is not None:
            await self._stop_tailer_for_edit(previous)
            async with self._lock:
                if self._tailers.get(session.id) is previous:
                    self._tailers.pop(session.id)

        # Use a fresh store instance now that the tailer is gone, so the
        # snapshot/truncate/restore operate on a store without a live writer.
        store = self.get_store(session.workspace_id, session.id)

        # Locate the transcript path (cached discovery is fine; we invalidate
        # before restarting the tailer).
        transcript_path = self._deps.discover_source_cached(adapter, session)

        # Snapshot both files BEFORE truncating so a failure can be undone.
        store_backup = await store.snapshot()
        transcript_backup = self._deps.snapshot_transcript(transcript_path)
        retain_backups = False

        try:
            # Truncate the Hub event store.
            await store.truncate_before(stream_seq)

            # Fork the provider transcript.  This matches the edited turn to
            # a provider user message by its recorded digest (falling back
            # to content for historical turns) and raises if it cannot.
            self._deps.fork_transcript(
                session,
                adapter,
                turn_index,
                self._deps.provider_text_for_stored_turn(turn_text, original_metadata),
                provider_text_sha256=digest_arg,
            )

            # Restart the tailer with a fresh transport that reads the forked
            # transcript, then deliver the edited text (with the original
            # turn's image attachments re-sent and re-referenced, plus any
            # image added during this edit).
            self._deps.invalidate_source(session.id)
            tailer = await self._get_or_create(session)
            send_kwargs: Dict[str, Any] = {}
            if turn_metas:
                # Reused metadata for the leading images (their preview slots
                # are ignored: those ids already exist). Only an added image
                # needs a preview slot, so an edit that adds none keeps the
                # plain reuse call shape.
                send_kwargs["reuse_attachments"] = turn_metas
                if added:
                    send_kwargs["previews"] = [None] * len(turn_metas) + added_preview_slots
            elif added:
                send_kwargs["previews"] = added_preview_slots
            if added:
                # Only this edit makes the tailer persist new previews, and this
                # caller rolls the turn back on failure (store restore below),
                # so those previews must not outlive it.
                send_kwargs["retain_previews_on_delivery_failure"] = False
            if turn_metadata:
                send_kwargs["turn_metadata"] = dict(turn_metadata)
            await tailer.send_message(text, all_images, client_turn_id, **send_kwargs)
        except BaseException:
            if not self.session_is_current(session):
                retain_backups = True
                raise StructuredSourceUnavailable("edit owner was deleted or replaced") from None
            # A replacement created by the edit must stop safely before rollback
            # can write its files. Keep both runtime and backups on failure.
            async with self._lock:
                created = self._tailers.get(session.id)
            if created is not None:
                try:
                    await created.stop(raise_errors=True)
                except BaseException:
                    retain_backups = True
                    raise
                async with self._lock:
                    if self._tailers.get(session.id) is created:
                        self._tailers.pop(session.id)

            if not self.session_is_current(session):
                retain_backups = True
                raise StructuredSourceUnavailable("edit owner was deleted or replaced") from None
            # Restore both files from their snapshots so the conversation is
            # byte-identical to before the edit.
            try:
                await store.restore(store_backup)
            except BaseException as exc:
                retain_backups = True
                if not isinstance(exc, Exception):
                    raise
                logger.exception(
                    "edit_resend: failed to restore event store for session %s",
                    session.id,
                )
            try:
                self._deps.restore_transcript(transcript_path, transcript_backup)
            except Exception:
                retain_backups = True
                logger.exception(
                    "edit_resend: failed to restore transcript for session %s",
                    session.id,
                )

            # Best-effort: start a fresh tailer on the restored state so the
            # session is immediately usable again.  Never masks the original
            # error.
            try:
                self._deps.invalidate_source(session.id)
                await self._get_or_create(session)
            except Exception:
                logger.exception(
                    "edit_resend: failed to restart tailer after restore for session %s",
                    session.id,
                )
            raise
        finally:
            if not retain_backups:
                await store.discard_snapshot(store_backup)
                self._deps.discard_snapshot(transcript_backup)

        return removed_turn_count
