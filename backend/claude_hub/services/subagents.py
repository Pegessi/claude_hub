"""Hub subagents: one native Chat tab per subagent, one durable result per turn.

``claude-hub agent run`` asks another provider for help the way a native
subagent would: one call that returns the final answer. This service owns only
the small record that maps a subagent to its Chat tab and each turn to its
result. Delivery reuses the external-conversation adapter
(``dispatch_tab_chat_and_wait``) and lifecycle reuses ordinary tab archive.

Turns run in server-owned asyncio tasks, so a result survives the request or
CLI process that started it. Records live in ``<runtime home>/subagents.json``
(single backend owner via ``BackendInstanceLock``). A turn still ``running``
when the backend starts again is marked failed, never left pending.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

from ..models import AgentType, SessionKind, TerminalTab
from ..models.subagent import (
    SUBAGENT_MAX_WAIT_SECONDS,
    TERMINAL_SUBAGENT_TURN_STATUSES,
    Subagent,
    SubagentCreate,
    SubagentTurn,
    SubagentTurnCreate,
    SubagentTurnStatus,
    utc_now,
)

logger = logging.getLogger(__name__)

_STATE_VERSION = 1
_FINAL_TEXT_MAX_CHARS = 1_000_000
_CLOSED_RETENTION = timedelta(days=7)
_CLOSED_RETENTION_MAX = 500
_CANCEL_JOIN_SECONDS = 30.0

# Model and reasoning-effort env keys the native Chat transports already read.
_MODEL_ENV_KEYS = {
    AgentType.CLAUDE: "ANTHROPIC_MODEL",
    AgentType.CODEX: "CODEX_MODEL",
    AgentType.TRAEX: "CODEX_MODEL",
    AgentType.CURSOR: "CURSOR_MODEL",
    AgentType.MASO: "MASO_MODEL",
}
_EFFORT_ENV_KEYS = {
    AgentType.CODEX: "CODEX_REASONING_EFFORT",
    AgentType.TRAEX: "CODEX_REASONING_EFFORT",
}
REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})

SUBAGENT_PREAMBLE = (
    "You are a subagent invoked through Claude Hub by another agent. Do the task below "
    "yourself, in this conversation. Your final message is returned verbatim to the caller "
    "as your answer, so make it complete and self-contained. Ask a question only if you are "
    "truly blocked; nobody can answer interactive prompts."
)
_PREAMBLE_OPEN = "<claude_hub_subagent>"
_PREAMBLE_CLOSE = "</claude_hub_subagent>"


def wrap_first_prompt(prompt: str) -> str:
    return f"{_PREAMBLE_OPEN}\n{SUBAGENT_PREAMBLE}\n{_PREAMBLE_CLOSE}\n\n{prompt}"


class SubagentError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


RunTurn = Callable[[str, str, str, str, float], Awaitable[str]]
"""``run_turn(tab_id, text, turn_id, visible_text, timeout_seconds) -> final text``.

Must cancel the provider turn itself before raising ``TimeoutError`` and
retire it when the awaiting task is cancelled.
"""


@dataclass
class SubagentRuntime:
    """Tab/Chat operations the service needs; tests substitute fakes."""

    create_tab: Callable[..., Awaitable[TerminalTab]]
    run_turn: RunTurn
    archive_tab: Callable[[str], Awaitable[Any]]
    get_tab: Callable[[str], Optional[TerminalTab]]
    list_tabs: Callable[[], Iterable[TerminalTab]]
    resolve_env: Callable[[Optional[str], Dict[str, str]], Dict[str, str]]


def _digest(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _create_digest(req: SubagentCreate) -> str:
    return _digest(req.model_dump(mode="json", exclude={"call_id"}))


def _turn_digest(req: SubagentTurnCreate) -> str:
    return _digest(req.model_dump(mode="json", exclude={"call_id"}))


def _error_text(exc: BaseException) -> str:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, str) and detail:
        return detail
    return str(exc) or type(exc).__name__


def _bounded_text(text: str) -> str:
    if len(text) <= _FINAL_TEXT_MAX_CHARS:
        return text
    return (
        text[:_FINAL_TEXT_MAX_CHARS]
        + "\n[... truncated by Claude Hub; open the Chat tab for the rest]"
    )


class SubagentService:
    def __init__(self, path: Path, runtime: SubagentRuntime) -> None:
        self.path = path
        self.runtime = runtime
        self._subagents: Dict[str, Subagent] = {}
        self._tasks: Dict[str, asyncio.Task[None]] = {}  # turn_id -> task
        self._done: Dict[str, asyncio.Event] = {}  # turn_id -> completion event
        self._locks: Dict[str, List[Any]] = {}  # key -> [lock, holders+waiters]
        self._background: set[asyncio.Task[Any]] = set()
        self._shutting_down = False
        self._started = False
        self._remove_listener: Optional[Callable[[], None]] = None

    # ── persistence ──────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            # A corrupt result file must not block the backend; keep the bad
            # copy for diagnosis and start empty.
            logger.exception("Unreadable subagent state %s; starting empty", self.path)
            try:
                self.path.replace(self.path.with_suffix(f".corrupt-{uuid.uuid4().hex[:8]}"))
            except OSError:
                pass
            return
        raw = payload.get("subagents") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            return
        for key, value in raw.items():
            try:
                record = Subagent.model_validate(value)
            except Exception:
                logger.warning("Dropping malformed subagent record %s", key)
                continue
            self._subagents[record.id] = record

    def _save(self) -> None:
        self._prune()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        state = {
            "version": _STATE_VERSION,
            "subagents": {
                key: value.model_dump(mode="json") for key, value in self._subagents.items()
            },
        }
        fd, raw_tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        tmp = Path(raw_tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=1, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def _prune(self) -> None:
        closed = sorted(
            (s for s in self._subagents.values() if s.closed),
            key=lambda s: s.closed_at or s.created_at,
            reverse=True,
        )
        cutoff = utc_now() - _CLOSED_RETENTION
        for index, record in enumerate(closed):
            if index >= _CLOSED_RETENTION_MAX or (record.closed_at or record.created_at) < cutoff:
                self._subagents.pop(record.id, None)

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Load records, fail interrupted turns and close what no caller can use."""
        from .terminal_runtime.retirement_listeners import add_tab_retired_listener

        self._load()
        interrupted: set[str] = set()
        now = utc_now()
        for record in self._subagents.values():
            for turn in record.turns:
                if turn.status == SubagentTurnStatus.RUNNING:
                    turn.status = SubagentTurnStatus.FAILED
                    turn.error = "Claude Hub restarted before this subagent turn finished"
                    turn.completed_at = now
                    interrupted.add(record.id)
        if interrupted:
            self._save()
        self._remove_listener = add_tab_retired_listener(self._on_tab_retired)
        self._started = True
        known_tabs = {s.tab_id: s for s in self._subagents.values() if s.tab_id}
        # Retirements while the backend was down produced no notification.
        for record in list(self._subagents.values()):
            if record.closed:
                continue
            parent = record.parent_tab_id and self.runtime.get_tab(record.parent_tab_id)
            own = record.tab_id and self.runtime.get_tab(record.tab_id)
            if record.parent_tab_id and (not parent or parent.archived):
                await self._close(record, "parent tab archived or deleted")
            elif not own or own.archived:
                await self._close(record, "subagent tab archived or deleted")
            elif not record.keep:
                reason = "Claude Hub restarted" if record.id in interrupted else "turn finished"
                await self._close(record, reason)
        # A crash between tab creation and record update can leave a tab that
        # no record owns; archive it (history stays restorable).
        for tab in list(self.runtime.list_tabs()):
            if tab.subagent_id and not tab.archived:
                owner = known_tabs.get(tab.id) or self._subagents.get(tab.subagent_id)
                if owner is None or owner.closed or owner.tab_id != tab.id:
                    await self._archive_quietly(tab.id)

    async def stop(self) -> None:
        self._shutting_down = True
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
        tasks = list(self._tasks.values()) + list(self._background)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=_CANCEL_JOIN_SECONDS)

    # ── queries ──────────────────────────────────────────────────────────

    def get(self, subagent_id: str) -> Subagent:
        record = self._subagents.get(subagent_id)
        if record is None:
            raise SubagentError(404, f"Subagent {subagent_id} not found")
        return record

    def list_subagents(
        self, *, parent_tab_id: Optional[str] = None, include_closed: bool = False
    ) -> List[Subagent]:
        rows = [
            s
            for s in self._subagents.values()
            if (parent_tab_id is None or s.parent_tab_id == parent_tab_id)
            and (include_closed or not s.closed)
        ]
        rows.sort(key=lambda s: s.created_at, reverse=True)
        return rows

    @staticmethod
    def _turn(record: Subagent, turn_id: Optional[str]) -> SubagentTurn:
        if not record.turns:
            raise SubagentError(404, f"Subagent {record.id} has no turns")
        if turn_id is None:
            return record.turns[-1]
        for turn in record.turns:
            if turn.turn_id == turn_id:
                return turn
        raise SubagentError(404, f"Turn {turn_id} not found on subagent {record.id}")

    async def wait_turn(
        self, subagent_id: str, turn_id: Optional[str], wait_seconds: float = 0.0
    ) -> Tuple[Subagent, SubagentTurn]:
        record = self.get(subagent_id)
        turn = self._turn(record, turn_id)
        wait = min(max(wait_seconds, 0.0), SUBAGENT_MAX_WAIT_SECONDS)
        if turn.status == SubagentTurnStatus.RUNNING and wait > 0:
            event = self._done.get(turn.turn_id)
            if event is not None:
                try:
                    await asyncio.wait_for(event.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
        return self.get(subagent_id), turn

    # ── mutations ────────────────────────────────────────────────────────

    @asynccontextmanager
    async def _keyed(self, key: str) -> AsyncIterator[None]:
        """Serialize one key; entries are dropped when nobody holds or awaits them."""
        entry = self._locks.get(key)
        if entry is None:
            entry = self._locks[key] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._locks.pop(key, None)

    def _require_running_service(self) -> None:
        if self._shutting_down or not self._started:
            raise SubagentError(503, "Subagents are unavailable while Claude Hub starts or stops")

    def _launch_env(self, req: SubagentCreate) -> Dict[str, str]:
        if req.agent_type == AgentType.TERMINAL:
            raise SubagentError(400, "agent_type must be a Chat provider, not terminal")
        try:
            env = self.runtime.resolve_env(req.env_preset, dict(req.env))
        except ValueError as exc:
            raise SubagentError(400, str(exc)) from exc
        if req.model:
            env[_MODEL_ENV_KEYS[req.agent_type]] = req.model.strip()
        if req.effort:
            effort = req.effort.strip().lower()
            key = _EFFORT_ENV_KEYS.get(req.agent_type)
            if key is None:
                raise SubagentError(
                    400,
                    f"{req.agent_type.value} Chat has no separate reasoning-effort setting; "
                    "choose a model variant instead",
                )
            if effort not in REASONING_EFFORTS:
                raise SubagentError(
                    400, f"effort must be one of {', '.join(sorted(REASONING_EFFORTS))}"
                )
            env[key] = effort
        return env

    def _resolve_cwd(self, req: SubagentCreate) -> str:
        cwd = req.cwd
        if req.parent_tab_id:
            parent = self.runtime.get_tab(req.parent_tab_id)
            if parent is None:
                raise SubagentError(404, f"Parent tab {req.parent_tab_id} not found")
            if parent.archived:
                raise SubagentError(409, f"Parent tab {req.parent_tab_id} is archived")
            cwd = cwd or parent.cwd
        if not cwd:
            raise SubagentError(400, "cwd is required when no parent tab supplies one")
        cwd = os.path.expanduser(cwd)
        if not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise SubagentError(400, f"cwd must be an existing absolute directory: {cwd}")
        return cwd

    async def create(
        self, req: SubagentCreate, *, created_by: Optional[str] = None
    ) -> Tuple[Subagent, SubagentTurn]:
        self._require_running_service()
        digest = _create_digest(req)
        if req.call_id is None:
            return await self._create_locked(req, digest, created_by)
        async with self._keyed(f"create:{req.call_id}"):
            for record in self._subagents.values():
                if record.call_id == req.call_id:
                    if record.request_digest != digest:
                        raise SubagentError(
                            409, f"call_id {req.call_id} was already used with a different request"
                        )
                    return record, record.turns[0]
            return await self._create_locked(req, digest, created_by)

    async def _create_locked(
        self, req: SubagentCreate, digest: str, created_by: Optional[str]
    ) -> Tuple[Subagent, SubagentTurn]:
        env = self._launch_env(req)
        cwd = self._resolve_cwd(req)
        subagent_id = f"sa-{uuid.uuid4().hex[:12]}"
        first_line = req.prompt.strip().splitlines()[0][:60]
        name = req.name or f"{req.agent_type.value} subagent: {first_line}"
        record = Subagent(
            id=subagent_id,
            parent_tab_id=req.parent_tab_id,
            agent_type=req.agent_type,
            model=req.model,
            effort=req.effort,
            cwd=cwd,
            env_preset=req.env_preset,
            name=name,
            keep=req.keep,
            created_at=utc_now(),
            created_by=created_by,
            call_id=req.call_id,
            request_digest=digest,
        )
        try:
            tab = await self.runtime.create_tab(
                name=name,
                cwd=cwd,
                solo_mode=req.solo_mode,
                agent_type=req.agent_type,
                session_kind=SessionKind.CHAT,
                env=env,
                parent_tab_id=req.parent_tab_id,
                subagent_id=subagent_id,
            )
        except SubagentError:
            raise
        except ValueError as exc:
            raise SubagentError(400, str(exc)) from exc
        record.tab_id = tab.id
        # The parent may have been retired while the tab was starting.
        parent = req.parent_tab_id and self.runtime.get_tab(req.parent_tab_id)
        if req.parent_tab_id and (not parent or parent.archived):
            await self._archive_quietly(tab.id)
            raise SubagentError(409, f"Parent tab {req.parent_tab_id} was closed")
        turn = self._new_turn(req.call_id, req.timeout_seconds, digest)
        record.turns.append(turn)
        self._subagents[record.id] = record
        self._save()
        self._spawn(record, turn, wrap_first_prompt(req.prompt), req.prompt)
        return record, turn

    async def send(
        self, subagent_id: str, req: SubagentTurnCreate
    ) -> Tuple[Subagent, SubagentTurn]:
        self._require_running_service()
        async with self._keyed(f"turns:{subagent_id}"):
            record = self.get(subagent_id)
            digest = _turn_digest(req)
            if req.call_id is not None:
                for turn in record.turns:
                    if turn.call_id == req.call_id:
                        if turn.request_digest != digest:
                            raise SubagentError(
                                409,
                                f"call_id {req.call_id} was already used with a different request",
                            )
                        return record, turn
            if record.closed:
                raise SubagentError(409, f"Subagent {subagent_id} is closed")
            if not record.keep:
                raise SubagentError(
                    409, "Only subagents created with keep=true accept follow-up turns"
                )
            if any(t.status == SubagentTurnStatus.RUNNING for t in record.turns):
                raise SubagentError(409, f"Subagent {subagent_id} is still running a turn")
            turn = self._new_turn(req.call_id, req.timeout_seconds, digest)
            record.turns.append(turn)
            self._save()
            self._spawn(record, turn, req.prompt, req.prompt)
            return record, turn

    async def cancel(self, subagent_id: str) -> Tuple[Subagent, Optional[SubagentTurn]]:
        record = self.get(subagent_id)
        await self._cancel_running(record)
        return record, (record.turns[-1] if record.turns else None)

    async def close(self, subagent_id: str, reason: str = "closed by caller") -> Subagent:
        record = self.get(subagent_id)
        await self._close(record, reason)
        return record

    # ── internals ────────────────────────────────────────────────────────

    @staticmethod
    def _new_turn(call_id: Optional[str], timeout_seconds: float, digest: str) -> SubagentTurn:
        return SubagentTurn(
            turn_id=f"subagent-turn-{uuid.uuid4().hex}",
            call_id=call_id,
            status=SubagentTurnStatus.RUNNING,
            started_at=utc_now(),
            timeout_seconds=timeout_seconds,
            request_digest=digest,
        )

    def _spawn(self, record: Subagent, turn: SubagentTurn, text: str, visible: str) -> None:
        self._done[turn.turn_id] = asyncio.Event()
        task = asyncio.create_task(
            self._execute(record, turn, text, visible), name=f"subagent-{record.id}-{turn.turn_id}"
        )
        self._tasks[turn.turn_id] = task

    async def _execute(self, record: Subagent, turn: SubagentTurn, text: str, visible: str) -> None:
        status = SubagentTurnStatus.FAILED
        final_text: Optional[str] = None
        error: Optional[str] = None
        try:
            assert record.tab_id is not None
            answer = await self.runtime.run_turn(
                record.tab_id, text, turn.turn_id, visible, turn.timeout_seconds
            )
            status, final_text = SubagentTurnStatus.COMPLETED, _bounded_text(answer)
        except asyncio.CancelledError:
            # Cancellation is this record's own decision (cancel/close/shutdown);
            # the adapter has already retired the provider turn.
            if self._shutting_down:
                error = "Claude Hub shut down before this subagent turn finished"
            else:
                status, error = SubagentTurnStatus.CANCELLED, "Cancelled"
        except TimeoutError:
            status = SubagentTurnStatus.TIMEOUT
            error = (
                f"No final answer within {turn.timeout_seconds:g} seconds; the turn was cancelled"
            )
        except Exception as exc:
            error = _error_text(exc)
        finally:
            self._tasks.pop(turn.turn_id, None)
        turn.status = status
        turn.final_text = final_text
        turn.error = error
        turn.completed_at = utc_now()
        try:
            self._save()
        except Exception:
            logger.exception("Failed to persist subagent turn %s", turn.turn_id)
        event = self._done.pop(turn.turn_id, None)
        if event is not None:
            event.set()
        if not record.keep and not record.closed and not self._shutting_down:
            await self._close(record, f"turn {status.value}")

    async def _cancel_running(self, record: Subagent) -> None:
        tasks = [
            self._tasks[t.turn_id]
            for t in record.turns
            if t.status == SubagentTurnStatus.RUNNING and t.turn_id in self._tasks
        ]
        current = asyncio.current_task()
        tasks = [task for task in tasks if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=_CANCEL_JOIN_SECONDS)

    async def _close(self, record: Subagent, reason: str) -> None:
        if record.closed:
            return
        # Mark first: a cancelled turn task must not start its own close.
        record.closed = True
        record.closed_at = utc_now()
        record.close_reason = reason
        await self._cancel_running(record)
        self._save()
        if record.tab_id:
            await self._archive_quietly(record.tab_id)

    async def _archive_quietly(self, tab_id: str) -> None:
        tab = self.runtime.get_tab(tab_id)
        if tab is None or tab.archived:
            return
        try:
            await self.runtime.archive_tab(tab_id)
        except Exception:
            logger.exception("Failed to archive subagent tab %s", tab_id)

    def _on_tab_retired(self, tab_id: str) -> None:
        affected = [
            s
            for s in self._subagents.values()
            if not s.closed and (s.parent_tab_id == tab_id or s.tab_id == tab_id)
        ]
        if not affected or self._shutting_down:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for record in affected:
            reason = (
                "parent tab archived or deleted"
                if record.parent_tab_id == tab_id
                else "subagent tab archived or deleted"
            )
            task = loop.create_task(self._close(record, reason), name=f"subagent-close-{record.id}")
            self._background.add(task)
            task.add_done_callback(self._background.discard)


def is_terminal(status: SubagentTurnStatus) -> bool:
    return status in TERMINAL_SUBAGENT_TURN_STATUSES


def subagent_state_path() -> Path:
    from .runtime_isolation import resolve_runtime_home

    return resolve_runtime_home() / "subagents.json"


def _default_runtime() -> SubagentRuntime:
    from . import ttyd_manager
    from .env_preset_resolver import merge_env_with_preset

    async def run_turn(
        tab_id: str, text: str, turn_id: str, visible_text: str, timeout_seconds: float
    ) -> str:
        # Imported lazily: the API facade owns the bound tailer manager.
        from ..api.agent_stream import dispatch_tab_chat_and_wait

        return await dispatch_tab_chat_and_wait(
            tab_id,
            text,
            turn_id,
            visible_text=visible_text,
            timeout_seconds=timeout_seconds,
            prefer_final_text=True,
        )

    def list_tabs() -> Iterable[TerminalTab]:
        return [process.to_schema() for process in list(ttyd_manager.processes.values())]

    return SubagentRuntime(
        create_tab=ttyd_manager.create_tab,
        run_turn=run_turn,
        archive_tab=ttyd_manager.archive_tab,
        get_tab=ttyd_manager.get_tab,
        list_tabs=list_tabs,
        resolve_env=lambda preset, env: merge_env_with_preset(preset=preset, explicit_env=env),
    )


_service: Optional[SubagentService] = None


def get_subagent_service() -> SubagentService:
    global _service
    if _service is None:
        _service = SubagentService(subagent_state_path(), _default_runtime())
    return _service


def set_subagent_service(service: Optional[SubagentService]) -> None:
    """Test seam: install (or clear) the process-wide service."""
    global _service
    _service = service
