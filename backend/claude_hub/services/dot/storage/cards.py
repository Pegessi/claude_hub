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

from ...runtime_isolation import resolve_runtime_home
from ..lessons import lesson_score
from ..prompts import DEFAULT_ROLE

logger = logging.getLogger(__name__)

CARD_KINDS = ("user", "work", "knowledge", "concept")
FOCUS_LIMIT = 7
MAX_BODY_CHARS = 200_000
MAX_FILE_BYTES = 50 * 1024 * 1024
_FRONTMATTER = "---"
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_JOURNAL_HEADER_RE = re.compile(r"(?m)^## (\d{2}:\d{2}) · (\w+)[ \t]*$")
_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
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
        return _whole_text(json.loads(raw))
    except ValueError:
        return raw


def _whole_text(value: Any) -> Any:
    """A JSON escape can give half an emoji (a lone surrogate), which no UTF-8 write,
    index or prompt accepts: it becomes "?"."""
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, list):
        return [_whole_text(one) for one in value]
    return value


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


def _priority(focus: dict[str, Any]) -> int:
    # focus.json is hand-editable too.
    try:
        return int(focus.get("priority") or 3)
    except (TypeError, ValueError, OverflowError):
        return 3


def _parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


def parse_card(text: str, path: Optional[Path] = None) -> Optional[Card]:
    """Parse a card file; tolerate hand edits. Returns None without a header."""
    # Only "\n" ends a line: str.splitlines() would also split on U+2028 or \x0c
    # inside a title or the body and change the card on its next save.
    # An editor may add a byte order mark.
    lines = text.removeprefix("\ufeff").split("\n")
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
    # The folder is the kind (lists, recall and the next save all agree), even when a
    # card was moved by hand without editing its header.
    folder = path.parent.name if path is not None else ""
    kind = folder if folder in CARD_KINDS else str(meta.get("kind") or "knowledge")
    try:
        importance = int(meta.get("importance", 2))
    except (TypeError, ValueError, OverflowError):
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
