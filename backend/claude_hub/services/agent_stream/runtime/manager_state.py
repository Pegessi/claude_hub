"""Single manager state and cross-responsibility contract."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncContextManager,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
)

from ....models import AgentStreamEvent, ManagedSession
from ..base import AgentStreamAdapter
from ..native import ProviderSession
from ..store import AgentStreamStore
from .dependencies import RuntimeDependencies
from .values import ExternalTurnRetirement, PostPersistObserver

if TYPE_CHECKING:
    from ..tailer import SessionTailer


class TailerFactory(Protocol):
    def __call__(
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
    ) -> SessionTailer: ...


class ManagerState(ABC):
    """One registry and lifecycle lock map per manager."""

    def __init__(
        self,
        dependencies: RuntimeDependencies,
        tailer_factory: TailerFactory,
        session_getter: Callable[[str], Optional[ManagedSession]],
        persist_session_id: Optional[Callable[[str, str], None]] = None,
        persist_mode: Optional[Callable[[str, str], None]] = None,
        post_persist_observers: Optional[List[PostPersistObserver]] = None,
        task_activity_observer: Optional[PostPersistObserver] = None,
    ) -> None:
        self._deps = dependencies
        self._tailer_factory = tailer_factory
        self._session_getter = session_getter
        # Optional durable persistence callback for the provider conversation
        # id. When provided, it is invoked with (session_id, conversation_id)
        # so the id survives a cold restart. When None, the id is only set on
        # the in-memory ManagedSession (used by tests).
        self._persist_session_id_cb = persist_session_id
        self._persist_mode_cb = persist_mode
        self._post_persist_observers = post_persist_observers or []
        self._task_activity_observer = task_activity_observer
        self._tailers: Dict[str, SessionTailer] = {}
        self._accepting_sends = True
        self._lock = asyncio.Lock()
        # The edit transaction owns its identity through rollback even when its
        # tailer is detached. Retry and retirement share this same barrier.
        self._lifecycle_locks: Dict[str, asyncio.Lock] = {}

    @abstractmethod
    def _retirement_guard(self, session_id: str) -> AsyncContextManager[None]:
        raise NotImplementedError()

    @abstractmethod
    def session_is_current(self, session: ManagedSession) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def quiesce(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _get_or_create(
        self, session: ManagedSession, *, required_conversation_id: Optional[str] = None
    ) -> SessionTailer:
        raise NotImplementedError()

    @abstractmethod
    async def subscribe(self, session: ManagedSession) -> "asyncio.Queue[AgentStreamEvent]":
        raise NotImplementedError()

    @abstractmethod
    async def subscribe_completion(
        self, session: ManagedSession
    ) -> "asyncio.Queue[AgentStreamEvent]":
        raise NotImplementedError()

    @abstractmethod
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
        strip_goal_protocol: bool = False,
        before_send: Optional[Callable[[], Awaitable[None]]] = None,
        before_delivery: Optional[Callable[[], None]] = None,
    ) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def cancel_turn(
        self, session: ManagedSession, expected_turn_id: Optional[str] = None
    ) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def retire_external_turn(
        self, session: ManagedSession, expected_turn_id: str
    ) -> ExternalTurnRetirement:
        raise NotImplementedError()

    @abstractmethod
    async def active_turn_id(self, session: ManagedSession) -> Optional[str]:
        raise NotImplementedError()

    @abstractmethod
    async def answer_pending_question(
        self,
        session: ManagedSession,
        text: str,
        expected_turn_id: str,
        *,
        expected_call_id: Optional[str] = None,
    ) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def turn_in_flight(self, session: ManagedSession) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def accepts_question_followup(
        self,
        session: ManagedSession,
        text: str,
        expected_turn_id: str,
        *,
        expected_call_id: Optional[str] = None,
    ) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def resolve_answered_questions(
        self,
        session: ManagedSession,
        text: str,
        *,
        expected_turn_id: Optional[str] = None,
        expected_call_id: Optional[str] = None,
    ) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def edit_resend(
        self,
        session: ManagedSession,
        text: str,
        client_turn_id: str,
        turn_id: str,
        *,
        turn_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _stop_tailer_for_edit(self, tailer: SessionTailer) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def _edit_resend_locked(
        self,
        session: ManagedSession,
        text: str,
        client_turn_id: str,
        turn_id: str,
        adapter: Any,
        *,
        turn_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def set_mode(self, session: ManagedSession, mode: str) -> None:
        raise NotImplementedError()

    @abstractmethod
    def set_env(self, session: ManagedSession, env: Dict[str, str]) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def ensure_started(
        self, session: ManagedSession, *, required_conversation_id: Optional[str] = None
    ) -> SessionTailer:
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    def _is_reusable(tailer: SessionTailer) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def retry(self, session: ManagedSession) -> SessionTailer:
        raise NotImplementedError()

    @abstractmethod
    def get_tailer(self, session_id: str) -> Optional[SessionTailer]:
        raise NotImplementedError()

    @abstractmethod
    def tailers(self) -> List[SessionTailer]:
        raise NotImplementedError()

    @abstractmethod
    def unsubscribe(self, session_id: str, queue: "asyncio.Queue[AgentStreamEvent]") -> None:
        raise NotImplementedError()

    @abstractmethod
    def unsubscribe_completion(
        self, session_id: str, queue: "asyncio.Queue[AgentStreamEvent]"
    ) -> None:
        raise NotImplementedError()

    @abstractmethod
    def hard_failed(self, session_id: str) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def is_structured_available(self, session: ManagedSession) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def get_store(self, workspace_id: str, session_id: str) -> AgentStreamStore:
        raise NotImplementedError()

    @abstractmethod
    def _persist_session_id(self, session_id: str, conversation_id: str) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def fence_session(self, session_id: str) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def forget_session(self, session_id: str) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def stop_all(self) -> None:
        raise NotImplementedError()
