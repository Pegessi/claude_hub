"""One attachment cache state object and explicit shared method contract."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .dependencies import MediaDependencies


class AttachmentState(ABC):
    """Single cache owner and typed contract for store/GC responsibilities."""

    DEFAULT_MAX_PREVIEW_BYTES = 8 * 1024 * 1024  # 8 MiB
    DEFAULT_MAX_SESSION_COUNT = 5000
    DEFAULT_MAX_SESSION_BYTES = 1024 * 1024 * 1024  # 1 GiB
    DEFAULT_MAX_GLOBAL_COUNT = 5000
    DEFAULT_MAX_GLOBAL_BYTES = 1024 * 1024 * 1024  # 1 GiB

    def __init__(
        self,
        dependencies: MediaDependencies,
        workspace_id: str,
        session_id: str,
        *,
        max_preview_bytes: Optional[int] = None,
        max_session_count: Optional[int] = None,
        max_session_bytes: Optional[int] = None,
        max_global_count: Optional[int] = None,
        max_global_bytes: Optional[int] = None,
        max_age_seconds: Optional[float] = None,
    ) -> None:
        # Limits default to application settings so every construction shares
        # the same operationally-configurable values. Explicit overrides
        # (e.g. in tests) are still honored.
        self._deps = dependencies
        cfg = self._deps._store_kwargs_from_settings()
        if max_preview_bytes is None:
            max_preview_bytes = cfg["max_preview_bytes"]
        if max_session_count is None:
            max_session_count = cfg["max_session_count"]
        if max_session_bytes is None:
            max_session_bytes = cfg["max_session_bytes"]
        if max_global_count is None:
            max_global_count = cfg["max_global_count"]
        if max_global_bytes is None:
            max_global_bytes = cfg["max_global_bytes"]
        # max_age_seconds stays None (disabled) unless set in settings or
        # explicitly passed.
        if max_age_seconds is None:
            max_age_seconds = cfg["max_age_seconds"]

        if max_preview_bytes <= 0:
            raise ValueError("max_preview_bytes must be positive")
        if max_session_count <= 0:
            raise ValueError("max_session_count must be positive")
        if max_session_bytes <= 0:
            raise ValueError("max_session_bytes must be positive")
        if max_global_count <= 0:
            raise ValueError("max_global_count must be positive")
        if max_global_bytes <= 0:
            raise ValueError("max_global_bytes must be positive")
        if max_age_seconds is not None:
            if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
                raise ValueError("max_age_seconds must be a positive finite number or None")

        # Consistency: a single valid preview must fit within both the session
        # and global byte budgets, otherwise it would be immediately
        # self-evicted and ``save`` would return an id that no longer exists.
        if max_preview_bytes > max_session_bytes:
            raise ValueError(
                f"max_preview_bytes ({max_preview_bytes}) must not exceed "
                f"max_session_bytes ({max_session_bytes})"
            )
        if max_preview_bytes > max_global_bytes:
            raise ValueError(
                f"max_preview_bytes ({max_preview_bytes}) must not exceed "
                f"max_global_bytes ({max_global_bytes})"
            )

        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("workspace_id must be a non-empty string")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")

        self.workspace_id = workspace_id
        self.session_id = session_id
        self.max_preview_bytes = max_preview_bytes
        self.max_session_count = max_session_count
        self.max_session_bytes = max_session_bytes
        self.max_global_count = max_global_count
        self.max_global_bytes = max_global_bytes
        self.max_age_seconds = max_age_seconds

    @property
    @abstractmethod
    def _session_dir(self) -> Path:
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    def _read_index(*, strict: bool = False) -> List[Dict[str, Any]]:
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    def _write_index(entries: List[Dict[str, Any]]) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _session_entries(self, index: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        raise NotImplementedError()

    @abstractmethod
    def has_residual_state(self) -> bool:
        raise NotImplementedError()

    @abstractmethod
    async def save(
        self,
        mime_type: str,
        preview_bytes: bytes,
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
        source_key: Optional[str] = None,
        owner_is_current: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError()

    @abstractmethod
    async def discard_source(
        self, source_key: str, *, owner_is_current: Optional[Callable[[], bool]] = None
    ) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def read_source(
        self, source_key: str, *, owner_is_current: Optional[Callable[[], bool]] = None
    ) -> Tuple[bytes, str]:
        raise NotImplementedError()

    @abstractmethod
    async def read(self, attachment_id: str) -> Tuple[bytes, str]:
        raise NotImplementedError()

    @abstractmethod
    def _read_entry(self, index: List[Dict[str, Any]], entry: Dict[str, Any]) -> Tuple[bytes, str]:
        raise NotImplementedError()

    @abstractmethod
    async def _delete_by_id(self, attachment_id: str) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def clear(self, *, strict: bool = False) -> None:
        raise NotImplementedError()

    @abstractmethod
    async def gc(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _gc_scan_root(self, root: Path, valid_keys: set) -> None:
        raise NotImplementedError()

    @abstractmethod
    def _evict_session(self, index: List[Dict[str, Any]]) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def _evict_all_sessions(self, index: List[Dict[str, Any]]) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def _evict_global(self, index: List[Dict[str, Any]]) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def _evict_aged(self, index: List[Dict[str, Any]]) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def _delete_entry(
        self,
        index: List[Dict[str, Any]],
        entry: Dict[str, Any],
        *,
        already_removed: bool = False,
        persist: bool = True,
    ) -> None:
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    def _validate_id(attachment_id: str) -> None:
        raise NotImplementedError()
