"""Provider transcript forking for edit-resend.

When a user edits a previously sent message, the conversation must rerun
from that point.  This requires truncating *both* planes:

1. **Hub event store** — ``AgentStreamStore.truncate_before`` (handled by
   the caller).
2. **Provider transcript** — this module.  The provider's own JSONL file
   is truncated so that the first K genuine user messages (and everything
   before the (K+1)th) are preserved.  When the native transport restarts
   and resumes via ``--resume`` / ``thread/resume``, the provider sees a
   valid prefix and the edited text is delivered as the new Kth turn.

Genuine user message identification
------------------------------------
Each provider stores user messages differently, and tool-result records
also appear as ``role: user`` in some formats.  The counting must only
include messages the human actually typed:

- **Claude**: ``type: "user"`` with ``message.content`` containing at
  least one ``type: "text"`` block (tool results have ``type:
  "tool_result"`` blocks).
- **Codex**: ``type: "event_msg"`` with ``payload.type ==
  "user_message"``.
- **Cursor**: ``role: "user"`` with a ``message`` dict.

Unsupported agent types fail closed (raise ``ValueError``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...models import AgentType, ManagedSession
from .base import discover_source_cached

logger = logging.getLogger(__name__)


class TranscriptForkError(ValueError):
    """Raised when the transcript cannot be safely forked."""


def _is_claude_user_message(obj: Dict[str, Any]) -> bool:
    """True for a genuine Claude user message (not a tool_result)."""
    if obj.get("type") != "user":
        return False
    message = obj.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "text"
        for block in content
    )


def _is_codex_user_message(obj: Dict[str, Any]) -> bool:
    """True for a Codex user_message event."""
    if obj.get("type") != "event_msg":
        return False
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return False
    return payload.get("type") == "user_message"


def _is_cursor_user_message(obj: Dict[str, Any]) -> bool:
    """True for a Cursor transcript user row."""
    if obj.get("role") != "user":
        return False
    return isinstance(obj.get("message"), dict)


def _user_message_predicate(agent_type: AgentType):
    """Return the genuine-user-message predicate for ``agent_type``."""
    if agent_type == AgentType.CLAUDE:
        return _is_claude_user_message
    if agent_type == AgentType.CODEX:
        return _is_codex_user_message
    if agent_type == AgentType.CURSOR:
        return _is_cursor_user_message
    raise TranscriptForkError(
        f"edit-resend is not supported for agent_type={agent_type}"
    )


def fork_transcript(
    session: ManagedSession,
    adapter: Any,
    turn_index: int,
) -> int:
    """Truncate the provider transcript at the ``turn_index``-th user message.

    Preserves all records *before* the (turn_index)-th genuine user message
    (0-based).  Everything from that user message onward is discarded.

    Args:
        session: The managed session.
        adapter: The structured-stream adapter (used to locate the file).
        turn_index: 0-based index of the user message to replace.

    Returns:
        The number of transcript lines removed.

    Raises:
        TranscriptForkError: If the transcript cannot be located, the turn
            index is out of range, or the agent type is unsupported.
    """
    path: Optional[Path] = discover_source_cached(adapter, session)
    if path is None:
        raise TranscriptForkError("could not locate provider transcript file")
    if not path.exists():
        raise TranscriptForkError(f"transcript file does not exist: {path}")

    predicate = _user_message_predicate(session.agent_type)

    kept: List[str] = []
    removed = 0
    user_count = 0
    found = False

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            if not found:
                try:
                    obj = json.loads(stripped)
                except (json.JSONDecodeError, ValueError):
                    # Keep unparseable lines in the prefix (defensive).
                    kept.append(stripped)
                    continue
                if predicate(obj):
                    if user_count == turn_index:
                        found = True
                        removed += 1
                        continue
                    user_count += 1
                kept.append(stripped)
            else:
                removed += 1

    if not found:
        raise TranscriptForkError(
            f"turn_index={turn_index} is out of range "
            f"(only {user_count} user messages in transcript)"
        )

    # Write the truncated transcript atomically: write to a temp file first,
    # then rename, so a crash mid-write does not corrupt the original.
    tmp_path = path.with_suffix(path.suffix + ".fork-tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            for line in kept:
                f.write(line + "\n")
        tmp_path.replace(path)
    except OSError as exc:
        raise TranscriptForkError(f"failed to write forked transcript: {exc}") from exc
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass

    logger.info(
        "forked transcript for session %s at turn_index=%d: kept %d lines, removed %d",
        session.id,
        turn_index,
        len(kept),
        removed,
    )
    return removed
