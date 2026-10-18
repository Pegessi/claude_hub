"""Dot tab ownership, turn preparation, and journal synchronization."""

from datetime import datetime

from claude_hub.services.dot.prompts import RETIRED_TIDY_MESSAGES
from claude_hub.services.dot.runtime.common import (
    _JOURNAL_TEXT_LIMIT,
    _SENT_TURNS_KEPT,
    MAX_SESSION_AGE,
    MAX_SESSION_TURNS,
    NOTIFY_REPEAT_SECONDS,
    QUIET_REPLY,
    RETIRED_WAKE_MESSAGES,
    TAB_STREAM_SESSION_PREFIX,
    TAB_STREAM_WORKSPACE_ID,
    TIDY_CRON,
    TIDY_MESSAGE,
    WAKE_INTERVAL_SECONDS,
    WAKE_MESSAGE,
    WATCH_INTERVAL_SECONDS,
    YIELD_PAUSE_SECONDS,
    Any,
    DotIndex,
    DotStore,
    Optional,
    TurnPack,
    TurnPlan,
    _parse_time,
    _sent_turns,
    asyncio,
    build_session_pack,
    build_turn_pack,
    datetime,
    json,
    logger,
    now_iso,
    reach_note,
    time,
    utc_now,
    uuid,
)


class DotSessionService(object):
    """One Dot per Hub runtime. Every method is safe to call when Dot is not set up."""

    def __init__(self, store: Optional[DotStore] = None) -> None:
        self.store = store or DotStore()
        self.index = DotIndex(self.store)
        self._journal_lock: Optional[asyncio.Lock] = None
        self._setup_lock: Optional[asyncio.Lock] = None
        self._wake_pending = False
        self._yielded_at = float("-inf")
        self._woke_at = float("-inf")
        self._notified: dict[str, float] = {}
        self._watch_task: Optional[asyncio.Task[None]] = None
        self._state_cache: tuple[tuple[int, int, int], dict[str, Any]] = ((-1, -1, -1), {})

    def _utc_now(self) -> datetime:
        return utc_now()

    def _watch_interval_seconds(self) -> float:
        return WATCH_INTERVAL_SECONDS

    def _wake_interval_seconds(self) -> int:
        return WAKE_INTERVAL_SECONDS

    def _yield_pause_seconds(self) -> float:
        return YIELD_PAUSE_SECONDS

    def _notify_repeat_seconds(self) -> float:
        return NOTIFY_REPEAT_SECONDS

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
        except (OSError, ValueError, RecursionError):
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

    def is_system_schedule(self, schedule_id: Optional[str]) -> bool:
        """Dot's own wake-up or tidy schedule: its turns give way to the user."""
        state = self._peek_state()
        return bool(schedule_id) and schedule_id in (
            state.get("wake_schedule_id"),
            state.get("tidy_schedule_id"),
        )

    def is_system_turn(self, turn_id: Optional[str]) -> bool:
        """The Chat turn is a run of Dot's wake-up or tidy schedule."""
        from ... import workspace_manager

        if not turn_id:
            return False
        return any(
            run.client_turn_id == turn_id and self.is_system_schedule(run.scheduled_task_id)
            for run in list(workspace_manager.scheduled_task_runs.values())
        )

    def drop_queued_system_runs(self) -> None:
        """Queued wake-up or tidy runs would take the Chat when a stopped turn ends.

        The user's message goes first; the wake-up is asked for again by ``yield_turn``.
        """
        from ... import workspace_manager

        state = self._peek_state()
        dropped = 0
        for key in ("wake_schedule_id", "tidy_schedule_id"):
            if state.get(key):
                dropped += workspace_manager.drop_pending_scheduled_task_runs(
                    state[key], "gave way to the user"
                )
        if dropped:
            # Even when the running turn ends before the stop lands.
            self._wake_pending = True
            self._yielded_at = time.monotonic()

    def yield_turn(self) -> None:
        """A wake-up or tidy turn was stopped for the user: it did not happen.

        Its events are unread again and wait for the next wake-up (asked for
        here). Its end may not be synced yet; the turn's record forgets them, so
        that end cannot give them back a second time after a later turn read them.
        """
        self._wake_pending = True
        self._yielded_at = time.monotonic()
        with self.store.lock:
            sent = _sent_turns(self.store.read_state())
            turn = sent[-1] if sent else {}
            if not turn.get("hub"):
                return
            ids = [str(one) for one in turn.get("ids") or []] if turn.get("acked") else []
            turn["ids"] = []
            self.store.update_state(sent_turns=sent)
        if ids:
            self.store.mark_inbox_unread(ids)

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
        from ....models.schemas import AgentType, SessionKind
        from ... import ttyd_manager

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
            await asyncio.to_thread(self.store.commit, "dot: new chat tab")
        assert tab_id is not None
        self.ensure_schedules(tab_id)
        return tab_id

    def env_seed_needed(self, env: Optional[dict[str, str]]) -> bool:
        """True when the Dot tab still runs on the built-in fallback env.

        Such a tab has no working provider yet, so setup may give it ``env``;
        a tab whose env the user chose keeps it.
        """
        from ... import ttyd_manager
        from ...ttyd_manager import DEFAULT_CLAUDE_LAUNCH_ENV

        tab_id = self.tab_id()
        tab = ttyd_manager.get_tab(tab_id) if tab_id and env else None
        if tab is None:
            return False
        current = dict(getattr(tab, "env", None) or {})
        return current in ({}, DEFAULT_CLAUDE_LAUNCH_ENV) and current != dict(env or {})

    def ensure_schedules(self, tab_id: str) -> None:
        """Keep the wake-up and nightly tidy Chat schedules pointing at the Dot tab."""
        from ....models.schemas import (
            ScheduledTaskCreate,
            ScheduledTaskKind,
            ScheduledTaskUpdate,
        )
        from ... import workspace_manager

        state = self.store.read_state()
        timings: tuple[tuple[str, str, dict[str, Any], str, tuple[str, ...]], ...] = (
            (
                "wake_schedule_id",
                "Dot wake-up",
                {"interval_seconds": self._wake_interval_seconds()},
                WAKE_MESSAGE,
                RETIRED_WAKE_MESSAGES,
            ),
            (
                "tidy_schedule_id",
                "Dot nightly tidy",
                {"cron": TIDY_CRON},
                TIDY_MESSAGE,
                RETIRED_TIDY_MESSAGES,
            ),
        )
        for key, name, timing, message, retired_messages in timings:
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
                if existing.message in retired_messages:
                    update["message"] = message
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

    def prepare_turn(self, text: str, conversation_id: Optional[str]) -> TurnPlan:
        """Decide rotation and build both packs for one Dot turn."""
        self.store.ensure()
        # Read-decide-write under the store lock so a concurrent rotation
        # request is not overwritten.
        # A cold index can take seconds to fill; not while holding the lock every
        # Dot route (on the event loop) waits for.
        try:
            self.index.sync()
        except Exception:
            logger.warning("dot: index sync failed", exc_info=True)
        feishu_bound = self._feishu_bound()
        with self.store.lock:
            return self._prepare_turn(text, conversation_id, feishu_bound)

    def _feishu_bound(self) -> bool:
        from ....api.feishu_bot import feishu_bound_to_tab

        tab_id = self.tab_id()
        try:
            return bool(tab_id) and feishu_bound_to_tab(str(tab_id))
        except Exception:
            logger.warning("dot: reading the Feishu binding failed", exc_info=True)
            return False

    def _prepare_turn(
        self, text: str, conversation_id: Optional[str], feishu_bound: bool = False
    ) -> TurnPlan:
        from ...agent_stream.turn_source import is_feishu_provider_text

        state = self.store.read_state()
        now = self._utc_now()
        stripped = text.lstrip()
        hub = stripped.startswith("[Dot ")
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
            recall=not hub,
            # A tidy turn has other work: events wait for a wake-up.
            inbox=not tidy,
            wake=hub,
            reach=reach_note(
                (
                    ("tidy" if tidy else "wake")
                    if hub
                    else "feishu" if is_feishu_provider_text(stripped) else "web"
                ),
                feishu_bound,
            ),
        )
        # Only a wake-up handles events. The user's turn shows them, but the user
        # comes first: they stay unread for the wake-up that follows.
        delivered = pack.delivered_event_ids if hub else []
        # Remembered until the turn ends: a failed turn gives its events and its
        # first-turn context back (see ``_turn_ended``).
        sent: dict[str, Any] = {
            "at": self._utc_now().isoformat(),
            "ids": delivered,
            "first": new_session,
        }
        if hub:
            sent["hub"] = True
        if tidy:
            # Counted as a tidy only when the turn ends well (see ``_turn_ended``).
            sent["tidy"] = True
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
        self.store.update_state(**updates)
        return TurnPlan(
            rotate=rotate,
            conversation_id=conversation_id,
            system_pack=system_pack,
            turn_pack=pack.text,
            delivered_event_ids=delivered,
        )

    def _turn_ended(
        self, started_at: Any, until: Any, ended_at: Any, failed: bool, cancelled: bool = False
    ) -> None:
        """Match a finished turn to the turn Dot prepared for it.

        A turn is prepared after it starts and before the next one starts
        (``until``). A turn the provider never accepted ends with its start time,
        so the end time alone cannot place it. Otherwise the latest turn prepared
        before the end is taken.

        A failed turn never reached Dot: its events are unread again, and when it
        opened a session the next turn opens a fresh one (with the recent talk).
        A stopped turn may not have handled its events either: they are unread
        again too, and a stopped tidy is no tidy.
        """
        started, bound, ended = _parse_time(started_at), _parse_time(until), _parse_time(ended_at)
        if ended is None:
            return
        with self.store.lock:
            state = self.store.read_state()
            sent = _sent_turns(state)
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
            # A failed tidy is no tidy; two tidies may end out of order.
            tidied, last = _parse_time(turn.get("at")), _parse_time(state.get("last_tidy_at"))
            finished = not failed and not cancelled
            if turn.get("tidy") and finished and tidied and (last is None or tidied > last):
                changes["last_tidy_at"] = turn["at"]
            self.store.update_state(**changes)
        # Only a turn the provider took marked its events read; a later turn may have
        # shown and read them since a turn that was never taken.
        ids = [str(one) for one in turn.get("ids") or []] if turn.get("acked") else []
        if not finished and ids:
            self.store.mark_inbox_unread(ids)
            # The watcher wakes Dot again for them, after a pause: a failing
            # provider must not be retried every minute.
            self._yielded_at = time.monotonic()

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

    async def notify(self, text: str) -> str:
        """Dot chose to reach the user now: send ``text`` to the Feishu chat bound
        to the Dot tab. ``sent``; ``repeat`` when the same text was sent a short
        while ago (not sent again); ``failed`` when no bot is bound there or the
        send failed."""
        from ....api.feishu_bot import push_to_tab
        from ...agent_stream.redaction import mask_secrets

        tab_id = self.tab_id()
        if not tab_id:
            return "failed"
        text = mask_secrets(text.strip())
        now = time.monotonic()
        self._notified = {
            one: at
            for one, at in self._notified.items()
            if now - at < self._notify_repeat_seconds()
        }
        if text in self._notified:
            return "repeat"
        if not await push_to_tab(tab_id, text):
            return "failed"
        self._notified[text] = now
        return "sent"

    async def on_stream_event(self, event: Any) -> None:
        """Post-persist observer: sync the journal when a Dot turn completes."""
        from ....models.agent_stream import AgentStreamEventType

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
        from ....models.agent_stream import AgentStreamEventType
        from ...agent_stream.store import AgentStreamStore

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
            ended: list[tuple[Any, Any, bool, bool]] = []
            added = 0
            last_completed = cursor
            while True:
                read_at = self._utc_now()
                page = await stream.read_since(cursor, limit=500)
                for event in page.events:
                    payload = event.payload if isinstance(event.payload, dict) else {}
                    if event.type == AgentStreamEventType.TURN_STARTED and event.turn_id:
                        starts[event.turn_id] = (
                            str(payload.get("summary") or ""),
                            event.created_at,
                        )
                        for turn in ended:
                            self._turn_ended(turn[0], event.created_at, *turn[1:])
                        ended.clear()
                    elif event.type == AgentStreamEventType.TURN_COMPLETED:
                        asked, asked_at = starts.pop(event.turn_id or "", ("", None))
                        answer = str(payload.get("assistant_text") or "")
                        last_completed = event.stream_sequence
                        status = payload.get("status")
                        ended.append(
                            (asked_at, event.created_at, status == "failed", status == "cancelled")
                        )
                        from_hub = asked.lstrip().startswith("[Dot ")
                        if from_hub and (answer.strip() == QUIET_REPLY or status == "cancelled"):
                            # Nothing needed the user, or the turn gave way to the user
                            # and runs again later; keep it out of the recent talk.
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
                self._turn_ended(turn[0], read_at, *turn[1:])
            if last_completed != int(self.store.read_state().get("journal_seq", -1)):
                self.store.update_state(journal_seq=last_completed)
            if added:
                await asyncio.to_thread(self.store.commit, f"dot: journal +{added} turn(s)")
            return added
