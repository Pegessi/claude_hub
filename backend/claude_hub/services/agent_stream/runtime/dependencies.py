"""Named runtime dependencies resolved by the legacy entrypoint."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Protocol, Set

from ....models import AgentStreamEventType, ManagedSession
from ..base import AgentStreamAdapter
from ..native import ProviderSession
from .goal_protocol import _GoalProtocolSanitizer


class NativeSessionFactory(Protocol):
    def __call__(
        self,
        session: ManagedSession,
        conversation_id_persist: Optional[Callable[[str], None]] = None,
        seed_history: Optional[str] = None,
        on_seed_consumed: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> ProviderSession: ...


class ImageCacheWriter(Protocol):
    async def __call__(
        self,
        session: ManagedSession,
        paths: List[str],
        *,
        owner_is_current: Optional[Callable[[], bool]] = None,
    ) -> None: ...


class RuntimeDependencies(Protocol):
    """Live compatibility dependencies; implementations contain no session state."""

    @property
    def POLL_INTERVAL_S(self) -> float: ...

    @property
    def IDLE_TTL_S(self) -> float: ...

    @property
    def STREAM_INACTIVITY_TIMEOUT_S(self) -> float: ...

    @property
    def ACTIVE_TURN_HARD_LIVENESS_TIMEOUT_S(self) -> float: ...

    @property
    def _MIN_TURN_SILENCE_S(self) -> float: ...

    @property
    def QUEUED_TURN_MAX_WAIT_S(self) -> float: ...

    @property
    def QUEUE_HEARTBEAT_STALL_S(self) -> float: ...

    @property
    def GOAL_TERMINAL_GRACE_S(self) -> float: ...

    @property
    def DISCOVERY_GRACE_S(self) -> float: ...

    @property
    def SUBSCRIBER_QUEUE_MAX(self) -> int: ...

    @property
    def COMPLETION_QUEUE_MAX(self) -> int: ...

    @property
    def _STOP_JOIN_TIMEOUT_S(self) -> float: ...

    @property
    def _RUNTIME_INTERRUPTED_MESSAGE(self) -> str: ...

    @property
    def _INACTIVITY_TIMEOUT_MESSAGE(self) -> str: ...

    @property
    def _HARD_LIVENESS_TIMEOUT_MESSAGE(self) -> str: ...

    @property
    def _QUEUE_CAP_TIMEOUT_MESSAGE(self) -> str: ...

    @property
    def _QUEUE_STALL_MESSAGE(self) -> str: ...

    @property
    def _TERMINAL_PERSISTENCE_FAILURE_MESSAGE(self) -> str: ...

    @property
    def _UNATTRIBUTED_DROP_TYPES(self) -> frozenset[AgentStreamEventType]: ...

    @property
    def _GENERATION_ACTIVITY_TYPES(self) -> frozenset[AgentStreamEventType]: ...

    @property
    def _HARD_FAILED_SESSION_IDS(self) -> Set[str]: ...

    @property
    def _DELETION_FENCED_SESSION_IDS(self) -> Set[str]: ...

    @property
    def _RETIRING_TAILER_IDS(self) -> Set[int]: ...

    @property
    def _GOAL_STATUS_TERMINAL_RE(self) -> re.Pattern[str]: ...

    @property
    def _GOAL_TAIL_SCAN_CHARS(self) -> int: ...

    @property
    def _GoalProtocolSanitizer(self) -> type[_GoalProtocolSanitizer]: ...

    @property
    def create_native_session(self) -> NativeSessionFactory: ...

    @property
    def cache_image_paths(self) -> ImageCacheWriter: ...

    @property
    def discover_source_cached(
        self,
    ) -> Callable[[AgentStreamAdapter, ManagedSession], Optional[Path]]: ...

    @property
    def invalidate_source(self) -> Callable[[str], None]: ...

    @property
    def snapshot_transcript(self) -> Callable[[Optional[Path]], Optional[Path]]: ...

    @property
    def restore_transcript(self) -> Callable[[Optional[Path], Optional[Path]], None]: ...

    @property
    def fork_transcript(self) -> Callable[[ManagedSession, Any, int, str], int]: ...

    @property
    def discard_snapshot(self) -> Callable[[Optional[Path]], None]: ...

    @property
    def get_adapter_for_session(
        self,
    ) -> Callable[[ManagedSession], Optional[AgentStreamAdapter]]: ...

    @property
    def read_seed_sidecar(self) -> Callable[[str, str], Optional[str]]: ...

    @property
    def discard_seed_sidecar(self) -> Callable[[str, str], None]: ...

    @property
    def provider_text_for_stored_turn(self) -> Callable[[str, Mapping[str, Any]], str]: ...

    @property
    def _detect_image_mime(self) -> Callable[[bytes], Optional[str]]: ...

    @property
    def parse_ask_question_response(self) -> Callable[[str], Optional[List[Dict[str, Any]]]]: ...
