"""Dot service: tab ownership, per-turn context, journal sync, delegation and wake-ups."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .context import TurnPack, build_session_pack, build_turn_pack
from .index import DotIndex
from .lessons import lesson_score, lint_brief, same_rule, session_rules
from .prompts import BRIEF_TEMPLATE, QUIET_REPLY, TIDY_MESSAGE, WAKE_MESSAGE
from .store import FOCUS_LIMIT, Card, DotStore, now_iso, utc_now

logger = logging.getLogger(__name__)

TAB_STREAM_WORKSPACE_ID = "terminal-tabs"
TAB_STREAM_SESSION_PREFIX = "terminal-tab-"
# Rotate the provider session before raw history piles up.
MAX_SESSION_TURNS = 24
MAX_SESSION_AGE = timedelta(hours=18)
# Dot is woken only by watcher events (run now). Schedules need a timing, so the
# wake-up gets one that never comes due: ten years.
WAKE_INTERVAL_SECONDS = 10 * 365 * 24 * 3600
TIDY_CRON = "17 3 * * *"
# Task state changes are rare; one cheap in-memory pass a minute is enough.
WATCH_INTERVAL_SECONDS = 60.0
DEFAULT_STALL_HOURS = 2.0
_TERMINAL_TASK_STATES = {"done", "failed"}
# Only running tasks can look stuck; review waits on a person, todo was never started.
_STALL_TASK_STATES = {"queued", "working"}
# A worker question: it counts against first pass.
_QUESTION_REPORT_STATES = {"needs_input", "blocked"}
_ATTENTION_REPORT_STATES = _QUESTION_REPORT_STATES | {"review_needs_input"}
# Report states that hand the work back (a new round after a verdict).
_RETURN_REPORT_STATES = {"ready_for_review", "completed", "failed"}
# review_reason of the report the Hub writes when a task is moved to review by hand,
_MANUAL_REVIEW_REASON = "Manual status transition to REVIEW."
# ... and the end of the review_reason of the Hub's stuck-review reaper.
_REAPER_REASON_SUFFIX = "recovered by background dispatcher."
# Per report field on a result card; a huge report must not make the card unsavable.
RESULT_TEXT_LIMIT = 20_000
# Prepared turns waiting for their end; more than this and the oldest never ended.
_SENT_TURNS_KEPT = 10
_JOURNAL_TEXT_LIMIT = 20_000
# skip: Dot cannot judge this one (say why in the note). Not counted anywhere.
REVIEW_VERDICTS = ("ok", "redo", "skip")
# change: the user changed what they want. No lesson, and not counted in the first-pass rate.
REVIEW_CAUSES = ("brief", "env", "worker", "change")
# First-pass rate covers the latest reviews only, so it shows the current trend.
STATS_WINDOW = 20


class LessonNotFound(LookupError):
    """A review named a lesson id that does not exist."""


class DelegateNotStarted(Exception):
    """The delegated task was created (and is tracked) but did not start."""


@dataclass
class TurnPlan:
    """What the provider transport must apply before sending one Dot turn."""

    rotate: bool
    conversation_id: Optional[str]
    system_pack: str
    turn_pack: str
    # Inbox events shown in ``turn_pack``; ack them only once the turn is sent.
    delivered_event_ids: list[str]


def _parse_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _checklist(value: Any) -> list[str]:
    """Done-when items; work items from before checklists hold one string."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def _ended_at(task: Any) -> Optional[str]:
    """When the task's latest run failed or finished; each run sets its own stamp."""
    status = getattr(getattr(task, "status", None), "value", None)
    field = {"failed": "failed_at", "done": "completed_at"}.get(str(status))
    moment = getattr(task, field, None) if field else None
    return moment.isoformat() if isinstance(moment, datetime) else None


def _report_state(report: Any) -> str:
    return str(getattr(getattr(report, "state", None), "value", "") or "")


def _sent_turns(state: dict[str, Any]) -> list[dict[str, Any]]:
    turns = state.get("sent_turns")
    return [turn for turn in turns if isinstance(turn, dict)] if isinstance(turns, list) else []


def _hub_report(report: Any) -> bool:
    """A report the Hub wrote to ask for a review: a manual move to review, a review
    asked again, or the stuck-review reaper. It carries no work and no question, so
    it is neither a new round, a result to show, nor news for Dot.
    """
    decision = getattr(getattr(report, "review_decision", None), "value", None)
    reason = str(getattr(report, "review_reason", None) or "")
    return (
        decision == "request"
        and not getattr(report, "changed_files", None)
        and not getattr(report, "validation", None)
        and not getattr(report, "risks", None)
        and (
            reason == getattr(report, "message", None)
            or reason == _MANUAL_REVIEW_REASON
            or reason.endswith(_REAPER_REASON_SUFFIX)
        )
    )


def _clip_text(text: str, limit: int = RESULT_TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _first_line(text: str, limit: int = 200) -> str:
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


class DotService:
    """One Dot per Hub runtime. Every method is safe to call when Dot is not set up."""

    def __init__(self, store: Optional[DotStore] = None) -> None:
        self.store = store or DotStore()
        self.index = DotIndex(self.store)
        self._journal_lock: Optional[asyncio.Lock] = None
        self._setup_lock: Optional[asyncio.Lock] = None
        self._wake_pending = False
        self._watch_task: Optional[asyncio.Task[None]] = None
        self._state_cache: tuple[tuple[int, int, int], dict[str, Any]] = ((-1, -1, -1), {})

    # ── identity ────────────────────────────────────────────────────────

    def _peek_state(self) -> dict[str, Any]:
        """Read state without creating the home (cheap; called on every Chat turn)."""
        path = self.store.home / "state.json"
        try:
            info = path.stat()
        except OSError:
            return {}
        # Writes are atomic replaces (new inode), so this key changes even when
        # two writes land in the same mtime tick.
        key = (info.st_ino, info.st_mtime_ns, info.st_size)
        cached_key, cached = self._state_cache
        if cached_key == key:
            return cached
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        state = value if isinstance(value, dict) else {}
        self._state_cache = (key, state)
        return state

    def tab_id(self) -> Optional[str]:
        value = self._peek_state().get("tab_id")
        return value if isinstance(value, str) and value else None

    def session_pack(self) -> str:
        """The current session's stable pack (no turn is counted)."""
        return str(self._peek_state().get("session_pack") or "")

    def is_dot_tab(self, tab_id: Optional[str]) -> bool:
        return bool(tab_id) and tab_id == self.tab_id()

    def is_set_up(self) -> bool:
        return self.tab_id() is not None

    # ── setup ───────────────────────────────────────────────────────────

    async def ensure_tab(self, env: Optional[dict[str, str]] = None) -> str:
        """Create (or reuse) the Dot Chat tab and its standing schedules.

        ``env`` is the provider launch env (API endpoint/key/model) for a new
        tab, exactly like a normal new Chat. See ``env_seed_needed`` for an
        existing tab.
        """
        # Two setups at once (double click, CLI) must not create two tabs.
        if self._setup_lock is None:
            self._setup_lock = asyncio.Lock()
        async with self._setup_lock:
            return await self._ensure_tab(env)

    async def _ensure_tab(self, env: Optional[dict[str, str]]) -> str:
        from ...models.schemas import AgentType, SessionKind
        from .. import ttyd_manager

        self.store.ensure()
        tab_id = self.tab_id()
        tab = ttyd_manager.get_tab(tab_id) if tab_id else None
        if tab is not None and getattr(tab, "archived", False):
            await ttyd_manager.unarchive_tab(tab.id)
        if tab is None:
            created = await ttyd_manager.create_tab(
                name=self.store.read_identity()["name"],
                cwd=str(self.store.home),
                solo_mode=True,
                agent_type=AgentType.CLAUDE,
                session_kind=SessionKind.CHAT,
                env=env or None,
            )
            tab_id = created.id
            self.store.update_state(
                tab_id=tab_id,
                session_conversation_id=None,
                session_turns=0,
                journal_seq=-1,
                session_pack=None,
            )
            self.store.commit("dot: new chat tab")
        assert tab_id is not None
        self.ensure_schedules(tab_id)
        return tab_id

    def env_seed_needed(self, env: Optional[dict[str, str]]) -> bool:
        """True when the Dot tab still runs on the built-in fallback env.

        Such a tab has no working provider yet, so setup may give it ``env``;
        a tab whose env the user chose keeps it.
        """
        from .. import ttyd_manager
        from ..ttyd_manager import DEFAULT_CLAUDE_LAUNCH_ENV

        tab_id = self.tab_id()
        tab = ttyd_manager.get_tab(tab_id) if tab_id and env else None
        if tab is None:
            return False
        current = dict(getattr(tab, "env", None) or {})
        return current in ({}, DEFAULT_CLAUDE_LAUNCH_ENV) and current != dict(env or {})

    def ensure_schedules(self, tab_id: str) -> None:
        """Keep the wake-up and nightly tidy Chat schedules pointing at the Dot tab."""
        from ...models.schemas import (
            ScheduledTaskCreate,
            ScheduledTaskKind,
            ScheduledTaskUpdate,
        )
        from .. import workspace_manager

        state = self.store.read_state()
        timings: tuple[tuple[str, str, dict[str, Any], str], ...] = (
            (
                "wake_schedule_id",
                "Dot wake-up",
                {"interval_seconds": WAKE_INTERVAL_SECONDS},
                WAKE_MESSAGE,
            ),
            ("tidy_schedule_id", "Dot nightly tidy", {"cron": TIDY_CRON}, TIDY_MESSAGE),
        )
        for key, name, timing, message in timings:
            existing = workspace_manager.scheduled_tasks.get(state.get(key) or "")
            if existing is not None:
                update: dict[str, Any] = {}
                if existing.tab_id != tab_id:
                    # The Hub turns off a schedule whose Chat was deleted. Its time is
                    # sent again so the next run is worked out anew, not a missed one.
                    update.update(tab_id=tab_id, enabled=True)
                    update.update(
                        {
                            field: getattr(existing, field, None) or value
                            for field, value in timing.items()
                        }
                    )
                # Older homes woke Dot every morning; move them to events only. A
                # time the user picked in the right form is kept.
                if any(getattr(existing, field, None) is None for field in timing):
                    update.update(timing)
                if update:
                    workspace_manager.update_scheduled_task(
                        existing.id, ScheduledTaskUpdate(**update)
                    )
                continue
            created = workspace_manager.create_scheduled_task(
                ScheduledTaskCreate(
                    name=name,
                    kind=ScheduledTaskKind.CHAT_TURN,
                    tab_id=tab_id,
                    message=message,
                    **timing,
                )
            )
            # Kept at once: if the next one fails, a retry must not create this again.
            self.store.update_state(**{key: created.id})

    # ── turns ───────────────────────────────────────────────────────────

    def prepare_turn(self, text: str, conversation_id: Optional[str]) -> TurnPlan:
        """Decide rotation and build both packs for one Dot turn."""
        self.store.ensure()
        # Read-decide-write under the store lock so a concurrent rotation
        # request is not overwritten.
        with self.store.lock:
            return self._prepare_turn(text, conversation_id)

    def _prepare_turn(self, text: str, conversation_id: Optional[str]) -> TurnPlan:
        state = self.store.read_state()
        now = utc_now()
        stripped = text.lstrip()
        tidy = stripped.startswith("[Dot tidy]")
        started = _parse_time(state.get("session_started_at"))
        turns = int(state.get("session_turns") or 0)
        same_session = bool(conversation_id) and conversation_id == state.get(
            "session_conversation_id"
        )
        rotate = (
            not conversation_id
            or tidy
            or bool(state.get("rotate_requested"))
            or (same_session and turns >= MAX_SESSION_TURNS)
            or (same_session and started is not None and now - started > MAX_SESSION_AGE)
        )
        new_session = rotate or not same_session
        if rotate:
            conversation_id = str(uuid.uuid4())
        if new_session:
            turns = 0
            system_pack = build_session_pack(self.store)
        else:
            system_pack = str(state.get("session_pack") or "") or build_session_pack(self.store)
        turns += 1
        pack: TurnPack = build_turn_pack(
            self.store,
            self.index,
            "" if tidy else text,
            first_turn=new_session,
            session_turn=turns,
            # A Hub wake-up's fixed words would recall the same cards every time.
            recall=not stripped.startswith("[Dot "),
            # A tidy turn is not the user's: events wait for a turn that can act on them.
            inbox=not tidy,
        )
        # Remembered until the turn ends: a failed turn gives its events and its
        # first-turn context back (see ``_turn_ended``).
        sent = {"at": utc_now().isoformat(), "ids": pack.delivered_event_ids, "first": new_session}
        updates: dict[str, Any] = {
            "session_conversation_id": conversation_id,
            "session_turns": turns,
            "session_pack": system_pack,
            # A tidy runs in its own session; the next real turn starts clean.
            "rotate_requested": True if tidy else None,
            "sent_turns": [*_sent_turns(state), sent][-_SENT_TURNS_KEPT:],
        }
        if new_session:
            updates["session_started_at"] = now_iso()
        if tidy:
            updates["last_tidy_at"] = now_iso()
        self.store.update_state(**updates)
        return TurnPlan(
            rotate=rotate,
            conversation_id=conversation_id,
            system_pack=system_pack,
            turn_pack=pack.text,
            delivered_event_ids=pack.delivered_event_ids,
        )

    def _turn_ended(self, started_at: Any, until: Any, ended_at: Any, failed: bool) -> None:
        """Match a finished turn to the turn Dot prepared for it.

        A turn is prepared after it starts and before the next one starts
        (``until``). A turn the provider never accepted ends with its start time,
        so the end time alone cannot place it. Otherwise the latest turn prepared
        before the end is taken.

        A failed turn never reached Dot: its events are unread again, and when it
        opened a session the next turn opens a fresh one (with the recent talk).
        """
        started, bound, ended = _parse_time(started_at), _parse_time(until), _parse_time(ended_at)
        if ended is None:
            return
        with self.store.lock:
            sent = _sent_turns(self.store.read_state())
            limit = max(bound, ended) if bound is not None else ended
            before = [one for one in sent if (_parse_time(one.get("at")) or ended) <= limit]
            mine = [
                one
                for one in before
                if started is not None
                and bound is not None
                and started <= (_parse_time(one.get("at")) or ended) < bound
            ]
            earlier = [one for one in before if (_parse_time(one.get("at")) or ended) <= ended]
            turn = mine[0] if mine else earlier[-1] if earlier else {}
            if not before:
                return
            rest = [one for one in sent if one not in before]
            changes: dict[str, Any] = {"sent_turns": rest or None}
            if failed and turn.get("first"):
                changes["rotate_requested"] = True
            self.store.update_state(**changes)
        # Only a turn the provider took marked its events read; a later turn may have
        # shown and read them since a turn that was never taken.
        ids = [str(one) for one in turn.get("ids") or []] if turn.get("acked") else []
        if failed and ids:
            self.store.mark_inbox_unread(ids)

    def mark_delivered(self, event_ids: list[str]) -> None:
        """The provider accepted a turn: its shown inbox events are now read."""
        if not event_ids:
            return
        self.store.mark_inbox_read(event_ids)
        with self.store.lock:
            sent = _sent_turns(self.store.read_state())
            for turn in reversed(sent):
                if turn.get("ids") == event_ids:
                    turn["acked"] = True
                    self.store.update_state(sent_turns=sent)
                    break

    def request_rotation(self) -> None:
        self.store.update_state(rotate_requested=True)

    # ── journal ─────────────────────────────────────────────────────────

    async def on_stream_event(self, event: Any) -> None:
        """Post-persist observer: sync the journal when a Dot turn completes."""
        from ...models.agent_stream import AgentStreamEventType

        if event.type != AgentStreamEventType.TURN_COMPLETED:
            return
        if not self.is_dot_tab(getattr(event, "tab_id", None)):
            return
        try:
            await self.sync_journal()
        except Exception:
            logger.exception("dot journal sync failed")

    async def sync_journal(self) -> int:
        """Append completed Dot exchanges to the journal; returns the number of turns added."""
        from ...models.agent_stream import AgentStreamEventType
        from ..agent_stream.store import AgentStreamStore

        tab_id = self.tab_id()
        if not tab_id:
            return 0
        if self._journal_lock is None:
            self._journal_lock = asyncio.Lock()
        async with self._journal_lock:
            stream = AgentStreamStore(
                TAB_STREAM_WORKSPACE_ID, f"{TAB_STREAM_SESSION_PREFIX}{tab_id}"
            )
            cursor = int(self.store.read_state().get("journal_seq", -1))
            if cursor >= 0 and await stream.count() == 0:
                cursor = -1
            starts: dict[str, tuple[str, Any]] = {}
            # Ended turns wait for the next start, the end of the window they were
            # prepared in; the last ones wait for the time of the last read.
            ended: list[tuple[Any, Any, bool]] = []
            added = 0
            last_completed = cursor
            while True:
                read_at = utc_now()
                page = await stream.read_since(cursor, limit=500)
                for event in page.events:
                    payload = event.payload if isinstance(event.payload, dict) else {}
                    if event.type == AgentStreamEventType.TURN_STARTED and event.turn_id:
                        starts[event.turn_id] = (
                            str(payload.get("summary") or ""),
                            event.created_at,
                        )
                        for turn in ended:
                            self._turn_ended(turn[0], event.created_at, turn[1], turn[2])
                        ended.clear()
                    elif event.type == AgentStreamEventType.TURN_COMPLETED:
                        asked, asked_at = starts.pop(event.turn_id or "", ("", None))
                        answer = str(payload.get("assistant_text") or "")
                        last_completed = event.stream_sequence
                        ended.append(
                            (asked_at, event.created_at, payload.get("status") == "failed")
                        )
                        from_hub = asked.lstrip().startswith("[Dot ")
                        if from_hub and answer.strip() == QUIET_REPLY:
                            # Nothing needed the user; keep it out of the recent talk.
                            continue
                        if asked.strip():
                            who = "hub" if from_hub else "you"
                            self.store.append_journal(
                                who, asked[:_JOURNAL_TEXT_LIMIT], _parse_time(asked_at)
                            )
                        if payload.get("status") == "failed":
                            # Error text is not something Dot said; keep a short note only.
                            self.store.append_journal(
                                "hub", "(turn failed)", _parse_time(event.created_at)
                            )
                        elif answer.strip():
                            self.store.append_journal(
                                "dot",
                                answer[:_JOURNAL_TEXT_LIMIT],
                                _parse_time(event.created_at),
                            )
                        added += 1
                cursor = page.next_sequence
                if not page.has_more:
                    break
            for turn in ended:
                self._turn_ended(turn[0], read_at, turn[1], turn[2])
            if last_completed != int(self.store.read_state().get("journal_seq", -1)):
                self.store.update_state(journal_seq=last_completed)
            if added:
                await asyncio.to_thread(self.store.commit, f"dot: journal +{added} turn(s)")
            return added

    # ── delegation ──────────────────────────────────────────────────────

    async def delegate(
        self,
        *,
        workspace_id: str,
        title: str,
        goal: str,
        done_when: list[str],
        output: str = "",
        context: str = "",
        out_of_scope: str = "",
        constraints: str = "",
        agent_type: str = "codex",
        task_mode: Optional[str] = None,
        start: bool = True,
        request_key: Optional[str] = None,
        stall_hours: Optional[float] = None,
        confirm: bool = False,
        actor_key: str,
    ) -> dict[str, Any]:
        """Check the brief, then register (and start) one Hub Task and track it.

        Returns ``held`` (nothing sent), the brief ``warnings`` and the tracked
        ``work`` item. A brief with warnings is held until Dot fixes it or sends
        ``confirm``. A retry with a known ``request_key`` is not checked again
        and leaves the tracked status to the watcher.
        """
        from ...models.schemas import (
            AgentType,
            StartTaskRequest,
            TaskSourceReference,
            WorkspaceTaskCreate,
            WorkspaceTaskMode,
        )
        from .. import workspace_manager
        from ..workspace_manager._task_execution import TaskExecutionConflict

        checks = [" ".join(item.split()) for item in done_when if item.strip()]
        if not title.strip() or not goal.strip() or not checks:
            raise ValueError("title, goal and done_when are required")
        self.store.ensure()
        warnings = lint_brief(done_when=checks, context=context, output=output)
        replay = bool(request_key) and any(
            item.get("request_key") == request_key
            for item in self.store.list_work(include_closed=True)
        )
        if warnings and not (confirm or replay):
            return {"held": True, "warnings": warnings, "work": None}
        brief = BRIEF_TEMPLATE.format(
            goal=goal.strip(),
            context=context.strip() or "(none given)",
            done_when="\n".join(f"{number}. {item}" for number, item in enumerate(checks, 1)),
            output=output.strip() or "(not given; say in the report where the result is)",
            out_of_scope=out_of_scope.strip() or "(none given)",
            constraints=constraints.strip() or "(none)",
        )
        fields: dict[str, Any] = {
            "title": title.strip(),
            "prompt": brief,
            "agent_type": AgentType(agent_type),
            "request_key": request_key,
        }
        tab_id = self.tab_id()
        if tab_id:
            fields["source"] = TaskSourceReference(kind="chat", tab_id=tab_id)
        if task_mode:
            fields["task_mode"] = WorkspaceTaskMode(task_mode)
        task, _replayed = await workspace_manager.register_task(
            workspace_id, WorkspaceTaskCreate(**fields), actor_key
        )
        # Tracked before it starts: a task that fails to start is still Dot's.
        item = self.store.upsert_work(
            task.id,
            workspace_id=workspace_id,
            title=task.title,
            goal=goal.strip(),
            done_when=checks,
            output=output.strip() or None,
            agent_type=agent_type,
            # A replayed task may have moved on since the last poll; the watcher must
            # still see that change, or its result event is lost.
            status=None if replay else task.status.value,
            stall_hours=stall_hours,
            request_key=request_key,
            # Kept so a later review can see which warnings Dot sent past.
            brief_warnings=warnings or None,
        )
        try:
            if start and task.status.value == "todo":
                task = await workspace_manager.start_task(
                    task.id, StartTaskRequest(agent_type=AgentType(agent_type))
                )
                if not replay:
                    item = self.store.upsert_work(task.id, status=task.status.value)
        except TaskExecutionConflict:
            raise
        except Exception as exc:
            raise DelegateNotStarted(
                f"Task {task.id} was created but did not start: {exc}"
            ) from exc
        finally:
            await asyncio.to_thread(self.store.commit, f"dot: delegate {task.id}")
        return {"held": False, "warnings": warnings, "work": item}

    # ── review and lessons ──────────────────────────────────────────────

    async def review(
        self,
        task_id: str,
        *,
        verdict: str,
        cause: Optional[str] = None,
        note: str = "",
        lesson: str = "",
        lesson_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Record Dot's verdict on a returned task; a brief miss adds or votes a lesson.

        A new rule that says nearly the same as a live lesson votes that lesson
        instead (``lesson_reused``). A redo not blamed on the brief needs a note,
        so blaming the environment, the worker or a changed request is never
        free. Raises KeyError for an unknown task, LessonNotFound for an unknown
        lesson id, ValueError for a bad combination.
        """
        item = self.store.get_work(task_id)
        if item is None:
            raise KeyError(task_id)
        if item.get("watched") or item.get("stopped"):
            raise ValueError("only tasks Dot delegated and still tracks are reviewed")
        if verdict not in REVIEW_VERDICTS:
            raise ValueError("verdict must be ok, redo or skip")
        if cause is not None and cause not in REVIEW_CAUSES:
            raise ValueError("cause must be brief, env, worker or change")
        if verdict == "redo" and cause is None:
            raise ValueError("a redo needs a cause: brief, env, worker or change")
        if verdict == "skip" and cause is not None:
            raise ValueError("a skip has no cause")
        if (verdict == "skip" or (verdict == "redo" and cause != "brief")) and not note.strip():
            raise ValueError(
                "say why in --note"
                + (": what you could not judge" if verdict == "skip" else ": what went wrong")
            )
        if not item.get("result_card"):
            raise ValueError("the task has not come back yet")
        if item.get("review") and not item.get("review_due"):
            raise ValueError("already reviewed; nothing new came back since")
        rule = lesson.strip()
        if (rule or lesson_id) and cause != "brief":
            raise ValueError("lessons are only for brief misses (cause brief)")
        if rule and lesson_id:
            raise ValueError("give a new lesson or a lesson id, not both")
        if cause == "brief" and not (rule or lesson_id):
            raise ValueError(
                "a brief miss needs a lesson: a new rule, or the id of an old one it repeats"
            )
        reused = False
        if rule and (same := same_rule(self.store.list_lessons(), rule)):
            lesson_id, reused = str(same["id"]), True
        saved = None
        if lesson_id:
            saved = self.store.vote_lesson(lesson_id, task_id)
            if saved is None:
                raise LessonNotFound(lesson_id)
        elif rule:
            saved = self.store.add_lesson(rule, task_id)
        record = {
            "verdict": verdict,
            "cause": cause,
            "note": " ".join(note.split()) or None,
            "lesson_id": saved["id"] if saved else None,
            "at": now_iso(),
        }
        changes: dict[str, Any] = {
            "review": {key: value for key, value in record.items() if value is not None},
            "review_due": False,
        }
        # The first verdict decides first-pass success. A skip is no verdict yet.
        if not item.get("first_verdict") and verdict != "skip":
            changes["first_verdict"] = verdict
            changes["first_cause"] = cause or ""
        elif item.get("first_verdict") and verdict == "redo" and cause != "change":
            # An ok that turned out wrong in a later round was not a first pass.
            changes["late_redo"] = True
        updated = self.store.upsert_work(task_id, **changes)
        await asyncio.to_thread(self.store.commit, f"dot: review {task_id} {verdict}")
        return {
            "work": updated,
            "lesson": saved,
            "lesson_reused": reused,
        }

    def lessons(self, *, include_dropped: bool = False) -> list[dict[str, Any]]:
        """Lessons with their current score and reach, strongest first.

        reach: ``always`` (in every session), ``hidden`` (too weak or outside the
        top few; kept as history), ``dropped`` or ``merged``.
        """
        now = utc_now()
        in_session = {lesson.get("id") for lesson in session_rules(self.store.list_lessons(), now)}
        rows = []
        for lesson in self.store.list_lessons(include_dropped=include_dropped):
            score = lesson_score(lesson, now)
            if lesson.get("merged_into"):
                reach = "merged"
            elif lesson.get("dropped"):
                reach = "dropped"
            else:
                reach = "always" if lesson.get("id") in in_session else "hidden"
            rows.append(
                {
                    **lesson,
                    "score": round(score, 2),
                    "in_session": reach == "always",
                    "reach": reach,
                }
            )
        return sorted(rows, key=lambda row: (bool(row.get("dropped")), -row["score"]))

    def review_stats(self) -> dict[str, Any]:
        """First-pass rate over the latest reviews: ok at the first verdict, no question asked,
        and no later redo.

        Tasks whose first review said the user changed what they want, and
        skipped ones, are left out. ``causes`` counts the first-round cause, so
        brief misses stand apart from env and worker ones. ``judged`` /
        ``returned`` show how many delegated tasks that came back got a verdict
        at all. ``confirmed`` / ``confirmed_first_pass`` cover briefs sent past
        the brief check's warnings: if they pass as often as the rest, the check
        is not worth its holds.
        """
        delegated = [
            item
            for item in self.store.list_work(include_closed=True)
            if not item.get("watched") and not item.get("stopped") and item.get("result_card")
        ]
        reviewed = [item for item in delegated if isinstance(item.get("review"), dict)]
        reviewed.sort(key=lambda item: str(item["review"].get("at") or ""))

        def first_cause(item: dict[str, Any]) -> str:
            return str(item.get("first_cause", item["review"].get("cause")) or "")

        def first_verdict(item: dict[str, Any]) -> str:
            return str(item.get("first_verdict", item["review"].get("verdict")) or "")

        window = [
            item
            for item in reviewed
            if first_cause(item) != "change" and first_verdict(item) in ("ok", "redo")
        ][-STATS_WINDOW:]

        def passed(item: dict[str, Any]) -> bool:
            return (
                first_verdict(item) == "ok" and not item.get("asked") and not item.get("late_redo")
            )

        first_pass = sum(1 for item in window if passed(item))
        confirmed = [item for item in window if item.get("brief_warnings")]
        causes: dict[str, int] = {}
        for item in window:
            cause = first_cause(item)
            if cause:
                causes[cause] = causes.get(cause, 0) + 1
        return {
            "reviewed": len(window),
            "first_pass": first_pass,
            "rate": round(first_pass / len(window), 2) if window else None,
            "causes": causes,
            "pending": len(self.store.pending_reviews()),
            "judged": len(reviewed),
            "returned": len(delegated),
            "confirmed": len(confirmed),
            "confirmed_first_pass": sum(1 for item in confirmed if passed(item)),
            "window": STATS_WINDOW,
        }

    async def watch(self, task_id: str, *, stall_hours: Optional[float] = None) -> dict[str, Any]:
        """Track an existing Hub task Dot did not start. Raises KeyError / ValueError."""
        from .. import workspace_manager

        task = workspace_manager.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        status = task.status.value
        if status in _TERMINAL_TASK_STATES:
            raise ValueError(f"task is already {status}")
        self.store.ensure()
        existing = self.store.get_work(task.id)
        # Dot's own delegation stays one: it keeps its review and unseen events.
        delegated = existing is not None and not existing.get("watched")
        if existing is not None and delegated and not existing.get("stopped"):
            return self.store.upsert_work(task.id, stall_hours=stall_hours)
        # Old reports are history; only reports after this point raise events.
        report = self._latest_report(task.workspace_id, task.id)
        item = self.store.upsert_work(
            task.id,
            workspace_id=task.workspace_id,
            title=task.title,
            agent_type=task.agent_type.value,
            status=status,
            watched=None if delegated else True,
            watched_at=None if delegated else now_iso(),
            closed=False,
            stopped=False,
            last_report_id=getattr(report, "id", None),
            stall_hours=stall_hours,
        )
        await asyncio.to_thread(self.store.commit, f"dot: watch {task.id}")
        return item

    async def unwatch(self, task_id: str) -> Optional[dict[str, Any]]:
        """Stop tracking a task (watched or delegated). Returns None when unknown."""
        if self.store.get_work(task_id) is None:
            return None
        item = self.store.upsert_work(task_id, closed=True, stopped=True, review_due=False)
        await asyncio.to_thread(self.store.commit, f"dot: unwatch {task_id}")
        return item

    # ── watcher ─────────────────────────────────────────────────────────

    def start(self) -> None:
        tab_id = self.tab_id()
        if tab_id:
            # Bring standing schedules up to date (e.g. drop an old daily wake-up).
            try:
                self.ensure_schedules(tab_id)
            except Exception:
                logger.exception("dot schedule refresh failed")
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(self._watch_loop(), name="dot-watch")

    async def stop(self) -> None:
        task, self._watch_task = self._watch_task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _watch_loop(self) -> None:
        while True:
            try:
                if self.is_set_up() and (await self.check_work() or self._wake_pending):
                    # A failed or skipped wake retries next tick while events are unread.
                    self._wake_pending = not await self.wake() and bool(self.store.list_inbox())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("dot work watcher failed")
            await asyncio.sleep(WATCH_INTERVAL_SECONDS)

    def _reports(self, workspace_id: str, task_id: str) -> list[Any]:
        from .. import workspace_manager

        try:
            return list(workspace_manager.reports_for_task(workspace_id, task_id))
        except KeyError:
            return []

    def _latest_report(self, workspace_id: str, task_id: str) -> Any:
        reports = self._reports(workspace_id, task_id)
        return reports[-1] if reports else None

    @staticmethod
    def _last_activity(item: dict[str, Any], task: Any, report: Any) -> Optional[datetime]:
        """Newest sign of life: task update, progress, runtime change, report, or watch start."""
        moments = (
            getattr(task, "updated_at", None),
            getattr(getattr(task, "latest_progress", None), "reported_at", None),
            getattr(getattr(task, "runtime_observation", None), "observed_at", None),
            getattr(report, "created_at", None),
            item.get("watched_at") or item.get("created"),
        )
        parsed = [moment for moment in map(_parse_time, moments) if moment is not None]
        return max(parsed, default=None)

    def _result_card(self, item: dict[str, Any], status: str, report: Any) -> Card:
        message = _clip_text(str(getattr(report, "message", "") or "(no report)"))
        lines = [f"Task `{item['task_id']}` in workspace `{item.get('workspace_id')}`: {status}."]
        if item.get("goal"):
            lines += ["", f"Goal: {item['goal']}"]
        if item.get("output"):
            lines.append(f"Output: {item['output']}")
        checks = _checklist(item.get("done_when"))
        if checks:
            # Next to the report, so each check can be held against the worker's answer.
            lines += [
                "",
                "## Done when",
                *[f"{number}. {check}" for number, check in enumerate(checks, 1)],
            ]
        lines += ["", "## Report", message]
        for label, attr in (("Validation", "validation"), ("Risks", "risks")):
            value = getattr(report, attr, None)
            if value:
                lines += ["", f"## {label}", _clip_text(str(value))]
        changed = list(getattr(report, "changed_files", []) or [])
        artifacts = list(getattr(report, "artifact_refs", []) or [])
        if changed:
            lines += ["", "## Changed files", *[f"- {path}" for path in changed[:50]]]
        if artifacts:
            lines += ["", "## Artifacts", *[f"- {ref}" for ref in artifacts[:50]]]
        card = Card(
            id=str(item.get("result_card") or ""),
            kind="work",
            title=f"Result: {item.get('title') or item['task_id']}",
            summary=f"[{status}] {_first_line(message)}",
            body="\n".join(lines),
            tags=["result", status],
            importance=3,
            source=f"task:{item['task_id']}",
        )
        return self.store.save_card(card)

    async def check_work(self) -> int:
        """Turn tracked task transitions into inbox events and result cards."""
        from .. import workspace_manager

        events = 0
        for item in self.store.list_work(include_closed=True):
            try:
                events += self._check_item(workspace_manager, item)
            except Exception:
                # One bad task or report must not stop the others from being seen.
                logger.exception("dot: checking task %s failed", item.get("task_id"))
        if events:
            await asyncio.to_thread(self.store.commit, f"dot: {events} task event(s)")
        return events

    def _check_item(self, workspace_manager: Any, item: dict[str, Any]) -> int:
        """Look at one tracked task; returns how many inbox events it raised."""
        task_id = str(item.get("task_id") or "")
        task = workspace_manager.tasks.get(task_id)
        # A re-run that ends the same way between two polls only shows as a new end time.
        ended = _ended_at(task)
        rerun = (
            task is not None
            and ended is not None
            and task.status.value == item.get("status")
            and "ended_at" in item
            and ended != item["ended_at"]
        )
        if item.get("closed"):
            if ended and "ended_at" not in item and not item.get("stopped"):
                # Closed before end times were kept: remember it, no event.
                self.store.upsert_work(task_id, ended_at=ended)
            # A closed task is looked at again only when it was restarted or re-run.
            if (
                item.get("stopped")
                or task is None
                or (task.status.value == item.get("status") and not rerun)
            ):
                return 0
        if task is None:
            self.store.add_inbox(
                "task_missing",
                f"{item.get('title') or task_id} — task no longer exists",
                task_id=task_id,
                key=f"task_missing:{task_id}",
            )
            self.store.upsert_work(task_id, status="missing", closed=True)
            return 1
        status = task.status.value
        previous = item.get("status")
        reports = self._reports(str(item.get("workspace_id") or ""), task_id)
        report = reports[-1] if reports else None
        report_id = getattr(report, "id", None)
        seen_ids = [getattr(old, "id", None) for old in reports]
        last_seen = item.get("last_report_id")
        new_reports = reports[seen_ids.index(last_seen) + 1 :] if last_seen in seen_ids else reports
        # The Hub's own review requests carry no result, question or round.
        result = next((old for old in reversed(reports) if not _hub_report(old)), report)
        fresh = [new for new in new_reports if not _hub_report(new)]
        latest = fresh[-1] if fresh else None
        changes: dict[str, Any] = {}
        notices: list[tuple[str, str, Optional[str]]] = []
        if status != previous:
            changes["status"] = status
        # Any question since the last check, even one answered before this poll, counts
        # against first pass (only until the first verdict).
        if not item.get("first_verdict") and any(
            _report_state(new) in _QUESTION_REPORT_STATES for new in fresh
        ):
            changes["asked"] = True
        # Dot judges what it delegated; a watched task's brief was not Dot's.
        review_due = None if item.get("watched") else True
        # A new final report in review/failed is a new round, even when the poll never
        # saw the task working again; before a verdict it refreshes the result card.
        again = status == previous and _report_state(latest) in _RETURN_REPORT_STATES
        title = item.get("title") or task_id
        if status in ("review", "failed") and (status != previous or again or rerun):
            card = self._result_card(item, status, result)
            changes["result_card"] = card.id
            changes["review_due"] = review_due
            summary = _first_line(str(getattr(result, "message", "") or ""), 300)
            kind = "task_ready" if status == "review" else "task_failed"
            notices.append((kind, f"{title} — {summary or status}", card.id))
        elif status == "done" and (previous not in ("review", "done") or rerun):
            card = self._result_card(item, status, result)
            changes["result_card"] = card.id
            changes["review_due"] = review_due
            notices.append(("task_done", f"{title} — done", card.id))
        elif (
            _report_state(latest) in _ATTENTION_REPORT_STATES
            and status not in _TERMINAL_TASK_STATES
        ):
            question = _first_line(str(getattr(latest, "message", "") or ""), 300)
            notices.append(("task_needs_input", f"{title} — {question}", None))
        elif status in _STALL_TASK_STATES and (activity := self._last_activity(item, task, report)):
            idle = utc_now() - activity
            limit = float(item.get("stall_hours") or DEFAULT_STALL_HOURS)
            # One alarm per stall: a new sign of life re-arms it.
            if idle > timedelta(hours=limit) and item.get("stall_alarm_for") != (
                marker := activity.isoformat()
            ):
                notices.append(
                    (
                        "task_stalled",
                        f"{title} — no progress for "
                        f"{idle.total_seconds() / 3600:.1f}h (status {status})",
                        None,
                    )
                )
                changes["stall_alarm_for"] = marker
        if report_id and report_id != last_seen:
            changes["last_report_id"] = report_id
        if status in _TERMINAL_TASK_STATES:
            changes["closed"] = True
            if ended and ended != item.get("ended_at"):
                changes["ended_at"] = ended
        elif item.get("closed"):
            changes["closed"] = False
        # Events go first, so a failed write cannot lose one. The next tick sees the same
        # change again; its key makes that the same unread event, not a second one.
        key = ":".join(
            str(part or "")
            for part in (task_id, status, report_id, ended, changes.get("stall_alarm_for"))
        )
        for kind, text, card_id in notices:
            extra = {"card_id": card_id} if card_id else {}
            self.store.add_inbox(kind, text, task_id=task_id, key=f"{kind}:{key}", **extra)
        if changes:
            self.store.upsert_work(task_id, **changes)
        return len(notices)

    # ── name and avatar ─────────────────────────────────────────────────

    async def update_identity(
        self,
        *,
        name: Optional[str] = None,
        avatar: Optional[str] = None,
        avatar_image: Optional[str] = None,
    ) -> dict[str, Any]:
        """Change Dot's name/avatar; a new name renames the tab and starts a fresh session."""
        from .. import ttyd_manager

        before = self.store.read_identity()
        identity = self.store.update_identity(name=name, avatar=avatar, avatar_image=avatar_image)
        if identity["name"] != before["name"]:
            self.request_rotation()
            tab_id = self.tab_id()
            if tab_id:
                ttyd_manager.rename_tab(tab_id, identity["name"])
        await asyncio.to_thread(self.store.commit, "dot: identity")
        return identity

    async def wake(self) -> bool:
        """Queue one wake-up turn in the Dot Chat (coalesced by the scheduler)."""
        from .. import ttyd_manager, workspace_manager

        schedule_id = self.store.read_state().get("wake_schedule_id")
        tab_id = self.tab_id()
        if schedule_id not in workspace_manager.scheduled_tasks and tab_id:
            # A deleted schedule would leave Dot asleep until the next restart.
            if ttyd_manager.get_tab(tab_id) is not None:
                try:
                    self.ensure_schedules(tab_id)
                except Exception:
                    logger.exception("dot: restoring the schedules failed")
                schedule_id = self.store.read_state().get("wake_schedule_id")
        if not schedule_id or schedule_id not in workspace_manager.scheduled_tasks:
            return False
        try:
            await workspace_manager.run_scheduled_task(schedule_id)
        except Exception as exc:
            logger.warning("dot wake-up failed: %s", exc)
            return False
        return True

    # ── health ──────────────────────────────────────────────────────────

    def health(self) -> dict[str, Any]:
        """Deterministic memory checks. No model call."""
        from .. import ttyd_manager

        self.store.ensure()
        state = self.store.read_state()
        now = utc_now()
        checks: list[dict[str, Any]] = []

        def check(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": ok, "detail": detail})

        tab_id = state.get("tab_id")
        check(
            "tab",
            bool(tab_id and ttyd_manager.get_tab(tab_id)),
            f"Dot tab {tab_id or 'not set up'}",
        )
        focus = self.store.list_focus()
        check(
            "focus_size",
            1 <= len(focus) <= FOCUS_LIMIT,
            f"{len(focus)} active focus item(s) (want 1-{FOCUS_LIMIT})",
        )
        stale = [
            item["title"]
            for item in focus
            if (updated := _parse_time(item.get("updated"))) and now - updated > timedelta(days=14)
        ]
        check("focus_fresh", not stale, f"stale > 14 days: {', '.join(stale) or 'none'}")
        long_running = [
            item.get("title", item.get("task_id"))
            for item in self.store.list_work()
            if (created := _parse_time(item.get("created"))) and now - created > timedelta(days=3)
        ]
        check(
            "work_moving",
            not long_running,
            f"open > 3 days: {', '.join(map(str, long_running)) or 'none'}",
        )
        unread = len(self.store.list_inbox())
        check("inbox", unread <= 5, f"{unread} unread event(s)")
        pending = len(self.store.pending_reviews())
        check("reviews", pending <= 3, f"{pending} returned task(s) waiting for a verdict")
        cards = self.store.list_cards()
        missing = [card.id for card in cards if not card.summary.strip()]
        check("summaries", not missing, f"{len(missing)} card(s) without a summary")
        titles: dict[str, int] = {}
        for card in cards:
            key = card.title.casefold().strip()
            titles[key] = titles.get(key, 0) + 1
        duplicates = sorted(title for title, count in titles.items() if count > 1)
        check("duplicates", not duplicates, f"duplicate titles: {', '.join(duplicates) or 'none'}")
        indexed = self.index.count()
        on_disk = len(list(self.store.iter_card_paths()))
        check("index", indexed == on_disk, f"{indexed} indexed / {on_disk} card file(s)")
        pack = build_session_pack(self.store)
        from .context import SESSION_PACK_CHARS

        check(
            "session_pack",
            len(pack) < SESSION_PACK_CHARS,
            f"{len(pack)} / {SESSION_PACK_CHARS} chars (full means profile is truncated)",
        )
        last_tidy = _parse_time(state.get("last_tidy_at"))
        check(
            "tidy",
            last_tidy is not None and now - last_tidy < timedelta(days=2),
            f"last tidy: {state.get('last_tidy_at') or 'never'}",
        )
        check("git", (self.store.home / ".git").exists(), "memory history in git")
        return {
            "ok": all(item["ok"] for item in checks),
            "checks": checks,
            "cards": len(cards),
        }

    # ── status ──────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        from .. import ttyd_manager

        state = self._peek_state()
        tab_id = state.get("tab_id")
        set_up = self.store.home.exists() and bool(tab_id)
        counts: dict[str, int] = {}
        if self.store.home.exists():
            for card in self.store.list_cards():
                counts[card.kind] = counts.get(card.kind, 0) + 1
        return {
            "set_up": set_up,
            "identity": self.store.read_identity(),
            "home": str(self.store.home),
            "tab_id": tab_id,
            "tab_exists": bool(tab_id and ttyd_manager.get_tab(tab_id)),
            "session": {
                "conversation_id": state.get("session_conversation_id"),
                "turns": state.get("session_turns", 0),
                "started_at": state.get("session_started_at"),
                "max_turns": MAX_SESSION_TURNS,
            },
            "cards": counts,
            "focus": len(self.store.list_focus()) if set_up else 0,
            "open_work": len(self.store.list_work()) if set_up else 0,
            "unread_events": len(self.store.list_inbox()) if set_up else 0,
            "pending_reviews": len(self.store.pending_reviews()) if set_up else 0,
            "wake_schedule_id": state.get("wake_schedule_id"),
            "tidy_schedule_id": state.get("tidy_schedule_id"),
            "last_tidy_at": state.get("last_tidy_at"),
        }


_service: Optional[DotService] = None


def get_dot_service() -> DotService:
    global _service
    if _service is None:
        _service = DotService()
    return _service


def reset_dot_service(service: Optional[DotService] = None) -> None:
    """Test hook: replace the process-wide service."""
    global _service
    _service = service
