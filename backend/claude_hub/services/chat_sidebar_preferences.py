"""Server-owned Chat sidebar preferences.

Pinning used to live in browser ``localStorage``, which split the preference by
origin and device. This store keeps the authoritative value under the Hub
runtime home and serializes read-modify-write operations across processes.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .runtime_isolation import resolve_runtime_home

_VERSION = 1


def _clean_tab_ids(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        tab_id = item.strip()
        if not tab_id or tab_id in seen:
            continue
        seen.add(tab_id)
        result.append(tab_id)
    return result


class ChatSidebarPreferencesStore:
    """Atomic, per-user preferences stored in one runtime JSON file."""

    def __init__(self, runtime_home: Path) -> None:
        self.directory = runtime_home
        self.path = runtime_home / "chat_sidebar_preferences.json"
        self.lock_path = runtime_home / "chat_sidebar_preferences.lock"

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": _VERSION, "users": {}}
        if not isinstance(payload, dict) or not isinstance(payload.get("users"), dict):
            return {"version": _VERSION, "users": {}}
        payload["version"] = _VERSION
        return payload

    def _write(self, state: dict[str, Any]) -> None:
        fd, raw_temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.directory)
        temporary = Path(raw_temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self._read()
            yield state
            self._write(state)

    @staticmethod
    def _user_state(state: dict[str, Any], user_id: str) -> dict[str, Any]:
        users = state.setdefault("users", {})
        user_state = users.get(user_id)
        if not isinstance(user_state, dict):
            user_state = {}
            users[user_id] = user_state
        user_state["pinned_tab_ids"] = _clean_tab_ids(user_state.get("pinned_tab_ids"))
        return user_state

    def get_pinned(self, user_id: str) -> list[str]:
        # Use the same lock as mutations so a reader never races an atomic
        # replacement originating in another backend worker.
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            state = self._read()
            return list(self._user_state(state, user_id)["pinned_tab_ids"])

    def set_pinned(self, user_id: str, tab_id: str, pinned: bool) -> list[str]:
        clean_tab_id = tab_id.strip()
        if not clean_tab_id:
            return self.get_pinned(user_id)
        with self._locked() as state:
            user_state = self._user_state(state, user_id)
            # Once the user performs an authoritative server mutation, a
            # browser that has not been opened since the upgrade must never
            # resurrect its stale localStorage pins.
            user_state["legacy_browser_pins_migrated"] = True
            pinned_tab_ids = list(user_state["pinned_tab_ids"])
            if pinned and clean_tab_id not in pinned_tab_ids:
                pinned_tab_ids.append(clean_tab_id)
            elif not pinned:
                pinned_tab_ids = [value for value in pinned_tab_ids if value != clean_tab_id]
            user_state["pinned_tab_ids"] = pinned_tab_ids
            return list(pinned_tab_ids)

    def migrate_browser_pins(self, user_id: str, tab_ids: Sequence[str]) -> list[str]:
        """Merge legacy browser pins at most once for this server user."""

        legacy_ids = _clean_tab_ids(list(tab_ids))
        with self._locked() as state:
            user_state = self._user_state(state, user_id)
            if not user_state.get("legacy_browser_pins_migrated", False):
                pinned_tab_ids = list(user_state["pinned_tab_ids"])
                for tab_id in legacy_ids:
                    if tab_id not in pinned_tab_ids:
                        pinned_tab_ids.append(tab_id)
                user_state["pinned_tab_ids"] = pinned_tab_ids
                user_state["legacy_browser_pins_migrated"] = True
            return list(user_state["pinned_tab_ids"])

    def remove_tab(self, tab_id: str) -> None:
        """Remove a permanently deleted tab from every user's pins."""

        with self._locked() as state:
            users = state.setdefault("users", {})
            for user_id in list(users):
                user_state = self._user_state(state, user_id)
                user_state["pinned_tab_ids"] = [
                    value for value in user_state["pinned_tab_ids"] if value != tab_id
                ]


def get_store() -> ChatSidebarPreferencesStore:
    return ChatSidebarPreferencesStore(resolve_runtime_home())
