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
from .prompts import BRIEF_TEMPLATE, TIDY_MESSAGE, WAKE_MESSAGE
from .store import FOCUS_LIMIT, Card, DotStore, now_iso, utc_now

logger = logging.getLogger(__name__)

TAB_NAME = "Dot"
TAB_STREAM_WORKSPACE_ID = "terminal-tabs"
TAB_STREAM_SESSION_PREFIX = "terminal-tab-"
# Rotate the provider session before raw history piles up.
MAX_SESSION_TURNS = 24
MAX_SESSION_AGE = timedelta(hours=18)
WAKE_CRON = "3 9 * * *"
TIDY_CRON = "17 3 * * *"
WATCH_INTERVAL_SECONDS = 15.0
_TERMINAL_TASK_STATES = {"done", "failed"}
_ATTENTION_REPORT_STATES = {"needs_input", "blocked"}
_JOURNAL_TEXT_LIMIT = 20_000


@dataclass
class TurnPlan:
    """What the provider transport must apply before sending one Dot turn."""

    rotate: bool
    conversation_id: Optional[str]
    system_pack: str
    turn_pack: str


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
        self._watch_task: Optional[asyncio.Task[None]] = None
        self._state_cache: tuple[float, dict[str, Any]] = (-1.0, {})

    # ── identity ────────────────────────────────────────────────────────

    def _peek_state(self) -> dict[str, Any]:
        """Read state without creating the home (cheap; called on every Chat turn)."""
        path = self.store.home / "state.json"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
        cached_mtime, cached = self._state_cache
        if cached_mtime == mtime:
            return cached
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        state = value if isinstance(value, dict) else {}
        self._state_cache = (mtime, state)
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
        from ...models.schemas import AgentType, SessionKind
        from .. import ttyd_manager

        self.store.ensure()
        tab_id = self.tab_id()
        tab = ttyd_manager.get_tab(tab_id) if tab_id else None
        if tab is not None and getattr(tab, "archived", False):
            await ttyd_manager.unarchive_tab(tab.id)
        if tab is None:
            created = await ttyd_manager.create_tab(
                name=TAB_NAME,
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
        for key, name, cron, message in (
            ("wake_schedule_id", "Dot wake-up", WAKE_CRON, WAKE_MESSAGE),
            ("tidy_schedule_id", "Dot nightly tidy", TIDY_CRON, TIDY_MESSAGE),
        ):
            existing = workspace_manager.scheduled_tasks.get(state.get(key) or "")
            if existing is not None:
                if existing.tab_id != tab_id:
                    workspace_manager.update_scheduled_task(
                        existing.id, ScheduledTaskUpdate(tab_id=tab_id)
                    )
                continue
            created = workspace_manager.create_scheduled_task(
                ScheduledTaskCreate(
                    name=name,
                    kind=ScheduledTaskKind.CHAT_TURN,
                    cron=cron,
                    tab_id=tab_id,
                    message=message,
                )
            )
            changes[key] = created.id
        if changes:
            self.store.update_state(**changes)

    # ── turns ───────────────────────────────────────────────────────────

    def prepare_turn(self, text: str, conversation_id: Optional[str]) -> TurnPlan:
        """Decide rotation and build both packs for one Dot turn."""
        self.store.ensure()
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
        if pack.delivered_event_ids:
            self.store.mark_inbox_read(pack.delivered_event_ids)
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
        )

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
                        if asked.strip():
                            who = "hub" if asked.lstrip().startswith("[Dot ") else "you"
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
                        last_completed = event.stream_sequence
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
        done_when: str,
        context: str = "",
        constraints: str = "",
        agent_type: str = "codex",
        task_mode: Optional[str] = None,
        start: bool = True,
        request_key: Optional[str] = None,
        actor_key: str,
    ) -> dict[str, Any]:
        """Register (and start) one Hub Task with a Dot brief; track it in work.json."""
        from ...models.schemas import (
            AgentType,
            StartTaskRequest,
            TaskSourceReference,
            WorkspaceTaskCreate,
            WorkspaceTaskMode,
        )
        from .. import workspace_manager

        if not title.strip() or not goal.strip() or not done_when.strip():
            raise ValueError("title, goal and done_when are required")
        self.store.ensure()
        brief = BRIEF_TEMPLATE.format(
            goal=goal.strip(),
            context=context.strip() or "(none given)",
            done_when=done_when.strip(),
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
            done_when=done_when.strip(),
            agent_type=agent_type,
            status=task.status.value,
        )
        await asyncio.to_thread(self.store.commit, f"dot: delegate {task.id}")
        return item

    # ── watcher ─────────────────────────────────────────────────────────

    def start(self) -> None:
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
                if self.is_set_up() and await self.check_work():
                    await self.wake()
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

    def _result_card(self, item: dict[str, Any], status: str, report: Any) -> Card:
        message = str(getattr(report, "message", "") or "(no report)")
        lines = [f"Task `{item['task_id']}` in workspace `{item.get('workspace_id')}`: {status}."]
        if item.get("goal"):
            lines += ["", f"Goal: {item['goal']}"]
        if item.get("done_when"):
            lines.append(f"Done when: {item['done_when']}")
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
            if status in ("review", "failed") and status != previous:
                card = self._result_card(item, status, report)
                changes["result_card"] = card.id
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
