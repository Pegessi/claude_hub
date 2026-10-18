"""Compatibility entrypoint for file-based Dot memory."""

from datetime import datetime

from claude_hub.services.dot.storage.cards import (
    _CONTROL_RE,
    _DAY_RE,
    _FRONTMATTER,
    _GITIGNORE,
    _JOURNAL_HEADER_RE,
    _SLUG_RE,
    AVATAR_IMAGE_BYTES,
    AVATAR_TEXT_LIMIT,
    CARD_KINDS,
    DEFAULT_NAME,
    DEFAULT_ROLE,
    FOCUS_LIMIT,
    LESSON_RULE_LIMIT,
    MAX_BODY_CHARS,
    MAX_FILE_BYTES,
    NAME_LIMIT,
    Any,
    Card,
    Iterable,
    Optional,
    Path,
    _as_str_list,
    _dump_value,
    _lesson_tasks,
    _lesson_votes,
    _load_value,
    _parse_json,
    _priority,
    _slug,
    _whole_text,
    parse_card,
    resolve_dot_home,
    utc_now,
)
from claude_hub.services.dot.storage.store import DotStore as _StorageDotStore


def now_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


class DotStore(_StorageDotStore):
    """Dot store with late-bound legacy facade clock hooks."""

    def _utc_now(self) -> datetime:
        return utc_now()

    def _now_iso(self) -> str:
        return now_iso()
