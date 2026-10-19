"""Dot identity, wake-up, health, and status surfaces."""

from claude_hub.services.dot.runtime.common import (
    FOCUS_LIMIT,
    MAX_SESSION_TURNS,
    Any,
    Optional,
    _parse_time,
    asyncio,
    build_session_pack,
    logger,
    timedelta,
)
from claude_hub.services.dot.runtime.work import DotWorkService


class DotService(DotWorkService):
    async def update_identity(
        self,
        *,
        name: Optional[str] = None,
        avatar: Optional[str] = None,
        avatar_image: Optional[str] = None,
    ) -> dict[str, Any]:
        """Change Dot's name/avatar; a new name renames the tab and starts a fresh session."""
        from ... import ttyd_manager

        async with self.setup_lock():
            before = self.store.read_identity()
            identity = self.store.update_identity(
                name=name, avatar=avatar, avatar_image=avatar_image
            )
            if identity["name"] != before["name"]:
                self.request_rotation()
                tab_id = self.tab_id()
                if tab_id:
                    ttyd_manager.rename_tab(tab_id, identity["name"])
            await asyncio.to_thread(self.store.commit, "dot: identity")
            return identity

    async def wake(self) -> bool:
        """Queue one wake-up turn in the Dot Chat (coalesced by the scheduler)."""
        from ... import ttyd_manager, workspace_manager

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

    def health(self) -> dict[str, Any]:
        """Deterministic memory checks. No model call."""
        from ... import ttyd_manager

        self.store.ensure()
        state = self.store.read_state()
        now = self._utc_now()
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
        # A note dropped into a card folder (no card header) is not a card to index.
        on_disk = len(self.store.list_cards(include_archived=True))
        stray = len(list(self.store.iter_card_paths())) - on_disk
        check(
            "index",
            indexed == on_disk,
            f"{indexed} indexed / {on_disk} card file(s)"
            + (f"; {stray} .md file(s) without a card header" if stray else ""),
        )
        pack = build_session_pack(self.store)
        from ..context import SESSION_PACK_CHARS, session_pack_cuts

        cuts = session_pack_cuts(self.store)
        check(
            "session_pack",
            len(pack) < SESSION_PACK_CHARS and not cuts,
            f"{len(pack)} / {SESSION_PACK_CHARS} chars"
            + (f"; cut to fit: {', '.join(cuts)}" if cuts else ""),
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

    def status(self) -> dict[str, Any]:
        from ... import ttyd_manager

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
