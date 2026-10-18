"""Dot focus, work, lesson, and inbox memory collections."""

from claude_hub.services.dot.storage.cards import (
    FOCUS_LIMIT,
    LESSON_RULE_LIMIT,
    Any,
    Iterable,
    Optional,
    _lesson_tasks,
    _lesson_votes,
    _priority,
    lesson_score,
    uuid,
)
from claude_hub.services.dot.storage.repository import DotRepository


class DotMemoryStore(DotRepository):

    # ── focus ───────────────────────────────────────────────────────────

    def list_focus(self, *, include_closed: bool = False) -> list[dict[str, Any]]:
        self.ensure()
        items = [item for item in self._read_json("focus.json", []) if isinstance(item, dict)]
        if not include_closed:
            items = [item for item in items if item.get("status", "active") == "active"]
        # Stable: same priority keeps the order items were added in.
        return sorted(items, key=_priority)

    def add_focus(self, title: str, why: str = "", priority: int = 3) -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise ValueError("title is required")
        with self._lock:
            items = self._read_json("focus.json", [], update=True)
            active = [item for item in items if item.get("status", "active") == "active"]
            if len(active) >= FOCUS_LIMIT:
                raise ValueError(f"focus is full ({FOCUS_LIMIT} active); close or drop one first")
            item = {
                "id": f"f-{uuid.uuid4().hex[:6]}",
                "title": title,
                "why": why.strip(),
                "priority": max(1, min(5, int(priority))),
                "status": "active",
                "created": self._now_iso(),
                "updated": self._now_iso(),
            }
            items.append(item)
            self._write_json("focus.json", items)
            return item

    def update_focus(self, focus_id: str, **changes: Any) -> Optional[dict[str, Any]]:
        with self._lock:
            items = self._read_json("focus.json", [], update=True)
            for item in items:
                if item.get("id") != focus_id:
                    continue
                for key in ("title", "why", "priority", "status"):
                    value = changes.get(key)
                    if value is None:
                        continue
                    if key == "title":
                        value = value.strip()
                        if not value:
                            raise ValueError("title is required")
                    if key == "priority":
                        value = max(1, min(5, int(value)))
                    if key == "status" and value not in ("active", "done", "dropped"):
                        raise ValueError("status must be active, done or dropped")
                    if key == "status" and value == "active" != item.get("status", "active"):
                        active = [one for one in items if one.get("status", "active") == "active"]
                        if len(active) >= FOCUS_LIMIT:
                            raise ValueError(
                                f"focus is full ({FOCUS_LIMIT} active); close or drop one first"
                            )
                    item[key] = value
                item["updated"] = self._now_iso()
                self._write_json("focus.json", items)
                return dict(item)
        return None

    # ── work ────────────────────────────────────────────────────────────

    def list_work(self, *, include_closed: bool = False) -> list[dict[str, Any]]:
        self.ensure()
        items = [item for item in self._read_json("work.json", []) if isinstance(item, dict)]
        if not include_closed:
            items = [item for item in items if not item.get("closed")]
        return items

    def upsert_work(self, task_id: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            items = self._read_json("work.json", [], update=True)
            for item in items:
                if item.get("task_id") == task_id:
                    item.update({key: value for key, value in fields.items() if value is not None})
                    item["updated"] = self._now_iso()
                    break
            else:
                item = {"task_id": task_id, "created": self._now_iso(), "updated": self._now_iso()}
                item.update({key: value for key, value in fields.items() if value is not None})
                items.append(item)
            self._write_json("work.json", items)
            return dict(item)

    def get_work(self, task_id: str) -> Optional[dict[str, Any]]:
        for item in self.list_work(include_closed=True):
            if item.get("task_id") == task_id:
                return item
        return None

    def pending_reviews(self) -> list[dict[str, Any]]:
        """Delegated tasks with a result Dot has not judged yet (oldest first)."""
        return [
            item
            for item in self.list_work(include_closed=True)
            # A watched task's brief was not Dot's; it is never judged.
            if item.get("review_due") and not item.get("stopped") and not item.get("watched")
        ]

    # ── lessons ─────────────────────────────────────────────────────────

    def list_lessons(self, *, include_dropped: bool = False) -> list[dict[str, Any]]:
        self.ensure()
        items = [item for item in self._read_json("lessons.json", []) if isinstance(item, dict)]
        if not include_dropped:
            items = [item for item in items if not item.get("dropped")]
        return items

    def get_lesson(self, lesson_id: str) -> Optional[dict[str, Any]]:
        for item in self.list_lessons(include_dropped=True):
            if item.get("id") == lesson_id:
                return item
        return None

    @staticmethod
    def _clean_rule(rule: str) -> str:
        clean = " ".join(rule.split())
        if not clean or len(clean) > LESSON_RULE_LIMIT:
            raise ValueError(f"a lesson is one rule of 1-{LESSON_RULE_LIMIT} characters")
        return clean

    def add_lesson(self, rule: str, task_id: Optional[str] = None) -> dict[str, Any]:
        clean = self._clean_rule(rule)
        self.ensure()
        with self._lock:
            items = self._read_json("lessons.json", [], update=True)
            stamp = self._now_iso()
            item = {
                "id": f"l-{uuid.uuid4().hex[:6]}",
                "rule": clean,
                "votes": 1,
                "tasks": [task_id] if task_id else [],
                "created": stamp,
                "seen": stamp,
            }
            items.append(item)
            self._write_json("lessons.json", items)
            return dict(item)

    def _change_lesson(self, lesson_id: str, change: Any) -> Optional[dict[str, Any]]:
        self.ensure()
        with self._lock:
            items = self._read_json("lessons.json", [], update=True)
            for item in items:
                if isinstance(item, dict) and item.get("id") == lesson_id:
                    change(item)
                    self._write_json("lessons.json", items)
                    return dict(item)
        return None

    def vote_lesson(
        self, lesson_id: str, task_id: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """The same miss happened again: one more vote, and the lesson is fresh again.

        A vote for a merged lesson goes to the lesson it was merged into. One
        task votes a lesson once, however many review rounds it has. Returns
        None for an unknown id; raises ValueError when the lesson was dropped
        (the user said it is wrong; a vote must not bring it back).
        """
        self.ensure()
        with self._lock:
            items = [
                item
                for item in self._read_json("lessons.json", [], update=True)
                if isinstance(item, dict)
            ]
            by_id = {item.get("id"): item for item in items}
            item = by_id.get(lesson_id)
            hops = 0
            while item is not None and item.get("merged_into") and hops < len(items):
                item, hops = by_id.get(item["merged_into"]), hops + 1
            if item is None:
                return None
            if item.get("dropped"):
                raise ValueError(f"lesson {item.get('id')} is dropped; write a new rule instead")
            if task_id and task_id in _lesson_tasks(item):
                return dict(item)
            now = self._utc_now()
            item["weight"] = round(lesson_score(item, now) + 1, 3)
            item["votes"] = _lesson_votes(item) + 1
            item["seen"] = now.isoformat(timespec="seconds")
            if task_id:
                item["tasks"] = [*_lesson_tasks(item), task_id][-20:]
            self._write_json("lessons.json", items)
            return dict(item)

    def update_lesson(
        self, lesson_id: str, *, rule: Optional[str] = None, dropped: Optional[bool] = None
    ) -> Optional[dict[str, Any]]:
        clean = self._clean_rule(rule) if rule is not None else None

        def update(item: dict[str, Any]) -> None:
            if clean is not None:
                item["rule"] = clean
            if dropped:
                item["dropped"] = True
            elif dropped is False:
                item.pop("dropped", None)

        return self._change_lesson(lesson_id, update)

    def merge_lessons(self, keep_id: str, other_ids: Iterable[str]) -> dict[str, Any]:
        """Fold lessons that say the same thing into one; strength, votes and tasks add up.

        Raises KeyError for an unknown id, ValueError when ``keep_id`` is dropped.
        """
        others = [other for other in dict.fromkeys(other_ids) if other != keep_id]
        self.ensure()
        with self._lock:
            items = [
                item
                for item in self._read_json("lessons.json", [], update=True)
                if isinstance(item, dict)
            ]
            by_id = {item.get("id"): item for item in items}
            keep = by_id.get(keep_id)
            missing = [other for other in others if other not in by_id]
            if keep is None or missing:
                raise KeyError(keep_id if keep is None else missing[0])
            if keep.get("dropped"):
                where = keep.get("merged_into")
                raise ValueError(
                    f"{keep_id} was merged into {where}; merge into that"
                    if where
                    else f"{keep_id} is dropped; keep a live lesson"
                )
            now = self._utc_now()
            # Strength is decayed to today first, so old votes count for what they are worth now.
            weight = lesson_score(keep, now)
            for other in others:
                item = by_id[other]
                if item.get("dropped"):
                    continue
                weight += lesson_score(item, now)
                keep["votes"] = _lesson_votes(keep) + _lesson_votes(item)
                keep["tasks"] = list(dict.fromkeys([*_lesson_tasks(keep), *_lesson_tasks(item)]))[
                    -20:
                ]
                item["dropped"] = True
                item["merged_into"] = keep_id
            keep["weight"] = round(weight, 3)
            keep["seen"] = now.isoformat(timespec="seconds")
            self._write_json("lessons.json", items)
            return dict(keep)

    # ── inbox ───────────────────────────────────────────────────────────

    def list_inbox(self, *, unread_only: bool = True) -> list[dict[str, Any]]:
        self.ensure()
        items = [item for item in self._read_json("inbox.json", []) if isinstance(item, dict)]
        if unread_only:
            items = [item for item in items if not item.get("read")]
        return items

    def add_inbox(self, kind: str, text: str, **extra: Any) -> dict[str, Any]:
        """Add one event; one with the ``key`` of an unread event is that event."""
        with self._lock:
            items = self._read_json("inbox.json", [], update=True)
            key = extra.get("key")
            for entry in items:
                if key and entry.get("key") == key and not entry.get("read"):
                    return dict(entry)
            item = {
                "id": f"e-{uuid.uuid4().hex[:8]}",
                "at": self._now_iso(),
                "kind": kind,
                "text": text,
                "read": False,
                **extra,
            }
            items.append(item)
            # Keep the file small: drop the oldest read events beyond 200.
            read = [entry for entry in items if entry.get("read")]
            if len(read) > 200:
                drop = {entry.get("id") for entry in read[: len(read) - 200]}
                items = [entry for entry in items if entry.get("id") not in drop]
            self._write_json("inbox.json", items)
            return item

    def mark_inbox_read(self, ids: Optional[Iterable[str]] = None) -> int:
        wanted = set(ids) if ids is not None else None
        with self._lock:
            items = self._read_json("inbox.json", [], update=True)
            count = 0
            for item in items:
                if item.get("read"):
                    continue
                if wanted is None or item.get("id") in wanted:
                    item["read"] = True
                    count += 1
            if count:
                self._write_json("inbox.json", items)
            return count

    def mark_inbox_unread(self, ids: Iterable[str]) -> int:
        wanted = set(ids)
        with self._lock:
            items = self._read_json("inbox.json", [], update=True)
            count = 0
            for item in items:
                if item.get("read") and item.get("id") in wanted:
                    item["read"] = False
                    count += 1
            if count:
                self._write_json("inbox.json", items)
            return count
