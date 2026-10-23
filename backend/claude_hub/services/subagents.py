"""Hub subagents: one native Chat tab per subagent, one durable result per turn.

``claude-hub agent run`` asks another provider for help the way a native
subagent would: one call that returns the final answer. This service owns only
the small record that maps a subagent to its Chat tab and each turn to its
result. Delivery reuses the external-conversation adapter
(``dispatch_tab_chat_and_wait``) and lifecycle reuses ordinary tab archive.

Turns run in server-owned asyncio tasks, so a result survives the request or
CLI process that started it. Records live in ``<runtime home>/subagents.json``
(single backend owner via ``BackendInstanceLock``), written atomically off the
event loop. A turn still ``running`` when the backend starts again is marked
failed, never left pending.
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
    SUBAGENT_ID_ENV,
    SUBAGENT_MAX_DEPTH,
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
# Bounds keep subagents.json small: the Chat tab holds the full history.
_FINAL_TEXT_MAX_CHARS = 64_000
_TURNS_KEPT = 20  # per record, plus the first turn (create idempotency)
_CLOSED_RETENTION = timedelta(days=7)
_CLOSED_RETENTION_MAX = 500
_CANCEL_JOIN_SECONDS = 30.0
# Trailing provider events (usage, transcript flush) can follow the completion
# event; archiving at once would cut them off.
_ARCHIVE_GRACE_SECONDS = 3.0

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


class SubagentInteractionRequired(RuntimeError):
    """The provider turn stopped for an approval or question nobody can answer."""


RunTurn = Callable[[str, str, str, str, float], Awaitable[str]]
"""``run_turn(tab_id, text, turn_id, visible_text, timeout_seconds) -> final text``.

Must cancel the provider turn itself before raising ``TimeoutError``, retire
it when the awaiting task is cancelled, and raise
``SubagentInteractionRequired`` after retiring a turn that awaits input.
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
    # Caller identity is context, not request content: a retry is the same call.
    return _digest(
        req.model_dump(mode="json", exclude={"call_id", "caller_tab_id", "caller_subagent_id"})
    )


def _turn_digest(req: SubagentTurnCreate) -> str:
    return _digest(req.model_dump(mode="json", exclude={"call_id"}))


def _error_text(exc: BaseException) -> str:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, str) and detail:
        return detail
    return str(exc) or type(exc).__name__


def _bounded_text(text: str) -> Tuple[str, bool]:
    if len(text) <= _FINAL_TEXT_MAX_CHARS:
        return text, False
    return (
        text[:_FINAL_TEXT_MAX_CHARS]
        + f"\n[... truncated by Claude Hub after {_FINAL_TEXT_MAX_CHARS} characters; "
        "open the subagent's Chat tab for the rest]",
        True,
    )


def _interaction_error(record: Subagent) -> str:
    where = f"its Chat (tab {record.tab_id}) in Web Chat"
    if record.solo_mode:
        hint = "Make the prompt self-contained, or re-run with --keep and answer in " + where + "."
    else:
        hint = (
            "Approvals are on (solo mode off, as inherited from the parent Chat or set with "
            "--no-solo). Re-run with --solo to skip approvals, or with --keep and approve in "
            + where
            + "."
        )
    return (
        "The subagent stopped for an approval or a question that `agent run` cannot answer; "
        "the turn was retired. " + hint
    )


class SubagentService:
    def __init__(
        self,
        path: Path,
        runtime: SubagentRuntime,
        *,
        archive_grace_seconds: float = _ARCHIVE_GRACE_SECONDS,
    ) -> None:
        self.path = path
        self.runtime = runtime
        self.archive_grace_seconds = archive_grace_seconds
        self._subagents: Dict[str, Subagent] = {}
        self._tasks: Dict[str, asyncio.Task[None]] = {}  # running turn_id -> task
        self._done: Dict[str, asyncio.Event] = {}  # turn_id -> completion event
        self._locks: Dict[str, List[Any]] = {}  # key -> [lock, holders+waiters]
        # Every task this service spawns (turns, closes), so stop() can join them.
        self._background: set[asyncio.Task[Any]] = set()
        # Records whose close is in progress (claimed before the tab is archived).
        self._closing: set[str] = set()
        self._save_lock = asyncio.Lock()
        self._save_requested = 0
        self._save_written = 0
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

    async def _save(self, pending: Optional[Subagent] = None) -> None:
        """Persist all records (plus ``pending``, not yet published) atomically.

        Saves are serialized in call order and coalesced: a save whose state a
        later write already covered returns at once. The JSON is built on the
        loop (a consistent snapshot); the write and fsync run in a thread and
        are never abandoned midway, so a cancelled caller cannot let an older
        snapshot land after a newer one.
        """
        self._save_requested += 1
        generation = self._save_requested
        async with self._save_lock:
            if pending is None and self._save_written >= generation:
                return
            self._prune()
            covered = self._save_requested
            records = dict(self._subagents)
            if pending is not None:
                records[pending.id] = pending
            state = {
                "version": _STATE_VERSION,
                "subagents": {key: value.model_dump(mode="json") for key, value in records.items()},
            }
            write = asyncio.ensure_future(asyncio.to_thread(self._write_state, state))
            cancellation: Optional[asyncio.CancelledError] = None
            while not write.done():
                try:
                    await asyncio.shield(write)
                except asyncio.CancelledError as exc:
                    cancellation = cancellation or exc
                except Exception:
                    break
            if cancellation is not None:
                raise cancellation
            write.result()
            if pending is None:
                self._save_written = max(self._save_written, covered)

    def _write_state(self, state: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        for record in self._subagents.values():
            self._trim_turns(record)
        closed = sorted(
            (s for s in self._subagents.values() if s.closed),
            key=lambda s: s.closed_at or s.created_at,
            reverse=True,
        )
        cutoff = utc_now() - _CLOSED_RETENTION
        for index, record in enumerate(closed):
            if index >= _CLOSED_RETENTION_MAX or (record.closed_at or record.created_at) < cutoff:
                self._subagents.pop(record.id, None)

    @staticmethod
    def _trim_turns(record: Subagent) -> None:
        """Keep the first turn, the last ``_TURNS_KEPT`` and any running turn."""
        turns = record.turns
        if len(turns) <= _TURNS_KEPT + 1:
            return
        recent = {id(t) for t in turns[-_TURNS_KEPT:]}
        record.turns = [
            t
            for index, t in enumerate(turns)
            if index == 0 or id(t) in recent or t.status == SubagentTurnStatus.RUNNING
        ]

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Load records, fail interrupted turns and close what no caller can use."""
        from .terminal_runtime.retirement_listeners import add_tab_retired_listener

        await asyncio.to_thread(self._load)
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
            await self._save()
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
            elif own.subagent_id != record.id:
                # Restored by the user as an ordinary Chat; _close keeps the tab.
                await self._close(record, "subagent tab detached")
            elif not record.keep:
                reason = "Claude Hub restarted" if record.id in interrupted else "turn finished"
                await self._close(record, reason)
        # A crash between tab creation and record update can leave a tab that
        # no record owns; archive it (history stays restorable). A subagent tab
        # the user unarchived was detached (no subagent_id), so it is kept.
        for tab in list(self.runtime.list_tabs()):
            if tab.subagent_id and not tab.archived:
                owner = known_tabs.get(tab.id) or self._subagents.get(tab.subagent_id)
                if owner is None or owner.closed or owner.tab_id != tab.id:
                    await self._archive_quietly(tab.id)

    async def stop(self) -> None:
        """Cancel and join every turn and pending close this service started.

        A non-kept subagent whose close was still pending is closed by the next
        start's reconciliation.
        """
        self._shutting_down = True
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
        tasks = list(self._background)
        for task in tasks:
            task.cancel()
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=_CANCEL_JOIN_SECONDS)
            if pending:
                logger.warning("%d subagent task(s) did not stop in time", len(pending))
        # A turn cancelled mid-save re-raises before its result is known to be
        # covered; one last save makes sure a just-finished result is on disk.
        try:
            await self._save()
        except Exception:
            logger.exception("Failed to persist subagent state on shutdown")

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

    def _resolve_caller(self, req: SubagentCreate) -> Optional[TerminalTab]:
        """The Chat tab asking for a subagent, as far as the server knows it.

        The tab record wins over the caller-supplied subagent id: a tab the
        user restored as an ordinary Chat is no longer a subagent.
        """
        if req.caller_tab_id:
            tab = self.runtime.get_tab(req.caller_tab_id)
            if tab is not None:
                return tab
        if req.caller_subagent_id:
            record = self._subagents.get(req.caller_subagent_id)
            tab = self.runtime.get_tab(record.tab_id) if record and record.tab_id else None
            if record is None or record.closed or tab is None:
                raise SubagentError(
                    409,
                    f"Calling subagent {req.caller_subagent_id} is closed or unknown; "
                    "it cannot start another subagent.",
                )
            return tab
        return None

    def _check_depth(self, tab: TerminalTab) -> None:
        depth = self._subagent_depth(tab)
        if depth >= SUBAGENT_MAX_DEPTH:
            raise SubagentError(
                400,
                f"Subagent nesting limit reached: tab {tab.id} is already a "
                f"depth-{depth} subagent (max depth {SUBAGENT_MAX_DEPTH}). "
                "Do this work yourself instead of starting another subagent.",
            )

    def _resolve_parent(self, req: SubagentCreate) -> Optional[TerminalTab]:
        caller = self._resolve_caller(req)
        if caller is not None and caller.subagent_id:
            # A subagent's subagents always hang off its own Chat, so depth is
            # counted and its close cascades whatever the request names.
            self._check_depth(caller)
            if not req.parent_tab_id:
                raise SubagentError(
                    400,
                    f"A subagent cannot start an unlinked subagent (--no-parent); its "
                    f"subagents are linked to its own Chat (tab {caller.id}).",
                )
            if caller.archived:
                raise SubagentError(409, f"Calling subagent tab {caller.id} is archived")
            if req.parent_tab_id != caller.id:
                logger.info(
                    "Subagent tab %s asked for parent %s; linking to the caller instead",
                    caller.id,
                    req.parent_tab_id,
                )
            return caller
        if not req.parent_tab_id:
            return None
        parent = self.runtime.get_tab(req.parent_tab_id)
        if parent is None:
            raise SubagentError(404, f"Parent tab {req.parent_tab_id} not found")
        if parent.archived:
            raise SubagentError(409, f"Parent tab {req.parent_tab_id} is archived")
        self._check_depth(parent)
        return parent

    def _subagent_depth(self, tab: TerminalTab) -> int:
        """How many subagent links lead from ``tab`` up to an ordinary Chat."""
        depth = 0
        seen: set[str] = set()
        current: Optional[TerminalTab] = tab
        while current is not None and current.subagent_id and current.id not in seen:
            seen.add(current.id)
            depth += 1
            parent_id = current.parent_tab_id
            current = self.runtime.get_tab(parent_id) if parent_id else None
        return depth

    @staticmethod
    def _resolve_cwd(req: SubagentCreate, parent: Optional[TerminalTab]) -> str:
        cwd = req.cwd or (parent.cwd if parent is not None else None)
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
        parent = self._resolve_parent(req)
        cwd = self._resolve_cwd(req, parent)
        # Unless the caller chooses, a subagent follows its parent's approval
        # policy; without a parent nobody could approve, so it runs solo.
        if req.solo_mode is not None:
            solo_mode = req.solo_mode
        else:
            solo_mode = parent.solo_mode if parent is not None else True
        subagent_id = f"sa-{uuid.uuid4().hex[:12]}"
        env[SUBAGENT_ID_ENV] = subagent_id
        parent_id = parent.id if parent is not None else None
        first_line = req.prompt.strip().splitlines()[0][:60]
        name = req.name or f"{req.agent_type.value} subagent: {first_line}"
        record = Subagent(
            id=subagent_id,
            parent_tab_id=parent_id,
            agent_type=req.agent_type,
            model=req.model,
            effort=req.effort,
            cwd=cwd,
            env_preset=req.env_preset,
            name=name,
            keep=req.keep,
            solo_mode=solo_mode,
            created_at=utc_now(),
            created_by=created_by,
            call_id=req.call_id,
            request_digest=digest,
        )
        try:
            tab = await self.runtime.create_tab(
                name=name,
                cwd=cwd,
                solo_mode=solo_mode,
                agent_type=req.agent_type,
                session_kind=SessionKind.CHAT,
                env=env,
                parent_tab_id=parent_id,
                subagent_id=subagent_id,
            )
        except SubagentError:
            raise
        except ValueError as exc:
            raise SubagentError(400, str(exc)) from exc
        record.tab_id = tab.id
        try:
            # The parent may have been retired while the tab was starting.
            current = parent_id and self.runtime.get_tab(parent_id)
            if parent_id and (not current or current.archived):
                raise SubagentError(409, f"Parent tab {parent_id} was closed")
            turn = self._new_turn(req.call_id, req.timeout_seconds, digest)
            record.turns.append(turn)
            # Durable first, then visible: a record that was never saved must
            # not look running in memory (nor be returned to a call_id retry).
            try:
                await self._save(pending=record)
            except (OSError, ValueError, TypeError) as exc:
                logger.exception("Failed to persist new subagent %s", record.id)
                raise SubagentError(
                    503,
                    f"Could not record subagent state ({exc}); its Chat tab was archived and "
                    "nothing ran. Retry with the same call_id.",
                ) from exc
        except BaseException:
            await self._archive_quietly(tab.id)
            raise
        if self._shutting_down:
            # stop() already joined its tasks; spawning now would outlive it.
            await self._archive_quietly(tab.id)
            try:
                await self._save()  # drop the never-published record from disk
            except Exception:
                logger.exception("Failed to drop unpublished subagent %s", record.id)
            raise SubagentError(
                503,
                "Claude Hub is shutting down; the subagent's Chat tab was archived and "
                "nothing ran. Retry with the same call_id after it restarts.",
            )
        self._subagents[record.id] = record
        self._spawn(record, turn, wrap_first_prompt(req.prompt), req.prompt)
        # A parent retired during the save found no published record to close.
        current = parent_id and self.runtime.get_tab(parent_id)
        if parent_id and (not current or current.archived):
            self._track(
                self._close(record, "parent tab archived or deleted"),
                f"subagent-close-{record.id}",
            )
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
            if record.closed or record.id in self._closing:
                raise SubagentError(409, f"Subagent {subagent_id} is closed")
            if not record.keep:
                raise SubagentError(
                    409, "Only subagents created with keep=true accept follow-up turns"
                )
            if any(t.status == SubagentTurnStatus.RUNNING for t in record.turns):
                raise SubagentError(409, f"Subagent {subagent_id} is still running a turn")
            turn = self._new_turn(req.call_id, req.timeout_seconds, digest)
            record.turns.append(turn)
            try:
                await self._save()
            except BaseException as exc:
                record.turns.remove(turn)
                if isinstance(exc, (OSError, ValueError, TypeError)):
                    logger.exception("Failed to persist subagent turn on %s", subagent_id)
                    raise SubagentError(
                        503,
                        f"Could not record subagent state ({exc}); nothing was sent. "
                        "Retry with the same call_id.",
                    ) from exc
                raise
            self._spawn(record, turn, req.prompt, req.prompt)
            return record, turn

    async def cancel(
        self, subagent_id: str, turn_id: Optional[str] = None
    ) -> Tuple[Subagent, Optional[SubagentTurn]]:
        """Cancel the running turn, or only ``turn_id`` when given (no-op once it ended)."""
        record = self.get(subagent_id)
        if turn_id is not None:
            turn = self._turn(record, turn_id)
            await self._cancel_running(record, turn_id=turn_id)
            return record, turn
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

    def _track(self, coro: Awaitable[Any], name: str) -> "asyncio.Task[Any]":
        task: asyncio.Task[Any] = asyncio.ensure_future(coro)
        task.set_name(name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _spawn(self, record: Subagent, turn: SubagentTurn, text: str, visible: str) -> None:
        self._done[turn.turn_id] = asyncio.Event()
        self._tasks[turn.turn_id] = self._track(
            self._execute(record, turn, text, visible), f"subagent-{record.id}-{turn.turn_id}"
        )

    async def _execute(self, record: Subagent, turn: SubagentTurn, text: str, visible: str) -> None:
        status = SubagentTurnStatus.FAILED
        final_text: Optional[str] = None
        truncated = False
        error: Optional[str] = None
        try:
            assert record.tab_id is not None
            answer = await self.runtime.run_turn(
                record.tab_id, text, turn.turn_id, visible, turn.timeout_seconds
            )
            status = SubagentTurnStatus.COMPLETED
            final_text, truncated = _bounded_text(answer)
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
        except SubagentInteractionRequired:
            error = _interaction_error(record)
        except Exception as exc:
            error = _error_text(exc)
        turn.status = status
        turn.final_text = final_text
        turn.final_text_truncated = truncated
        turn.error = error
        turn.completed_at = utc_now()
        self._tasks.pop(turn.turn_id, None)
        try:
            await self._save()
        except Exception:
            logger.exception("Failed to persist subagent turn %s", turn.turn_id)
        finally:
            event = self._done.pop(turn.turn_id, None)
            if event is not None:
                event.set()
        if (
            not record.keep
            and not record.closed
            and record.id not in self._closing
            and not self._shutting_down
        ):
            self._track(
                self._close_after_grace(record, f"turn {status.value}"),
                f"subagent-close-{record.id}",
            )

    async def _close_after_grace(self, record: Subagent, reason: str) -> None:
        if self.archive_grace_seconds > 0:
            await asyncio.sleep(self.archive_grace_seconds)
        await self._close(record, reason)

    async def _cancel_running(self, record: Subagent, *, turn_id: Optional[str] = None) -> None:
        tasks = [
            self._tasks[t.turn_id]
            for t in record.turns
            if t.status == SubagentTurnStatus.RUNNING
            and t.turn_id in self._tasks
            and (turn_id is None or t.turn_id == turn_id)
        ]
        current = asyncio.current_task()
        tasks = [task for task in tasks if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=_CANCEL_JOIN_SECONDS)

    async def _close(self, record: Subagent, reason: str) -> None:
        if record.closed or record.id in self._closing:
            return
        # Claim first: a cancelled turn task must not start its own close. The
        # record reports closed only after its tab is archived, so no reader
        # sees a closed subagent whose Chat is still live. An interrupted close
        # (shutdown) leaves the record open for the next start to reconcile.
        self._closing.add(record.id)
        try:
            await self._cancel_running(record)
            if record.tab_id:
                tab = self.runtime.get_tab(record.tab_id)
                # A tab the user unarchived was detached from this record: keep it.
                if tab is not None and tab.subagent_id == record.id:
                    await self._archive_quietly(record.tab_id)
            record.closed = True
            record.closed_at = utc_now()
            record.close_reason = reason
        finally:
            self._closing.discard(record.id)
        try:
            await self._save()
        except Exception:
            logger.exception("Failed to persist closing subagent %s", record.id)

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
            if not s.closed
            and s.id not in self._closing
            and (s.parent_tab_id == tab_id or s.tab_id == tab_id)
        ]
        if not affected or self._shutting_down:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        for record in affected:
            reason = (
                "parent tab archived or deleted"
                if record.parent_tab_id == tab_id
                else "subagent tab archived or deleted"
            )
            self._track(self._close(record, reason), f"subagent-close-{record.id}")


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
        from ..api.agent_stream import ExternalInteractionRequired, dispatch_tab_chat_and_wait

        try:
            return await dispatch_tab_chat_and_wait(
                tab_id,
                text,
                turn_id,
                visible_text=visible_text,
                timeout_seconds=timeout_seconds,
                prefer_final_text=True,
            )
        except ExternalInteractionRequired as exc:
            raise SubagentInteractionRequired(str(exc)) from exc

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
