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
from .lessons import lesson_score, lint_brief, match_lessons, session_rules
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
_ATTENTION_REPORT_STATES = {"needs_input", "blocked"}
_JOURNAL_TEXT_LIMIT = 20_000
REVIEW_VERDICTS = ("ok", "redo")
REVIEW_CAUSES = ("brief", "env", "worker")
# First-pass rate covers the latest reviews only, so it shows the current trend.
STATS_WINDOW = 20


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
        changes: dict[str, Any] = {}
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
                    update["tab_id"] = tab_id
                # Older homes woke Dot every morning; move them to events only.
                if any(getattr(existing, field, None) != value for field, value in timing.items()):
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
            changes[key] = created.id
        if changes:
            self.store.update_state(**changes)

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
            recall=not tidy,
        )
        updates: dict[str, Any] = {
            "session_conversation_id": conversation_id,
            "session_turns": turns,
            "session_pack": system_pack,
            # A tidy runs in its own session; the next real turn starts clean.
            "rotate_requested": True if tidy else None,
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

    def mark_delivered(self, event_ids: list[str]) -> None:
        """The provider accepted a turn: its shown inbox events are now read."""
        if event_ids:
            self.store.mark_inbox_read(event_ids)

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
            added = 0
            last_completed = cursor
            while True:
                page = await stream.read_since(cursor, limit=500)
                for event in page.events:
                    payload = event.payload if isinstance(event.payload, dict) else {}
                    if event.type == AgentStreamEventType.TURN_STARTED and event.turn_id:
                        starts[event.turn_id] = (
                            str(payload.get("summary") or ""),
                            event.created_at,
                        )
                    elif event.type == AgentStreamEventType.TURN_COMPLETED:
                        asked, asked_at = starts.pop(event.turn_id or "", ("", None))
                        answer = str(payload.get("assistant_text") or "")
                        last_completed = event.stream_sequence
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

        Returns ``held`` (nothing sent), the brief ``warnings``, the matching
        ``lessons`` and the tracked ``work`` item. A brief with warnings or
        matching lessons is held until Dot fixes it or sends ``confirm``.
        """
        from ...models.schemas import (
            AgentType,
            StartTaskRequest,
            TaskSourceReference,
            WorkspaceTaskCreate,
            WorkspaceTaskMode,
        )
        from .. import workspace_manager

        checks = [" ".join(item.split()) for item in done_when if item.strip()]
        if not title.strip() or not goal.strip() or not checks:
            raise ValueError("title, goal and done_when are required")
        self.store.ensure()
        warnings = lint_brief(done_when=checks, context=context, output=output)
        lessons = match_lessons(
            self.store.list_lessons(),
            " ".join([title, goal, context, output, out_of_scope, constraints, *checks]),
            utc_now(),
        )
        if (warnings or lessons) and not confirm:
            return {"held": True, "warnings": warnings, "lessons": lessons, "work": None}
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
        if start and task.status.value == "todo":
            task = await workspace_manager.start_task(
                task.id, StartTaskRequest(agent_type=AgentType(agent_type))
            )
        item = self.store.upsert_work(
            task.id,
            workspace_id=workspace_id,
            title=task.title,
            goal=goal.strip(),
            done_when=checks,
            output=output.strip() or None,
            agent_type=agent_type,
            status=task.status.value,
            stall_hours=stall_hours,
            # Kept so a later review can see which warnings Dot sent past.
            brief_warnings=warnings or None,
        )
        await asyncio.to_thread(self.store.commit, f"dot: delegate {task.id}")
        return {"held": False, "warnings": warnings, "lessons": lessons, "work": item}

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

        Raises KeyError for an unknown task, ValueError for a bad combination.
        """
        item = self.store.get_work(task_id)
        if item is None:
            raise KeyError(task_id)
        if verdict not in REVIEW_VERDICTS:
            raise ValueError("verdict must be ok or redo")
        if cause is not None and cause not in REVIEW_CAUSES:
            raise ValueError("cause must be brief, env or worker")
        if verdict == "redo" and cause is None:
            raise ValueError("a redo needs a cause: brief, env or worker")
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
        saved = None
        if lesson_id:
            saved = self.store.vote_lesson(lesson_id, task_id)
            if saved is None:
                raise ValueError(f"lesson not found: {lesson_id}")
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
        # The first verdict decides first-pass success; later rounds do not rewrite it.
        if not item.get("first_verdict"):
            changes["first_verdict"] = verdict
        updated = self.store.upsert_work(task_id, **changes)
        await asyncio.to_thread(self.store.commit, f"dot: review {task_id} {verdict}")
        return {"work": updated, "lesson": saved}

    def lessons(self, *, include_dropped: bool = False) -> list[dict[str, Any]]:
        """Lessons with their current score, strongest first."""
        now = utc_now()
        lessons = self.store.list_lessons(include_dropped=include_dropped)
        in_session = {lesson.get("id") for lesson in session_rules(lessons, now)}
        rows = [
            {
                **lesson,
                "score": round(lesson_score(lesson, now), 2),
                "in_session": lesson.get("id") in in_session,
            }
            for lesson in lessons
        ]
        return sorted(rows, key=lambda row: (bool(row.get("dropped")), -row["score"]))

    def review_stats(self) -> dict[str, Any]:
        """First-pass rate over the latest reviews: ok at the first verdict, no question asked."""
        reviewed = [
            item
            for item in self.store.list_work(include_closed=True)
            if isinstance(item.get("review"), dict)
        ]
        reviewed.sort(key=lambda item: str(item["review"].get("at") or ""))
        window = reviewed[-STATS_WINDOW:]
        first_pass = sum(
            1 for item in window if item.get("first_verdict") == "ok" and not item.get("asked")
        )
        causes: dict[str, int] = {}
        for item in window:
            cause = item["review"].get("cause")
            if cause:
                causes[cause] = causes.get(cause, 0) + 1
        return {
            "reviewed": len(window),
            "first_pass": first_pass,
            "rate": round(first_pass / len(window), 2) if window else None,
            "causes": causes,
            "pending": len(self.store.pending_reviews()),
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
        # Old reports are history; only reports after this point raise events.
        report = self._latest_report(task.workspace_id, task.id)
        item = self.store.upsert_work(
            task.id,
            workspace_id=task.workspace_id,
            title=task.title,
            agent_type=task.agent_type.value,
            status=status,
            watched=True,
            watched_at=now_iso(),
            closed=False,
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

    def _latest_report(self, workspace_id: str, task_id: str) -> Any:
        from .. import workspace_manager

        try:
            reports = workspace_manager.reports_for_task(workspace_id, task_id)
        except KeyError:
            return None
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
        message = str(getattr(report, "message", "") or "(no report)")
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
                lines += ["", f"## {label}", str(value)]
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
        for item in self.store.list_work():
            task_id = str(item.get("task_id") or "")
            task = workspace_manager.tasks.get(task_id)
            if task is None:
                self.store.upsert_work(task_id, status="missing", closed=True)
                self.store.add_inbox(
                    "task_missing",
                    f"{item.get('title') or task_id} — task no longer exists",
                    task_id=task_id,
                )
                events += 1
                continue
            status = task.status.value
            previous = item.get("status")
            report = self._latest_report(str(item.get("workspace_id") or ""), task_id)
            report_id = getattr(report, "id", None)
            changes: dict[str, Any] = {}
            if status != previous:
                changes["status"] = status
            # Dot judges what it delegated; a watched task's brief was not Dot's.
            review_due = None if item.get("watched") else True
            if status in ("review", "failed") and status != previous:
                card = self._result_card(item, status, report)
                changes["result_card"] = card.id
                changes["review_due"] = review_due
                kind = "task_ready" if status == "review" else "task_failed"
                summary = _first_line(str(getattr(report, "message", "") or ""), 300)
                self.store.add_inbox(
                    kind,
                    f"{item.get('title') or task_id} — {summary or status}",
                    task_id=task_id,
                    card_id=card.id,
                )
                events += 1
            elif status == "done" and previous not in ("review", "done"):
                card = self._result_card(item, status, report)
                changes["result_card"] = card.id
                changes["review_due"] = review_due
                self.store.add_inbox(
                    "task_done",
                    f"{item.get('title') or task_id} — done",
                    task_id=task_id,
                    card_id=card.id,
                )
                events += 1
            elif (
                report is not None
                and report_id != item.get("last_report_id")
                and getattr(getattr(report, "state", None), "value", "") in _ATTENTION_REPORT_STATES
                and status not in _TERMINAL_TASK_STATES
            ):
                self.store.add_inbox(
                    "task_needs_input",
                    f"{item.get('title') or task_id} — {_first_line(str(report.message), 300)}",
                    task_id=task_id,
                )
                # Not a first-pass success any more, whatever the final verdict.
                changes["asked"] = True
                events += 1
            elif status in _STALL_TASK_STATES and (
                activity := self._last_activity(item, task, report)
            ):
                idle = utc_now() - activity
                limit = float(item.get("stall_hours") or DEFAULT_STALL_HOURS)
                # One alarm per stall: a new sign of life re-arms it.
                if idle > timedelta(hours=limit) and item.get("stall_alarm_for") != (
                    marker := activity.isoformat()
                ):
                    self.store.add_inbox(
                        "task_stalled",
                        f"{item.get('title') or task_id} — no progress for "
                        f"{idle.total_seconds() / 3600:.1f}h (status {status})",
                        task_id=task_id,
                    )
                    changes["stall_alarm_for"] = marker
                    events += 1
            if report_id and report_id != item.get("last_report_id"):
                changes["last_report_id"] = report_id
            if status in _TERMINAL_TASK_STATES:
                changes["closed"] = True
            if changes:
                self.store.upsert_work(task_id, **changes)
        if events:
            await asyncio.to_thread(self.store.commit, f"dot: {events} task event(s)")
        return events

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
        from .. import workspace_manager

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
