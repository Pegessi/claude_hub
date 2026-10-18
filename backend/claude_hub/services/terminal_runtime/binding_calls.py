"""Explicit callback signatures used by terminal runtime bindings."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Tuple

from ...models import AgentStreamEvent, AgentType, ExecutionTarget, SessionKind
from .records import ScanEntry, _PendingTabCleanup


class AgentSpawnEnvCall(Protocol):
    def __call__(self) -> dict: ...


class ClaudeProjectDirForCwdCall(Protocol):
    def __call__(self, cwd: str) -> Path: ...


class CodexCandidatesForCwdCall(Protocol):
    def __call__(
        self, cwd: str, scan: Optional[Dict[str, ScanEntry]] = ...
    ) -> List[Tuple[float, str, str]]: ...


class CodexHomeDirCall(Protocol):
    def __call__(self) -> Path: ...


class CodexIdExistsCall(Protocol):
    def __call__(self, sid: str, cwd: Optional[str] = ...) -> bool: ...


class CodexRootsCall(Protocol):
    def __call__(self) -> List[Tuple[Path, bool]]: ...


class CodexScanSessionsCall(Protocol):
    def __call__(
        self, with_title: bool = ..., skip_title_sids: Optional[set] = ...
    ) -> Dict[str, ScanEntry]: ...


class CodexTitleFromLinesCall(Protocol):
    def __call__(self, lines: Iterable[str]) -> str: ...


class CursorIdExists(Protocol):
    def __call__(self, sid: str, cwd: str, home: Optional[str] = ...) -> bool: ...


class DiffScansCall(Protocol):
    def __call__(self, pre: Dict[str, ScanEntry], post: Dict[str, ScanEntry]) -> Dict[str, str]: ...


class EnsureTmuxServerCall(Protocol):
    def __call__(self) -> bool: ...


class EventTurnOrdinalsCall(Protocol):
    def __call__(self, events: List[AgentStreamEvent]) -> List[int]: ...


class FinishTmuxLauncherRollbackCall(Protocol):
    async def __call__(self, proc: asyncio.subprocess.Process, session_name: str) -> None: ...


class IsLocalPortAvailableCall(Protocol):
    def __call__(self, port: int) -> bool: ...


class IsLocalPortListeningCall(Protocol):
    def __call__(self, port: int) -> bool: ...


class IsLoopbackUrlCall(Protocol):
    def __call__(self, value: str) -> bool: ...


class JsonlStartEpochCall(Protocol):
    def __call__(self, path: str) -> Optional[float]: ...


class ParseOptionalIsoCall(Protocol):
    def __call__(self, value: Optional[str]) -> Optional[datetime]: ...


class ParseSessionMetaCall(Protocol):
    def __call__(
        self, first_line: str
    ) -> Tuple[Optional[str], Optional[float], Optional[str], str]: ...


class ParseTabsSnapshotCall(Protocol):
    def __call__(
        self, value: Any
    ) -> Tuple[List[Dict[str, Any]], Dict[str, _PendingTabCleanup]]: ...


class PickBackfillSessionCall(Protocol):
    def __call__(
        self, session_created: float, candidates: List[Tuple[float, str, str]]
    ) -> Optional[str]: ...


class PromoteCursorChatTransportCall(Protocol):
    def __call__(
        self,
        session_kind: SessionKind,
        agent_type: AgentType,
        target: ExecutionTarget,
        cursor_transport: str,
    ) -> str: ...


class RefreshTmuxServerEnvironmentCall(Protocol):
    def __call__(self) -> None: ...


class TmuxKillSessionCall(Protocol):
    async def __call__(self, session_name: str) -> None: ...


class TmuxListSessionCreatedCall(Protocol):
    async def __call__(self) -> dict[str, float]: ...


class TmuxListSessionsCall(Protocol):
    async def __call__(self) -> set[str]: ...


class TmuxServerRunningCall(Protocol):
    def __call__(self) -> bool: ...


class TmuxSessionCreatedCall(Protocol):
    def __call__(self, session_name: str) -> Optional[float]: ...


class TmuxSessionExistsAsyncCall(Protocol):
    async def __call__(self, session_name: str) -> bool: ...


class TmuxSessionNameCall(Protocol):
    def __call__(self, tab_id: str) -> str: ...


class WritePrivateLaunchFileCall(Protocol):
    def __call__(self, path: Path, content: str) -> None: ...


class WriteTabsSnapshotCall(Protocol):
    def __call__(self, payload: Dict[str, Any]) -> None: ...


class CursorCliVersionFromExecutableCall(Protocol):
    def __call__(self) -> Optional[str]: ...


class CursorDataDirForEnvCall(Protocol):
    def __call__(self, env: Optional[Dict[str, str]]) -> str: ...


class CursorTerminalTranscriptPathCall(Protocol):
    def __call__(self, cwd: str, session_id: str, *, data_dir: Optional[str] = ...) -> Path: ...


class CursorTerminalTranscriptProvenanceValidCall(Protocol):
    def __call__(
        self,
        *,
        cwd: Optional[str],
        session_id: Optional[str],
        cli_version: Optional[str],
        transcript_path: Optional[str],
        transcript_schema: Optional[str],
        data_dir: Optional[str],
        env: Optional[Dict[str, str]] = ...,
    ) -> bool: ...


class GetAgentCommandCall(Protocol):
    def __call__(self, agent_type: AgentType) -> str: ...


class GetDefaultCommandCall(Protocol):
    def __call__(self) -> str: ...


class GetTabNativeRuntimeSnapshotCall(Protocol):
    def __call__(self, tab_id: str) -> Any: ...


class NativeProviderBinaryAvailableCall(Protocol):
    def __call__(self, agent_type: AgentType) -> bool: ...


class TmuxCommand(Protocol):
    def __call__(
        self, *args: str, repo_root: Path | None = ..., environ: Mapping[str, str] | None = ...
    ) -> list[str]: ...
