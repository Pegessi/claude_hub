"""Single state owner shared by the session runtime mixins."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from ....models import AgentStreamEvent, ManagedSession
from ..attachments import AgentStreamAttachmentStore
from ..base import AgentStreamAdapter
from ..coalescer import AgentStreamCoalescer
from ..local_images import ImageReferenceBuffer
from ..native import ProviderSession
from ..store import AgentStreamStore
from .dependencies import RuntimeDependencies
from .goal_protocol import _GoalProtocolSanitizer
from .values import (
    ExternalTurnRetirement,
    NativeRuntimeSnapshot,
    PostPersistObserver,
    _PendingApproval,
)


class SessionState(ABC):
    """One initializer and typed contract for all session responsibilities."""

    def __init__(
        self,
        dependencies: RuntimeDependencies,
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
        self._deps = dependencies
        self.workspace_id = workspace_id
        self.session_id = session_id
        self.adapter = adapter
        self._session_getter = session_getter
        initial_session = session_getter()
        self._session_identity = (
            (
                initial_session.workspace_id,
                initial_session.tab_id,
                initial_session.created_at,
            )
            if initial_session is not None
            else None
        )
        self._store = store or AgentStreamStore(workspace_id, session_id)
        self._attachment_store: Optional[AgentStreamAttachmentStore] = None
        self._image_references = ImageReferenceBuffer()
        self._cursor_path = self._store.cursor_path
        self._native_transport = native_transport
        # When set, the agent session required a native transport but it could
        # not be created. Fail-closed: never fall back to transcript as a
        # real-time source for agent sessions.
        self._native_error = native_error
        self._post_persist_observers = post_persist_observers or []
        self._task_activity_observer = task_activity_observer
        self._send_admission = send_admission

        self._offset = 0
        self._inode: Optional[int] = None
        self._run_epoch = 0
        self._loaded_cursor = False
        self._snapshot_source_ids: List[str] = []
        self._snapshot_source_kinds: List[str] = []
        self._snapshot_digest: Optional[str] = None
        # Native transports have no backfill concept: every record is live.
        # Transcript/snapshot sessions start in backfill mode (_is_live=False)
        # and flip to live after the first successful read.
        self._is_live = native_transport is not None

        # The active user turn's stable id. Set by ``send_message`` the moment
        # a user turn is submitted (before the provider runs), and cleared when
        # the turn completes. Every provider event normalized while this is set
        # is stamped with it so the frontend can upsert by identity.
        self._active_turn_id: Optional[str] = None
        self._assistant_text: str = ""
        # Approval cards (``approval_required``) awaiting an answer, keyed by
        # the card's ``call_id``. Each entry captures the turn the card was
        # emitted in so ``_emit_approval_resolved`` can stamp the durable
        # ``approval_resolved`` event with the card's own turn even when the
        # answer is delivered as a steer that cancels that turn (Claude/Cursor).
        # Cleared when the turn completes.
        self._pending_approvals: Dict[str, _PendingApproval] = {}
        # Whether the provider has emitted a terminal ``turn_completed`` event
        # for the active turn. Used at EOF to decide whether a failed
        # ``turn_completed`` must be synthesized (nonzero exit or early EOF
        # with no completion record).
        self._turn_completed_seen: bool = False
        self._goal_protocol_sanitizer: Optional[_GoalProtocolSanitizer] = None
        # Ordinary turns in a Chat with Goal history are sanitized too, but only
        # Goal continuation turns may synthesize a completion from the signal.
        self._goal_protocol_turn: bool = False
        self._visible_assistant_text: str = ""
        # The turn's last answer: visible text after its last tool call. A chat
        # app gets this, not the narration between tool calls.
        self._final_assistant_text: str = ""
        self._final_text_after_tool: bool = False
        # Runtime status is terminalized as soon as the authoritative
        # TURN_COMPLETED event has been persisted and fanned out. The turn
        # guard (``turn_in_flight``) is also released at TURN_COMPLETED for
        # every adapter, since TURN_COMPLETED is the provider's final record
        # and there are no trailing records after it. This prevents the guard
        # from staying stuck while a one-shot subprocess lingers (e.g. a
        # long-running tool call keeps the process alive after the turn).
        self._native_terminal_status: Optional[NativeRuntimeSnapshot] = None

        self._task: Optional[asyncio.Task[Any]] = None
        self._stop_lock = asyncio.Lock()
        self._retirement_pending = False
        self._subscribers: Set[asyncio.Queue[AgentStreamEvent]] = set()
        self._completion_subscribers: Set[asyncio.Queue[AgentStreamEvent]] = set()
        self._last_subscriber_at = time.monotonic()
        # Wall-clock stamp of when the current in-flight turn was first
        # observed active; ``None`` when no turn is in flight. Used to clear
        # per-turn watchdog state after a turn finishes.
        self._turn_in_flight_since: Optional[float] = None
        # Wall-clock stamp of the last accepted provider record in the current
        # turn. Initialized on turn start and updated on every accepted record;
        # ``None`` when no turn is in flight. Drives the model-stream
        # inactivity timeout in ``_run_native``.
        self._last_event_at: Optional[float] = None
        # Tool calls and approval questions are external waits: the model is
        # not expected to stream while they are outstanding. Track stable
        # provider call ids so a long shell command or unanswered blocking
        # question is not mistaken for a dead model stream. Durable approval
        # cards can outlive their turn (Claude/Cursor follow-up answers), so
        # ``_blocking_approval_call_ids`` tracks only cards blocking the
        # current provider turn while ``_pending_approvals`` retains answer
        # routing for late responses.
        self._active_tool_call_ids: Set[str] = set()
        self._blocking_approval_call_ids: Set[str] = set()
        self._waiting_for_model_capacity = False
        # Capacity-queue waiting state (TraeX/Codex ``queue/status``). The turn
        # is accepted but generation has not started. ``_queued_since`` stamps
        # the start of the CURRENT contiguous queued stretch (cleared the moment
        # real model output or a ``ready`` arrives); ``_last_queue_at`` is the
        # latest queue heartbeat. A queue heartbeat does NOT refresh
        # ``_last_event_at``: queueing is not model-stream liveness.
        self._queued_since: Optional[float] = None
        self._last_queue_at: Optional[float] = None
        # Visible text of the last persisted queue snapshot for the active turn.
        # Consecutive queue/status snapshots that repeat the same rendered text
        # are suppressed at the source (they share one stable message id and the
        # frontend replaces rather than appends them), so a ~1/s flood cannot
        # bloat the durable stream; a changed position still persists.
        self._last_persisted_queue_text: Optional[str] = None
        # When the model's trailing Goal terminal envelope arrived, the stamp
        # at which it first became complete. While set, the silence watchdog is
        # suppressed; after ``GOAL_TERMINAL_GRACE_S`` without a provider
        # completion record the tailer synthesizes a completed turn instead.
        self._goal_terminal_at: Optional[float] = None
        self._hard_failed = False
        self._discovery_deadline: Optional[float] = None
        self._stopped = False
        self._drop_warned = False
        self._current_source: Optional[Path] = None
        self._last_error: Optional[str] = None
        self._poll_lock = asyncio.Lock()
        # Serializes composer input so two concurrent ``send_message`` calls
        # cannot race on ``_active_turn_id`` / ``_run_epoch``. The lock is
        # held across the busy-check, the authoritative ``turn_started``
        # publish, and the transport send so a busy second send never mutates
        # state that the in-flight first turn still depends on.
        self._send_lock = asyncio.Lock()
        # A user Stop persists the ``cancelled`` edge and releases the Hub
        # active-turn guard immediately, then lets the provider teardown
        # (interrupt grace / kill+relaunch+resume) run in this background task.
        # While it is pending the provider guard is still held and the watchdog
        # must not double-reap; composer sends await this barrier before
        # acquiring ``_send_lock`` so a fast resend is ordered AFTER the
        # kill/relaunch and reaches the fresh server.
        self._turn_teardown_task: Optional["asyncio.Task[None]"] = None
        self._turn_teardown_pending = False

        # Semantic coalescer for text_delta / thinking_delta bursts. Merges
        # consecutive same-stream deltas within a ~60ms window so the fanout
        # rate stays bounded. The coalescer invokes ``_persist_and_fanout``
        # for each merged event; non-coalescable events are flushed through
        # ``_publish`` which drains the coalescer first.
        self._coalescer = AgentStreamCoalescer(on_flush=self._persist_and_fanout)

    @property
    @abstractmethod
    def hard_failed(self) -> bool:
        raise NotImplementedError()

    @property
    @abstractmethod
    def store(self) -> AgentStreamStore:
        raise NotImplementedError()

    @property
    @abstractmethod
    def attachment_store(self) -> AgentStreamAttachmentStore:
        raise NotImplementedError()

    @abstractmethod
    def is_running(self) -> bool:
        raise NotImplementedError()

    @property
    @abstractmethod
    def last_error(self) -> Optional[str]:
        raise NotImplementedError()

    @property
    @abstractmethod
    def current_source(self) -> Optional[Path]:
        raise NotImplementedError()

    @property
    @abstractmethod
    def native_transport(self) -> Optional[ProviderSession]:
        raise NotImplementedError()

    @property
    @abstractmethod
    def native_started(self) -> bool:
        raise NotImplementedError()

    @property
    @abstractmethod
    def native_ready(self) -> bool:
        raise NotImplementedError()

    @property
    @abstractmethod
    def input_busy(self) -> bool:
        raise NotImplementedError()

    @property
    @abstractmethod
    def native_error(self) -> Optional[str]:
        raise NotImplementedError()

    @property
    @abstractmethod
    def native_terminal_status(self) -> Optional[NativeRuntimeSnapshot]:
        raise NotImplementedError()

    @abstractmethod
    def _reset_turn_watchdog_state(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _reset_queue_wait_state(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _terminalize_native_runtime(self, status: Any) -> None:
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    def _answer_covers_card(answers: List[Dict[str, Any]], card: _PendingApproval) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def answer_pending_question(
        self, text: str, expected_turn_id: str, *, expected_call_id: Optional[str] = None
    ) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def resolve_answered_questions(
        self,
        text: str,
        *,
        expected_turn_id: Optional[str] = None,
        expected_call_id: Optional[str] = None,
    ) -> None:
        raise NotImplementedError()

    @abstractmethod
    def accepts_question_followup(
        self, text: str, expected_turn_id: str, *, expected_call_id: Optional[str] = None
    ) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def send_message(
        self,
        text: str,
        images: List[bytes],
        client_turn_id: str,
        *,
        previews: Optional[List[Optional[bytes]]] = None,
        delivery: str = "normal",
        reuse_attachments: Optional[List[Dict[str, Any]]] = None,
        visible_text: Optional[str] = None,
        turn_metadata: Optional[Dict[str, Any]] = None,
        strip_goal_protocol: bool = False,
        before_send: Optional[Callable[[], Awaitable[None]]] = None,
        before_delivery: Optional[Callable[[], None]] = None,
        allow_question_answer: bool = True,
    ) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def cancel_turn(self, expected_turn_id: Optional[str] = None) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def retire_external_turn(self, expected_turn_id: str) -> ExternalTurnRetirement:
        raise NotImplementedError()

    @abstractmethod
    async def _recover_orphaned_turn_locked(self, expected_turn_id: Optional[str] = None) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def _terminalize_orphaned_turn_locked(self, orphan: AgentStreamEvent) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _publish_turn_completion(
        self,
        *,
        turn_id: Optional[str],
        run_epoch: Optional[int],
        status: str,
        error_message: Optional[str] = None,
    ) -> AgentStreamEvent:
        raise NotImplementedError()

    @abstractmethod
    def _waiting_for_external_activity(self) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def _stream_inactive(self) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def _turn_hard_liveness_expired(self) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def _note_assistant_text(self, transport: ProviderSession) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _goal_terminal_grace_expired(self) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def _synthesize_goal_completion_locked(self, transport: ProviderSession) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _record_watchdog_activity(self, event: AgentStreamEvent) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _note_raw_provider_wait(self, record: Any) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def _is_duplicate_queue_snapshot(self, event: AgentStreamEvent) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def _queued_heartbeat_stalled(self) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def _queued_wait_cap_expired(self) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def _cancel_active_turn_locked(
        self,
        transport: ProviderSession,
        *,
        error_message: Optional[str] = None,
        await_teardown: bool = False,
        require_teardown: bool = False,
    ) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _spawn_turn_teardown(self, transport: ProviderSession) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _run_turn_teardown(self, transport: ProviderSession) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _turn_teardown_done(self, task: "asyncio.Task[None]") -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _await_turn_teardown(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _reap_active_turn_locked(
        self, transport: ProviderSession, error_message: str
    ) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def _fail_active_turn(
        self, message: str, transport: ProviderSession
    ) -> Optional[AgentStreamEvent]:
        raise NotImplementedError()

    @abstractmethod
    async def poll_once(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def subscribe(self) -> "asyncio.Queue[AgentStreamEvent]":
        raise NotImplementedError()

    @abstractmethod
    async def subscribe_completion(self) -> "asyncio.Queue[AgentStreamEvent]":
        raise NotImplementedError()

    @abstractmethod
    def unsubscribe(self, queue: "asyncio.Queue[AgentStreamEvent]") -> None:
        raise NotImplementedError()

    @abstractmethod
    def unsubscribe_completion(self, queue: "asyncio.Queue[AgentStreamEvent]") -> None:
        raise NotImplementedError()

    @abstractmethod
    def matches_session(self, session: ManagedSession) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def _incarnation_is_current(self) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def _require_startable_incarnation(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _require_publishable_incarnation(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _publishable_incarnation_is_current(self) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def start(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def stop(self, *, raise_errors: bool = False) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _stop(self, *, raise_errors: bool) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _run(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _run_poll(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _run_native(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _consume_turn_completion_record(
        self, event: AgentStreamEvent, observer_event: AgentStreamEvent, transport: ProviderSession
    ) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def _publish_delivery_rejection_event(self, event: AgentStreamEvent) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _notify_post_persist(self, event: AgentStreamEvent) -> None:
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    async def _run_observer(observer: PostPersistObserver, event: AgentStreamEvent) -> None:
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    def _log_observer_failure(task: "asyncio.Task[None]") -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _poll_once(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _poll_native(self, session: ManagedSession) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _tail_file(self, path: Path, session: ManagedSession) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _tail_snapshot(self, path: Path, session: ManagedSession) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _read_new_lines(self, path: Path) -> Tuple[List[Dict[str, Any]], int, int, bool]:
        raise NotImplementedError()

    @abstractmethod
    def _record_approval_card(self, event: AgentStreamEvent) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _emit_approval_resolved(
        self,
        answers: Optional[List[Dict[str, Any]]] = None,
        *,
        expected_turn_id: Optional[str] = None,
        expected_call_id: Optional[str] = None,
    ) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _persist_and_fanout(self, event: AgentStreamEvent) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _publish(self, event: AgentStreamEvent) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _fanout_volatile_terminal_failure(self, event: AgentStreamEvent) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _fanout_completion(self, event: AgentStreamEvent) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _fanout(self, event: AgentStreamEvent) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _load_cursor(self, path: Path) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _save_cursor(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _reset_for_rotation(self, path: Path) -> None:
        raise NotImplementedError()

    @abstractmethod
    def peek_task_execution_ref(self, tab_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError()
