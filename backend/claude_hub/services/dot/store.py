"""File-based Dot memory home.

Markdown cards and small JSON files under one git-backed directory are the
source of truth. Everything else (the search index, context packs) is derived
from them, so Dot can also read and edit the files directly with its own tools.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from ..runtime_isolation import resolve_runtime_home
from .lessons import lesson_score
from .prompts import DEFAULT_ROLE

logger = logging.getLogger(__name__)

CARD_KINDS = ("user", "work", "knowledge", "concept")
FOCUS_LIMIT = 7
MAX_BODY_CHARS = 200_000
MAX_FILE_BYTES = 50 * 1024 * 1024
_FRONTMATTER = "---"
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_JOURNAL_HEADER_RE = re.compile(r"(?m)^## (\d{2}:\d{2}) · (\w+)[ \t]*$")
_GITIGNORE = "index.sqlite*\n*.tmp\n.lock\n"
DEFAULT_NAME = "Dot"
NAME_LIMIT = 40
# Code points, not characters: one emoji may be several code points.
AVATAR_TEXT_LIMIT = 16
AVATAR_IMAGE_BYTES = 2 * 1024 * 1024
LESSON_RULE_LIMIT = 200
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


def resolve_dot_home() -> Path:
    """Return the Dot home, honouring ``CLAUDE_HUB_DOT_HOME``."""
    override = os.environ.get("CLAUDE_HUB_DOT_HOME")
    if override:
        return Path(override).expanduser()
    return resolve_runtime_home() / "dot"


def _slug(text: str, limit: int = 40) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug[:limit].strip("-") or "card"


def _dump_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load_value(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        return ""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


@dataclass
class Card:
    """One memory card: a fact, decision, result, file note or concept."""

    id: str
    kind: str
    title: str
    summary: str = ""
    body: str = ""
    tags: list[str] = field(default_factory=list)
    importance: int = 2
    links: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    source: str = ""
    created: str = ""
    updated: str = ""
    archived: bool = False
    path: Optional[Path] = None

    def to_text(self) -> str:
        meta = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "tags": self.tags,
            "importance": self.importance,
            "links": self.links,
            "files": self.files,
            "source": self.source,
            "created": self.created,
            "updated": self.updated,
            "archived": self.archived,
        }
        lines = [_FRONTMATTER]
        lines.extend(f"{key}: {_dump_value(value)}" for key, value in meta.items())
        lines.append(_FRONTMATTER)
        body = self.body.rstrip()
        return "\n".join(lines) + "\n" + (body + "\n" if body else "")

    def to_dict(self, *, with_body: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "tags": self.tags,
            "importance": self.importance,
            "links": self.links,
            "files": self.files,
            "source": self.source,
            "created": self.created,
            "updated": self.updated,
            "archived": self.archived,
        }
        if with_body:
            data["body"] = self.body
        return data


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _lesson_votes(lesson: dict[str, Any]) -> int:
    # lessons.json is hand-editable; a bad count is 0, not a crash.
    try:
        return max(int(lesson.get("votes") or 0), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _lesson_tasks(lesson: dict[str, Any]) -> list[str]:
    tasks = lesson.get("tasks")
    return [str(task) for task in tasks] if isinstance(tasks, list) else []


def parse_card(text: str, path: Optional[Path] = None) -> Optional[Card]:
    """Parse a card file; tolerate hand edits. Returns None without a header."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER:
        return None
    meta: dict[str, Any] = {}
    end = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == _FRONTMATTER:
            end = index
            break
        key, sep, raw = line.partition(":")
        if sep:
            meta[key.strip()] = _load_value(raw)
    if end is None:
        return None
    body = "\n".join(lines[end + 1 :]).strip("\n")
    fallback_id = path.stem if path is not None else ""
    kind = str(meta.get("kind") or (path.parent.name if path is not None else "knowledge"))
    try:
        importance = int(meta.get("importance", 2))
    except (TypeError, ValueError):
        importance = 2
    return Card(
        id=str(meta.get("id") or fallback_id),
        kind=kind if kind in CARD_KINDS else "knowledge",
        title=str(meta.get("title") or fallback_id),
        summary=str(meta.get("summary") or ""),
        body=body,
        tags=_as_str_list(meta.get("tags")),
        importance=max(1, min(5, importance)),
        links=_as_str_list(meta.get("links")),
        files=_as_str_list(meta.get("files")),
        source=str(meta.get("source") or ""),
        created=str(meta.get("created") or ""),
        updated=str(meta.get("updated") or ""),
        archived=meta.get("archived") in (True, "true", "yes", 1),
        path=path,
    )


class DotStore:
    """Read and write the Dot home. Thread-safe within one process."""

    def __init__(self, home: Optional[Path] = None) -> None:
        self.home = (home or resolve_dot_home()).expanduser()
        self._lock = threading.RLock()
        # Commits use their own lock: git can take seconds and must not block
        # JSON reads and writes (some run on the event loop). Writes are
        # atomic replaces, so ``git add`` never sees half a file.
        self._git_lock = threading.Lock()
        self._ready = False

    @property
    def lock(self) -> threading.RLock:
        """The store lock, for callers that read, decide and write state."""
        return self._lock

    # ── layout ──────────────────────────────────────────────────────────

    @property
    def memory_dir(self) -> Path:
        return self.home / "memory"

    @property
    def journal_dir(self) -> Path:
        return self.home / "journal"

    @property
    def files_dir(self) -> Path:
        return self.home / "files"

    @property
    def role_path(self) -> Path:
        return self.home / "DOT.md"

    @property
    def index_path(self) -> Path:
        return self.home / "index.sqlite"

    def ensure(self) -> None:
        """Create the home layout and git repo once per process."""
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            for directory in (self.home, self.journal_dir, self.files_dir):
                directory.mkdir(parents=True, exist_ok=True)
            for kind in CARD_KINDS:
                (self.memory_dir / kind).mkdir(parents=True, exist_ok=True)
            if not self.role_path.exists():
                self.role_path.write_text(DEFAULT_ROLE, encoding="utf-8")
            gitignore = self.home / ".gitignore"
            if not gitignore.exists():
                gitignore.write_text(_GITIGNORE, encoding="utf-8")
            for name, empty in (
                ("focus.json", []),
                ("work.json", []),
                ("inbox.json", []),
                ("lessons.json", []),
                ("state.json", {}),
            ):
                if not (self.home / name).exists():
                    self._write_json(name, empty)
            if not (self.home / ".git").exists() and shutil.which("git"):
                self._git("init", "-q")
                self.commit("dot: init memory home")
            self._ready = True

    # ── json files ──────────────────────────────────────────────────────

    def _read_json(self, name: str, default: Any) -> Any:
        try:
            value = json.loads((self.home / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
        return value if isinstance(value, type(default)) else default

    def _write_json(self, name: str, value: Any) -> None:
        self._atomic_write(self.home / name, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def _atomic_write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(raw)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def read_state(self) -> dict[str, Any]:
        self.ensure()
        return dict(self._read_json("state.json", {}))

    def update_state(self, **changes: Any) -> dict[str, Any]:
        self.ensure()
        with self._lock:
            state = self._read_json("state.json", {})
            for key, value in changes.items():
                if value is None:
                    state.pop(key, None)
                else:
                    state[key] = value
            self._write_json("state.json", state)
            return dict(state)

    def read_role(self) -> str:
        self.ensure()
        try:
            return self.role_path.read_text(encoding="utf-8")
        except OSError:
            return DEFAULT_ROLE

    # ── identity ────────────────────────────────────────────────────────

    def read_identity(self) -> dict[str, Any]:
        """Dot's name and avatar. Never creates the home (read by every status call)."""
        raw = self._read_json("identity.json", {})
        name = " ".join(str(raw.get("name") or "").split())[:NAME_LIMIT]
        avatar = str(raw.get("avatar") or "").strip()[:AVATAR_TEXT_LIMIT]
        image = raw.get("avatar_image")
        if not (isinstance(image, str) and self.resolve_file(image) is not None):
            image = None
        return {"name": name or DEFAULT_NAME, "avatar": avatar, "avatar_image": image}

    def update_identity(
        self,
        *,
        name: Optional[str] = None,
        avatar: Optional[str] = None,
        avatar_image: Optional[str] = None,
    ) -> dict[str, Any]:
        """Change the given fields; ``None`` keeps a field, ``""`` clears avatar/image."""
        self.ensure()
        with self._lock:
            identity = self.read_identity()
            if name is not None:
                clean = " ".join(name.split())
                if not clean or len(clean) > NAME_LIMIT or _CONTROL_RE.search(clean):
                    raise ValueError(f"name must be 1-{NAME_LIMIT} characters")
                identity["name"] = clean
            if avatar is not None:
                clean = avatar.strip()
                if len(clean) > AVATAR_TEXT_LIMIT or _CONTROL_RE.search(clean):
                    raise ValueError("avatar must be a short emoji or text")
                identity["avatar"] = clean
            if avatar_image is not None:
                if avatar_image and self.resolve_file(avatar_image) is None:
                    raise ValueError(f"file not found in Dot home: {avatar_image}")
                identity["avatar_image"] = avatar_image or None
            self._write_json("identity.json", identity)
            return identity

    def store_avatar(self, data: bytes) -> str:
        """Store an avatar image (PNG, JPEG, GIF or WebP; no SVG); return its path."""
        if len(data) > AVATAR_IMAGE_BYTES:
            raise ValueError(f"avatar image must be at most {AVATAR_IMAGE_BYTES // 1024} KB")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = ".png"
        elif data.startswith(b"\xff\xd8\xff"):
            suffix = ".jpg"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            suffix = ".gif"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            suffix = ".webp"
        else:
            raise ValueError("avatar must be a PNG, JPEG, GIF or WebP image")
        return self.store_file(data, f"avatar{suffix}")

    # ── cards ───────────────────────────────────────────────────────────

    def iter_card_paths(self) -> Iterable[Path]:
        self.ensure()
        for kind in CARD_KINDS:
            yield from sorted((self.memory_dir / kind).glob("*.md"))

    def list_cards(
        self, kind: Optional[str] = None, *, include_archived: bool = False
    ) -> list[Card]:
        cards: list[Card] = []
        for path in self.iter_card_paths():
            if kind and path.parent.name != kind:
                continue
            card = self.read_card_path(path)
            if card is None or (card.archived and not include_archived):
                continue
            cards.append(card)
        return cards

    @staticmethod
    def read_card_path(path: Path) -> Optional[Card]:
        try:
            return parse_card(path.read_text(encoding="utf-8"), path)
        except OSError:
            return None

    def find_card_path(self, card_id: str) -> Optional[Path]:
        card_id = card_id.strip()
        if not card_id or "/" in card_id or card_id.startswith("."):
            return None
        for kind in CARD_KINDS:
            candidate = self.memory_dir / kind / f"{card_id}.md"
            if candidate.exists():
                return candidate
        for path in self.iter_card_paths():
            card = self.read_card_path(path)
            if card is not None and card.id == card_id:
                return path
        return None

    def get_card(self, card_id: str) -> Optional[Card]:
        path = self.find_card_path(card_id)
        return self.read_card_path(path) if path is not None else None

    def new_card_id(self, kind: str, title: str) -> str:
        return f"{kind[0]}-{_slug(title)}-{uuid.uuid4().hex[:6]}"

    def save_card(self, card: Card) -> Card:
        """Create or replace a card. Moves the file if its kind changed."""
        self.ensure()
        if card.kind not in CARD_KINDS:
            raise ValueError(f"kind must be one of {', '.join(CARD_KINDS)}")
        card.title = card.title.strip()
        if not card.title:
            raise ValueError("title is required")
        if len(card.body) > MAX_BODY_CHARS:
            raise ValueError(f"body is longer than {MAX_BODY_CHARS} characters")
        with self._lock:
            stamp = now_iso()
            if not card.id:
                card.id = self.new_card_id(card.kind, card.title)
            old_path = self.find_card_path(card.id)
            card.created = card.created or stamp
            card.updated = stamp
            path = self.memory_dir / card.kind / f"{card.id}.md"
            self._atomic_write(path, card.to_text())
            if old_path is not None and old_path != path:
                old_path.unlink(missing_ok=True)
            card.path = path
            return card

    def archive_card(self, card_id: str) -> Optional[Card]:
        card = self.get_card(card_id)
        if card is None:
            return None
        card.archived = True
        return self.save_card(card)

    def delete_card(self, card_id: str) -> bool:
        path = self.find_card_path(card_id)
        if path is None:
            return False
        path.unlink(missing_ok=True)
        return True

    # ── files ───────────────────────────────────────────────────────────

    def store_file(self, data: bytes, filename: str) -> str:
        """Store bytes under ``files/`` by content hash; return the home-relative path."""
        self.ensure()
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("file is too large")
        suffix = Path(filename).suffix.lower()[:12]
        if not re.fullmatch(r"(\.[a-z0-9]+)?", suffix):
            suffix = ""
        digest = hashlib.sha256(data).hexdigest()[:16]
        path = self.files_dir / f"{digest}{suffix}"
        if not path.exists():
            with self._lock:
                fd, raw = tempfile.mkstemp(prefix=".file.", suffix=".tmp", dir=self.files_dir)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                os.replace(raw, path)
        return str(path.relative_to(self.home))

    def resolve_home_path(self, relative: str) -> Optional[Path]:
        """Resolve a home-relative path, refusing escapes."""
        candidate = (self.home / relative).resolve()
        try:
            candidate.relative_to(self.home.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def resolve_file(self, relative: str) -> Optional[Path]:
        """Resolve a ``files/...`` path, refusing anything outside ``files/``."""
        candidate = (self.home / relative).resolve()
        try:
            candidate.relative_to(self.files_dir.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    @staticmethod
    def guess_mime(path: Path) -> str:
        return mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    # ── focus ───────────────────────────────────────────────────────────

    def list_focus(self, *, include_closed: bool = False) -> list[dict[str, Any]]:
        self.ensure()
        items = [item for item in self._read_json("focus.json", []) if isinstance(item, dict)]
        if not include_closed:
            items = [item for item in items if item.get("status", "active") == "active"]
        return sorted(items, key=lambda item: (int(item.get("priority") or 3), item.get("id", "")))

    def add_focus(self, title: str, why: str = "", priority: int = 3) -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise ValueError("title is required")
        with self._lock:
            items = self._read_json("focus.json", [])
            active = [item for item in items if item.get("status", "active") == "active"]
            if len(active) >= FOCUS_LIMIT:
                raise ValueError(f"focus is full ({FOCUS_LIMIT} active); close or drop one first")
            item = {
                "id": f"f-{uuid.uuid4().hex[:6]}",
                "title": title,
                "why": why.strip(),
                "priority": max(1, min(5, int(priority))),
                "status": "active",
                "created": now_iso(),
                "updated": now_iso(),
            }
            items.append(item)
            self._write_json("focus.json", items)
            return item

    def update_focus(self, focus_id: str, **changes: Any) -> Optional[dict[str, Any]]:
        with self._lock:
            items = self._read_json("focus.json", [])
            for item in items:
                if item.get("id") != focus_id:
                    continue
                for key in ("title", "why", "priority", "status"):
                    value = changes.get(key)
                    if value is None:
                        continue
                    if key == "priority":
                        value = max(1, min(5, int(value)))
                    if key == "status" and value not in ("active", "done", "dropped"):
                        raise ValueError("status must be active, done or dropped")
                    item[key] = value
                item["updated"] = now_iso()
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
            items = self._read_json("work.json", [])
            for item in items:
                if item.get("task_id") == task_id:
                    item.update({key: value for key, value in fields.items() if value is not None})
                    item["updated"] = now_iso()
                    break
            else:
                item = {"task_id": task_id, "created": now_iso(), "updated": now_iso()}
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
            if item.get("review_due") and not item.get("stopped")
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
            items = self._read_json("lessons.json", [])
            stamp = now_iso()
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
            items = self._read_json("lessons.json", [])
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
            items = [item for item in self._read_json("lessons.json", []) if isinstance(item, dict)]
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
            now = utc_now()
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
            items = [item for item in self._read_json("lessons.json", []) if isinstance(item, dict)]
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
            now = utc_now()
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
        with self._lock:
            items = self._read_json("inbox.json", [])
            item = {
                "id": f"e-{uuid.uuid4().hex[:8]}",
                "at": now_iso(),
                "kind": kind,
                "text": text,
                "read": False,
                **extra,
            }
            items.append(item)
            # Keep the file small: drop the oldest read events beyond 200.
            read = [entry for entry in items if entry.get("read")]
            if len(read) > 200:
                drop = {entry["id"] for entry in read[: len(read) - 200]}
                items = [entry for entry in items if entry.get("id") not in drop]
            self._write_json("inbox.json", items)
            return item

    def mark_inbox_read(self, ids: Optional[Iterable[str]] = None) -> int:
        wanted = set(ids) if ids is not None else None
        with self._lock:
            items = self._read_json("inbox.json", [])
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

    # ── journal ─────────────────────────────────────────────────────────

    def journal_path(self, day: str) -> Path:
        return self.journal_dir / f"{day}.md"

    def append_journal(self, who: str, text: str, at: Optional[datetime] = None) -> None:
        self.ensure()
        moment = (at or utc_now()).astimezone()
        path = self.journal_path(moment.strftime("%Y-%m-%d"))
        entry = f"## {moment.strftime('%H:%M')} · {who}\n\n{text.strip()}\n\n"
        with self._lock, path.open("a", encoding="utf-8") as stream:
            stream.write(entry)

    def journal_days(self) -> list[str]:
        self.ensure()
        return sorted(path.stem for path in self.journal_dir.glob("*.md"))

    def recent_journal(self, max_entries: int = 8, max_chars: int = 400) -> list[str]:
        """Return the newest journal entries (oldest first), each shortened."""
        entries: list[str] = []
        for day in reversed(self.journal_days()[-3:]):
            try:
                text = self.journal_path(day).read_text(encoding="utf-8")
            except OSError:
                continue
            parts = _JOURNAL_HEADER_RE.split(text)
            # split() yields [preamble, time, who, body, time, who, body, ...]
            chunks = [(parts[i], parts[i + 1], parts[i + 2]) for i in range(1, len(parts) - 2, 3)]
            for clock, who, body in reversed(chunks):
                head = f"{clock} {who}"
                body = " ".join(body.split())
                if len(body) > max_chars:
                    body = body[: max_chars - 1] + "…"
                entries.append(f"[{day} {head}] {body}")
                if len(entries) >= max_entries:
                    return list(reversed(entries))
        return list(reversed(entries))

    # ── git ─────────────────────────────────────────────────────────────

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "git",
                "-C",
                str(self.home),
                "-c",
                "user.name=Dot",
                "-c",
                "user.email=dot@claude-hub.local",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def commit(self, message: str) -> bool:
        """Commit every change in the home. Returns True when a commit was made."""
        if not (self.home / ".git").exists():
            return False
        with self._git_lock:
            try:
                self._git("add", "-A")
                if self._git("diff", "--cached", "--quiet").returncode == 0:
                    return False
                result = self._git("commit", "-q", "-m", message)
            except (OSError, subprocess.SubprocessError):
                logger.warning("dot memory commit failed", exc_info=True)
                return False
            if result.returncode != 0:
                logger.warning("dot memory commit failed: %s", result.stderr.strip())
                return False
            return True
