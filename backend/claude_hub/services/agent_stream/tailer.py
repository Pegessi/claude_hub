"""Per-session transcript tailer for the structured observation plane.

One :class:`SessionTailer` per session, owned by a process-wide
:class:`TailerManager`. The tailer polls the agent's transcript file (found by
the provider adapter), normalizes each new line through the adapter, redacts +
persists the resulting events to the per-session store, and fans them out to
any live SSE/wait subscribers.

Design points:

- **One tailer per session** — the manager dedupes; subscribers share one loop.
- **Close the file every poll** — ``_read_new_lines`` opens, reads, and closes
  within a worker thread, so no fd is held open between polls.
- **Persisted cursor** — ``{session}.cursor.json`` records ``(path, inode,
  offset, run_epoch)`` plus snapshot identities when necessary, so a tailer
  restarted after idle (or after a backend restart) resumes without
  re-processing or duplicating already-persisted events. Written atomically
  (tmp + rename).
- **run_epoch** — incremented for each ``turn_started`` and stamped on every
  event of that turn, so the frontend can group a prompt's full response.
- **Backfill vs live** — the first read from offset 0 is a backfill: events are
  persisted but NOT fanned out (live subscribers use history for replay; SSE
  is for live only).
- **Fail-closed** — if the source cannot be discovered within a grace period,
  the tailer marks itself ``hard_failed``; the capabilities endpoint then
  reports ``structured=False`` and the frontend falls back to the raw terminal.
- **Idle reaping** — a tailer with zero subscribers stops after ``IDLE_TTL_S``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
import time
import uuid
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from functools import partial
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
)

import claude_hub.services.agent_stream.runtime.goal_protocol as _goal_protocol

from ...models import (
    AgentRuntimeStatus,
    AgentStreamEvent,
    AgentStreamEventType,
    AgentType,
    ManagedSession,
    SessionKind,
)
from .attachments import AgentStreamAttachmentStore, _sync_existing_directory
from .base import (
    AgentStreamAdapter,
    NativeInputNotSubmittedError,
    NormalizeContext,
    discover_source_cached,
    invalidate_source,
)
from .coalescer import AgentStreamCoalescer
from .fork_seed import discard_seed_sidecar, read_seed_sidecar
from .local_images import ImageReferenceBuffer, cache_image_paths
from .native import (
    ProviderSession,
    _detect_image_mime,
    create_native_session,
    parse_ask_question_response,
)
from .redaction import redact_event
from .registry import get_adapter_for_session
from .runtime.dependencies import ImageCacheWriter, NativeSessionFactory, RuntimeDependencies

# Keep the legacy imports, including the sanitizer factory patched by callers.
from .runtime.goal_protocol import _CHECKPOINT_OPENER as _CHECKPOINT_OPENER
from .runtime.goal_protocol import _GOAL_STATUS_TERMINAL_RE as _GOAL_STATUS_TERMINAL_RE
from .runtime.goal_protocol import _GOAL_TAIL_SCAN_CHARS as _GOAL_TAIL_SCAN_CHARS
from .runtime.goal_protocol import _MAX_STATUS_BODY_CHARS as _MAX_STATUS_BODY_CHARS
from .runtime.goal_protocol import _STRICT_STATUS_OPENER_RE as _STRICT_STATUS_OPENER_RE
from .runtime.manager import TailerManager as _RuntimeTailerManager
from .runtime.session import SessionTailer as _RuntimeSessionTailer
from .runtime.values import EditResendTurnInFlightError as EditResendTurnInFlightError
from .runtime.values import ExternalTurnRetirement as ExternalTurnRetirement
from .runtime.values import NativeRuntimeSnapshot as NativeRuntimeSnapshot
from .runtime.values import PostPersistObserver as PostPersistObserver
from .runtime.values import StructuredSourceUnavailable as StructuredSourceUnavailable
from .runtime.values import _PendingApproval as _PendingApproval
from .store import AgentStreamStore, TurnLifecycleState, _complete_io
from .transcript_fork import (
    TranscriptForkError,
    discard_snapshot,
    fork_transcript,
    restore_transcript,
    snapshot_transcript,
)
from .turn_source import provider_text_for_stored_turn

logger = logging.getLogger(__name__)


POLL_INTERVAL_S = 1.0
IDLE_TTL_S = 300.0
# Stream inactivity timeout. The model API is streaming while the agent is
# reasoning or generating text, so a healthy model phase emits events
# continuously. If no provider record arrives for this long and the turn is
# neither running a tool nor waiting for an approval answer, the stream is
# dead (e.g. the model backend stopped responding). There is deliberately no
# fixed cap on total turn duration: a legitimate review or test run can stream
# actively for much longer than an hour.
STREAM_INACTIVITY_TIMEOUT_S = 600.0
# Outer liveness bound for an active turn that has produced NO provider record
# of any kind. Unlike ``STREAM_INACTIVITY_TIMEOUT_S`` this is not suppressed by
# an outstanding tool call: a wedged persistent app-server (TraeX/Codex) or a
# subprocess killed outside Hub can leave a tool call open *forever*, which the
# streaming watchdog alone would never converge. It is deliberately far longer
# than the streaming timeout and is refreshed by EVERY accepted record — tool
# lifecycle, capacity-queue notifications, and control-plane notifications
# included — so genuinely long work that keeps emitting something is never
# reaped. Only total provider silence (typically a frozen process past any real
# tool's horizon) trips it. An open blocking approval card suppresses it, since
# that silence is a human wait, not a dead provider. Tunable for environments
# with longer legitimate silent commands.
ACTIVE_TURN_HARD_LIVENESS_TIMEOUT_S = float(
    os.environ.get("CLAUDE_HUB_ACTIVE_TURN_LIVENESS_TIMEOUT_S", "7200")
)
# Minimum silence before either silence watchdog (inactivity / hard liveness)
# may reap a turn, regardless of a smaller configured timeout (the zeroed
# timeouts used in tests are the only case that binds: real configs are 600s+).
# Each watchdog evaluates at the TOP of a poll tick, BEFORE that tick's next
# record is read. On a freshly started turn the provider's first records can
# already be buffered in transport stdout without having been drained yet, so
# the watchdog briefly observes a brand-new activity stamp (age ≈ 0) and an
# empty active-tool set. With a zero/near-zero timeout that microsecond-old turn
# was killed before its first ``item/started`` registered — the very tool that
# would have suppressed the inactivity reap — discarding the buffered
# ``item/completed`` and losing the stop-restart turn lifecycle. This debounce
# spans several poll ticks, long enough to drain the handoff, far below any real
# timeout.
_MIN_TURN_SILENCE_S = 0.25
# Provider capacity queue (TraeX/Codex ``queue/status``): the turn has been
# accepted but model generation has NOT started, so a queue heartbeat is not
# evidence that the model stream is alive. Two distinct bounds apply while a
# turn is in this waiting state:
#
# * ``QUEUED_TURN_MAX_WAIT_S`` — a LIVE queue that keeps heartbeating but never
#   reaches generation past this bound is terminalized (cancelled, not
#   restarted: the wait is server-side capacity; resending re-queues). This is
#   the escape hatch for "queue position 357 at ~1/minute", where the provider
#   is healthy but the user would otherwise be locked out for hours. Stop is
#   always available regardless. 0 disables the cap (wait indefinitely).
# * ``QUEUE_HEARTBEAT_STALL_S`` — a turn the provider DECLARED as queued but
#   whose queue heartbeats then fall silent past this bound is treated as a
#   wedged/dead runtime and reaped via the hard-liveness path (kill + relaunch
#   + resume). A healthy queue emits roughly one status per second.
QUEUED_TURN_MAX_WAIT_S = float(os.environ.get("CLAUDE_HUB_QUEUED_TURN_MAX_WAIT_S", "1800"))
QUEUE_HEARTBEAT_STALL_S = float(os.environ.get("CLAUDE_HUB_QUEUE_HEARTBEAT_STALL_S", "90"))
# Grace between a Goal turn's trailing terminal signal and the provider's
# completion record. One-shot CLIs (Claude/Cursor) can have their stdout held
# open by a lingering child after the final assistant text, so the result
# record sometimes never arrives even though the model's output is complete.
# Once the Goal envelope has fully arrived, wait this long for the provider
# record before synthesizing a completed turn rather than reaping the turn as
# silent 10 minutes later.
GOAL_TERMINAL_GRACE_S = 60.0
DISCOVERY_GRACE_S = 30.0
SUBSCRIBER_QUEUE_MAX = 2000
COMPLETION_QUEUE_MAX = 8
_STOP_JOIN_TIMEOUT_S = 5.0
_RUNTIME_INTERRUPTED_MESSAGE = (
    "Turn interrupted because its backend runtime was no longer available."
)
# A turn went silent while the model itself was expected to stream. A running
# tool or an open approval card suppresses this timeout because both are
# legitimate external waits that can produce no model events for a long time.
_INACTIVITY_TIMEOUT_MESSAGE = "Turn stopped after the stream went silent."
# A turn produced NO provider record at all past the outer liveness bound,
# including while a tool was nominally outstanding: the provider process is
# wedged or gone without an EOF.
_HARD_LIVENESS_TIMEOUT_MESSAGE = "Turn stopped after the provider stopped producing any output."
# A live provider capacity queue never advanced to generation within the cap.
# The runtime is healthy (it keeps reporting its position); the request simply
# did not get capacity. Cancel rather than restart so resending re-queues.
_QUEUE_CAP_TIMEOUT_MESSAGE = (
    "Turn stopped after waiting too long for model capacity. "
    "Resend your message to rejoin the queue."
)
# A turn was declared queued but the queue heartbeats then stopped: the runtime
# went away mid-wait, so it is reaped and resumed like any other dead turn.
_QUEUE_STALL_MESSAGE = "Turn stopped while queued after the provider stopped responding."
_TERMINAL_PERSISTENCE_FAILURE_MESSAGE = (
    "Chat turn completed but its final state could not be saved."
)
# Turn-interior event types that only have meaning attributed to a live Hub
# turn. A record of one of these arriving with no owning turn (``turn_id`` is
# null AND no turn is active) is an unreferenceable replay/background record —
# e.g. TraeX re-emitting a nested code-mode ``item/completed`` long after the
# turn it belonged to terminalized. Persisting it would mint an orphan
# never-completed turn and pin the composer lock forever. Explicit boundaries
# (``turn_started``), control-plane notifications (``status``, such as
# ``thread/goal/updated`` with ``turnId=null``), and session-level ``error``
# records are intentionally allowed without a turn.
_UNATTRIBUTED_DROP_TYPES = frozenset(
    {
        AgentStreamEventType.TEXT_DELTA,
        AgentStreamEventType.THINKING_DELTA,
        AgentStreamEventType.TOOL_CALL_STARTED,
        AgentStreamEventType.TOOL_CALL_COMPLETED,
        AgentStreamEventType.APPROVAL_REQUIRED,
        AgentStreamEventType.APPROVAL_RESOLVED,
        AgentStreamEventType.TURN_COMPLETED,
    }
)
# Provider records proving the active turn has left the capacity queue and is
# genuinely executing (model tokens or a tool call). Any one of these clears
# the contiguous queue wait tracked for the liveness watchdog.
_GENERATION_ACTIVITY_TYPES = frozenset(
    {
        AgentStreamEventType.TEXT_DELTA,
        AgentStreamEventType.THINKING_DELTA,
        AgentStreamEventType.TOOL_CALL_STARTED,
        AgentStreamEventType.TOOL_CALL_COMPLETED,
    }
)

_HARD_FAILED_SESSION_IDS: Set[str] = set()
# A strict deletion fence is process-wide: all managers must reject a new
# owner between provider teardown and durable identity removal. It also records
# a successful fence across a failed state commit so deletion can be retried
# without requiring the already-stopped runtime to be recreated.
_DELETION_FENCED_SESSION_IDS: Set[str] = set()
_PROVIDER_FENCED_SESSION_IDS: Set[str] = set()
# The retiring owner alone may append its final cancelled/error lifecycle
# edges while the process-wide deletion fence blocks every new owner.
_RETIRING_TAILER_IDS: Set[int] = set()
_TAILER_MANAGERS: weakref.WeakSet[TailerManager] = weakref.WeakSet()


def _trailing_envelope_state(text: str) -> str:
    """Resolve legacy module overrides at call time, including for active streams."""
    return _goal_protocol._trailing_envelope_state(
        text,
        tail_scan_chars=_GOAL_TAIL_SCAN_CHARS,
        checkpoint_opener=_CHECKPOINT_OPENER,
        status_opener_re=_STRICT_STATUS_OPENER_RE,
        max_status_body_chars=_MAX_STATUS_BODY_CHARS,
    )


class _GoalProtocolSanitizer(_goal_protocol._GoalProtocolSanitizer):
    """Keep legacy lookup paths while the helper owns the parsing implementation."""

    @staticmethod
    def _possible_opener_suffix(text: str, *, openers: Optional[Tuple[str, ...]] = None) -> int:
        return _goal_protocol._GoalProtocolSanitizer._possible_opener_suffix(
            text, openers=_GoalProtocolSanitizer._OPENERS if openers is None else openers
        )

    def _envelope_state(self, text: str) -> str:
        return _trailing_envelope_state(text)


class _LegacyRuntimeDependencies(RuntimeDependencies):
    """Read legacy module symbols at use time without mirroring their state."""

    @property
    def POLL_INTERVAL_S(self) -> float:
        return POLL_INTERVAL_S

    @property
    def IDLE_TTL_S(self) -> float:
        return IDLE_TTL_S

    @property
    def STREAM_INACTIVITY_TIMEOUT_S(self) -> float:
        return STREAM_INACTIVITY_TIMEOUT_S

    @property
    def ACTIVE_TURN_HARD_LIVENESS_TIMEOUT_S(self) -> float:
        return ACTIVE_TURN_HARD_LIVENESS_TIMEOUT_S

    @property
    def _MIN_TURN_SILENCE_S(self) -> float:
        return _MIN_TURN_SILENCE_S

    @property
    def QUEUED_TURN_MAX_WAIT_S(self) -> float:
        return QUEUED_TURN_MAX_WAIT_S

    @property
    def QUEUE_HEARTBEAT_STALL_S(self) -> float:
        return QUEUE_HEARTBEAT_STALL_S

    @property
    def GOAL_TERMINAL_GRACE_S(self) -> float:
        return GOAL_TERMINAL_GRACE_S

    @property
    def DISCOVERY_GRACE_S(self) -> float:
        return DISCOVERY_GRACE_S

    @property
    def SUBSCRIBER_QUEUE_MAX(self) -> int:
        return SUBSCRIBER_QUEUE_MAX

    @property
    def COMPLETION_QUEUE_MAX(self) -> int:
        return COMPLETION_QUEUE_MAX

    @property
    def _STOP_JOIN_TIMEOUT_S(self) -> float:
        return _STOP_JOIN_TIMEOUT_S

    @property
    def _RUNTIME_INTERRUPTED_MESSAGE(self) -> str:
        return _RUNTIME_INTERRUPTED_MESSAGE

    @property
    def _INACTIVITY_TIMEOUT_MESSAGE(self) -> str:
        return _INACTIVITY_TIMEOUT_MESSAGE

    @property
    def _HARD_LIVENESS_TIMEOUT_MESSAGE(self) -> str:
        return _HARD_LIVENESS_TIMEOUT_MESSAGE

    @property
    def _QUEUE_CAP_TIMEOUT_MESSAGE(self) -> str:
        return _QUEUE_CAP_TIMEOUT_MESSAGE

    @property
    def _QUEUE_STALL_MESSAGE(self) -> str:
        return _QUEUE_STALL_MESSAGE

    @property
    def _TERMINAL_PERSISTENCE_FAILURE_MESSAGE(self) -> str:
        return _TERMINAL_PERSISTENCE_FAILURE_MESSAGE

    @property
    def _UNATTRIBUTED_DROP_TYPES(self) -> frozenset[AgentStreamEventType]:
        return _UNATTRIBUTED_DROP_TYPES

    @property
    def _GENERATION_ACTIVITY_TYPES(self) -> frozenset[AgentStreamEventType]:
        return _GENERATION_ACTIVITY_TYPES

    @property
    def _HARD_FAILED_SESSION_IDS(self) -> Set[str]:
        return _HARD_FAILED_SESSION_IDS

    @property
    def _DELETION_FENCED_SESSION_IDS(self) -> Set[str]:
        return _DELETION_FENCED_SESSION_IDS

    @property
    def _RETIRING_TAILER_IDS(self) -> Set[int]:
        return _RETIRING_TAILER_IDS

    @property
    def _GOAL_STATUS_TERMINAL_RE(self) -> re.Pattern[str]:
        return _GOAL_STATUS_TERMINAL_RE

    @property
    def _GOAL_TAIL_SCAN_CHARS(self) -> int:
        return _GOAL_TAIL_SCAN_CHARS

    @property
    def _GoalProtocolSanitizer(self) -> type[_GoalProtocolSanitizer]:
        return _GoalProtocolSanitizer

    @property
    def create_native_session(self) -> NativeSessionFactory:
        return create_native_session

    @property
    def cache_image_paths(self) -> ImageCacheWriter:
        return cache_image_paths

    @property
    def discover_source_cached(
        self,
    ) -> Callable[[AgentStreamAdapter, ManagedSession], Optional[Path]]:
        return discover_source_cached

    @property
    def invalidate_source(self) -> Callable[[str], None]:
        return invalidate_source

    @property
    def snapshot_transcript(self) -> Callable[[Optional[Path]], Optional[Path]]:
        return snapshot_transcript

    @property
    def restore_transcript(self) -> Callable[[Optional[Path], Optional[Path]], None]:
        return restore_transcript

    @property
    def fork_transcript(self) -> Callable[[ManagedSession, Any, int, str], int]:
        return fork_transcript

    @property
    def discard_snapshot(self) -> Callable[[Optional[Path]], None]:
        return discard_snapshot

    @property
    def get_adapter_for_session(self) -> Callable[[ManagedSession], Optional[AgentStreamAdapter]]:
        return get_adapter_for_session

    @property
    def read_seed_sidecar(self) -> Callable[[str, str], Optional[str]]:
        return read_seed_sidecar

    @property
    def discard_seed_sidecar(self) -> Callable[[str, str], None]:
        return discard_seed_sidecar

    @property
    def provider_text_for_stored_turn(self) -> Callable[[str, Mapping[str, Any]], str]:
        return provider_text_for_stored_turn

    @property
    def _detect_image_mime(self) -> Callable[[bytes], Optional[str]]:
        return _detect_image_mime

    @property
    def parse_ask_question_response(self) -> Callable[[str], Optional[List[Dict[str, Any]]]]:
        return parse_ask_question_response


class SessionTailer(_RuntimeSessionTailer):
    """Legacy construction path with live dependency lookup."""

    def __init__(
        self,
        workspace_id: str,
        session_id: str,
        adapter: AgentStreamAdapter,
        session_getter: Callable[[], Optional[ManagedSession]],
        store: Optional[AgentStreamStore] = None,
        native_transport: Optional[ProviderSession] = None,
        native_error: Optional[str] = None,
        post_persist_observers: Optional[List[PostPersistObserver]] = None,
        task_activity_observer: Optional[PostPersistObserver] = None,
        send_admission: Optional[Callable[[], bool]] = None,
    ) -> None:
        super().__init__(
            dependencies=_LegacyRuntimeDependencies(),
            workspace_id=workspace_id,
            session_id=session_id,
            adapter=adapter,
            session_getter=session_getter,
            store=store,
            native_transport=native_transport,
            native_error=native_error,
            post_persist_observers=post_persist_observers,
            task_activity_observer=task_activity_observer,
            send_admission=send_admission,
        )


def _create_session_tailer(
    workspace_id: str,
    session_id: str,
    adapter: AgentStreamAdapter,
    session_getter: Callable[[], Optional[ManagedSession]],
    store: Optional[AgentStreamStore] = None,
    native_transport: Optional[ProviderSession] = None,
    native_error: Optional[str] = None,
    post_persist_observers: Optional[List[PostPersistObserver]] = None,
    task_activity_observer: Optional[PostPersistObserver] = None,
    send_admission: Optional[Callable[[], bool]] = None,
) -> SessionTailer:
    """Resolve the legacy class factory when an existing manager creates an owner."""
    return SessionTailer(
        workspace_id=workspace_id,
        session_id=session_id,
        adapter=adapter,
        session_getter=session_getter,
        store=store,
        native_transport=native_transport,
        native_error=native_error,
        post_persist_observers=post_persist_observers,
        task_activity_observer=task_activity_observer,
        send_admission=send_admission,
    )


class TailerManager(_RuntimeTailerManager):
    """Legacy construction path with live dependency lookup."""

    def __init__(
        self,
        session_getter: Callable[[str], Optional[ManagedSession]],
        persist_session_id: Optional[Callable[[str, str], None]] = None,
        persist_mode: Optional[Callable[[str, str], None]] = None,
        post_persist_observers: Optional[List[PostPersistObserver]] = None,
        task_activity_observer: Optional[PostPersistObserver] = None,
    ) -> None:
        super().__init__(
            dependencies=_LegacyRuntimeDependencies(),
            tailer_factory=_create_session_tailer,
            session_getter=session_getter,
            persist_session_id=persist_session_id,
            persist_mode=persist_mode,
            post_persist_observers=post_persist_observers,
            task_activity_observer=task_activity_observer,
        )
        _TAILER_MANAGERS.add(self)


def _native_runtime_snapshot(tailer: _RuntimeSessionTailer) -> Optional[NativeRuntimeSnapshot]:
    if tailer.hard_failed or tailer.native_error is not None:
        return NativeRuntimeSnapshot(
            status=AgentRuntimeStatus.OFFLINE,
            detail=tailer.native_error or tailer.last_error or "native provider unavailable",
        )
    transport = tailer.native_transport
    if transport is None:
        return None
    if transport.last_error is not None:
        return NativeRuntimeSnapshot(
            status=AgentRuntimeStatus.OFFLINE,
            detail=transport.last_error,
        )
    terminal_status = tailer.native_terminal_status
    if terminal_status is not None:
        return terminal_status
    if transport.turn_in_flight:
        return NativeRuntimeSnapshot(
            status=AgentRuntimeStatus.WORKING,
            detail="native provider turn is in flight",
        )
    if transport.exit_error is not None:
        return NativeRuntimeSnapshot(
            status=AgentRuntimeStatus.ATTENTION,
            detail=transport.exit_error,
        )
    return NativeRuntimeSnapshot(
        status=AgentRuntimeStatus.IDLE,
        detail="native provider is ready",
    )


def get_native_runtime_snapshot(session_id: str) -> Optional[NativeRuntimeSnapshot]:
    """Peek an existing native tailer without creating or starting one."""

    for manager in list(_TAILER_MANAGERS):
        tailer = manager.get_tailer(session_id)
        if tailer is not None:
            snapshot = _native_runtime_snapshot(tailer)
            if snapshot is not None:
                return snapshot
    return None


def get_tab_native_runtime_snapshot(tab_id: str) -> Optional[NativeRuntimeSnapshot]:
    """Peek the native owner for a direct top-level Chat tab."""

    for manager in list(_TAILER_MANAGERS):
        for tailer in manager.tailers():
            transport = tailer.native_transport
            if transport is None or transport.session.tab_id != tab_id:
                continue
            snapshot = _native_runtime_snapshot(tailer)
            if snapshot is not None:
                return snapshot
    return None


def structured_source_hard_failed(session_id: str) -> bool:
    """Return the process-local terminal discovery state for ``session_id``."""
    return session_id in _HARD_FAILED_SESSION_IDS


async def fence_session_stream(session_id: str) -> bool:
    """Strictly stop every in-process owner while retaining durable evidence.

    ``False`` means no manager owned a process-local runtime; it is not a
    provider fence. Callers must keep that distinction when validating an
    accepted but semantically unacknowledged delivery. The reservation is kept
    until destructive cleanup finishes, so a failed state commit cannot admit
    a replacement owner or forget a previously completed fence.
    """

    _DELETION_FENCED_SESSION_IDS.add(session_id)
    provider_fenced = session_id in _PROVIDER_FENCED_SESSION_IDS
    for manager in list(_TAILER_MANAGERS):
        provider_fenced = await manager.fence_session(session_id) or provider_fenced
    provider_fenced = provider_fenced or session_id in _PROVIDER_FENCED_SESSION_IDS
    if provider_fenced and session_id in _DELETION_FENCED_SESSION_IDS:
        # A concurrent deleter may have already completed discard while this
        # caller was joining the same owner. Do not resurrect its reservation.
        _PROVIDER_FENCED_SESSION_IDS.add(session_id)
    return provider_fenced


async def discard_session_stream(
    workspace_id: str,
    session_id: str,
    *,
    acknowledged_call_ids: Optional[Set[str]] = None,
    provider_fenced: Optional[bool] = None,
) -> None:
    """Purge structured state only after its runtime identity is retired.

    When ``provider_fenced`` is supplied, the caller already completed strict
    teardown before durably removing the owning session. Omitting it preserves
    the original combined fence-and-discard behavior for direct tab lifecycle
    callers.
    """

    if provider_fenced is None:
        provider_fenced = await fence_session_stream(session_id)

    validate_session_stream_cleanup(
        workspace_id,
        session_id,
        acknowledged_call_ids=acknowledged_call_ids or set(),
        provider_fenced=provider_fenced,
    )
    await discard_retired_session_artifacts(workspace_id, session_id)


def validate_session_stream_cleanup(
    workspace_id: str,
    session_id: str,
    *,
    acknowledged_call_ids: Set[str],
    provider_fenced: bool,
) -> None:
    """Authorize artifact deletion before its owning identity is retired."""
    from .. import managed_chat

    managed_chat.validate_delivery_state_discard(
        workspace_id,
        session_id,
        acknowledged_call_ids=acknowledged_call_ids,
        provider_fenced=provider_fenced,
    )


def session_stream_has_owner(session_id: str) -> bool:
    """Reject cleanup collisions without acquiring or stopping a new runtime."""
    return any(manager.get_tailer(session_id) is not None for manager in _TAILER_MANAGERS)


async def discard_retired_session_artifacts(workspace_id: str, session_id: str) -> None:
    """Idempotently collect artifacts for an already-authorized retired owner.

    Callers must retain their durable identity reservation until this finishes;
    replay does not infer permission from missing process-local fence state.
    """
    from .. import managed_chat

    store = AgentStreamStore(workspace_id, session_id)
    configured_root = store.path.parent.parent.parent
    root = configured_root.resolve()
    # This one-shot cleanup store captures the trusted root alias; owned
    # namespace children are derived lexically and still checked without follow.
    store._path = root / store.path.relative_to(configured_root)
    store._dir = store.path.parent
    stream_directory = store.path.parent
    for directory in (root, stream_directory.parent, stream_directory):
        try:
            mode = directory.lstat().st_mode
        except FileNotFoundError:
            break
        if not stat.S_ISDIR(mode):
            raise OSError("retired stream directory is not a real directory")
    await store.clear()
    await _complete_io(discard_snapshot, store.path.with_name(store.path.name + ".edit-bak"))
    for suffix in (
        ".fork-seed.json",
        ".fork-seed.json.tmp",
        ".managed-delivery.json",
        ".maso-delivery.json",
        ".maso-delivery.tmp",
    ):
        # Only fixed names derived from this captured stream identity. The
        # ordinary seed helper is best effort and cannot authorize durable ACK.
        await _complete_io(discard_snapshot, store.path.with_suffix(suffix))
    await AgentStreamAttachmentStore(workspace_id, session_id).clear(strict=True)
    managed_chat._turn_state_cache.pop(session_id, None)
    # Confirm unlinks even when a previous failed sync already removed files.
    await _complete_io(partial(_sync_existing_directory, trusted_root=root), stream_directory)
    _PROVIDER_FENCED_SESSION_IDS.discard(session_id)
    _DELETION_FENCED_SESSION_IDS.discard(session_id)


def release_session_stream_fence(session_id: str) -> None:
    """Allow a durably restored owner to restart without deleting its history.

    Only call under the owning session lifecycle locks after restoration commits.
    """
    _PROVIDER_FENCED_SESSION_IDS.discard(session_id)
    _DELETION_FENCED_SESSION_IDS.discard(session_id)


async def stop_session_stream(session_id: str) -> None:
    """Stop in-process tailers/transports for a session WITHOUT clearing history.

    Unlike ``discard_session_stream``, this leaves the on-disk event log and
    attachment store intact. Used by archive, which releases runtime resources
    but may later restore the conversation.
    """
    for manager in list(_TAILER_MANAGERS):
        await manager.forget_session(session_id)
