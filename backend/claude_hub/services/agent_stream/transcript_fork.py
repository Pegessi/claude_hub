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
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ...models import AgentType, ManagedSession
from .base import discover_source_cached

logger = logging.getLogger(__name__)


class TranscriptForkError(ValueError):
    """Raised when the transcript cannot be safely forked."""


# ── snapshot / restore (edit-resend rollback) ───────────────────────────────


def snapshot_transcript(path: Optional[Path]) -> Optional[Path]:
    """Create a ``.edit-bak`` sidecar copy of the transcript for rollback.

    Returns the backup path, or ``None`` if the transcript does not exist.
    """
    if path is None or not path.exists():
        return None
    backup = path.with_name(path.name + ".edit-bak")
    shutil.copy2(path, backup)
    return backup


def restore_transcript(path: Optional[Path], backup: Optional[Path]) -> None:
    """Restore the transcript from a snapshot created by :func:`snapshot_transcript`."""
    if path is None or backup is None or not backup.exists():
        return
    shutil.copy2(backup, path)


def discard_snapshot(backup: Optional[Path]) -> None:
    """Delete a snapshot sidecar file, if it exists.  Never raises."""
    if backup is None:
        return
    try:
        backup.unlink()
    except FileNotFoundError:
        pass


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
    return any(isinstance(block, dict) and block.get("type") == "text" for block in content)


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


def _user_message_predicate(agent_type: AgentType) -> Callable[[Dict[str, Any]], bool]:
    """Return the genuine-user-message predicate for ``agent_type``."""
    if agent_type == AgentType.CLAUDE:
        return _is_claude_user_message
    if agent_type == AgentType.CODEX:
        return _is_codex_user_message
    if agent_type == AgentType.CURSOR:
        return _is_cursor_user_message
    raise TranscriptForkError(f"edit-resend is not supported for agent_type={agent_type}")


def _extract_user_text(obj: Dict[str, Any], agent_type: AgentType) -> str:
    """Extract the typed text from a genuine user message for content matching.

    Each provider stores the text differently; this must agree with the
    predicate above so the same records are both counted and matched.
    """
    if agent_type == AgentType.CLAUDE:
        message = obj.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
        return ""
    if agent_type == AgentType.CODEX:
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            return ""
        message = payload.get("message")
        return message if isinstance(message, str) else ""
    if agent_type == AgentType.CURSOR:
        message = obj.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(parts)
        return ""
    return ""


def _normalize_text(text: str) -> str:
    """Normalize text for comparison: strip surrounding whitespace."""
    return text.strip()


def _find_target_line(
    user_messages: List[Tuple[int, int, str]],
    turn_index: int,
    turn_text: str,
) -> int:
    """Find the transcript line number of the user message for the edited turn.

    ``user_messages`` is a list of ``(line_number, ordinal, text)`` tuples in
    file order.  The edited turn is matched to a provider message by *content*
    (normalized), disambiguated by ordinal position:

    1. An exact ordinal+content match is preferred.
    2. Otherwise the content match with the largest ordinal below
       ``turn_index`` is used (divergence from failed deliveries shifts
       provider ordinals down).
    3. Otherwise the content match with the smallest ordinal.

    Raises ``TranscriptForkError`` if there is no content match at all — the
    turn's delivery likely failed and there is no provider message to
    truncate at, so we fail fast rather than guess.
    """
    target = _normalize_text(turn_text)
    matches = [
        (ordinal, line_no)
        for line_no, ordinal, text in user_messages
        if _normalize_text(text) == target
    ]
    if not matches:
        raise TranscriptForkError(
            "edited turn has no matching provider user message "
            "(its delivery may have failed); refusing to guess the truncation point"
        )
    for ordinal, line_no in matches:
        if ordinal == turn_index:
            return line_no
    below = [(ordinal, line_no) for ordinal, line_no in matches if ordinal < turn_index]
    if below:
        return max(below, key=lambda item: item[0])[1]
    return min(matches, key=lambda item: item[0])[1]


def fork_transcript(
    session: ManagedSession,
    adapter: Any,
    turn_index: int,
    turn_text: str,
) -> int:
    """Truncate the provider transcript at the user message for the edited turn.

    The Hub turn identified by the caller is matched to a provider user
    message by *content* (``turn_text``), disambiguated by ``turn_index``, so
    a ``turn_started`` whose provider delivery failed cannot shift the
    truncation onto the wrong message.  All records before the matched user
    message are preserved; the matched message and everything after it is
    discarded.

    Args:
        session: The managed session.
        adapter: The structured-stream adapter (used to locate the file).
        turn_index: 0-based ordinal of the edited turn among Hub
            ``turn_started`` events (used to disambiguate duplicate content).
        turn_text: The edited turn's text (``payload.summary`` from the Hub
            store) used to match the provider user message by content.

    Returns:
        The number of transcript lines removed.

    Raises:
        TranscriptForkError: If the transcript cannot be located, the agent
            type is unsupported, or no provider user message matches
            ``turn_text`` (unmappable turn).
    """
    path: Optional[Path] = discover_source_cached(adapter, session)
    if path is None:
        raise TranscriptForkError("could not locate provider transcript file")
    if not path.exists():
        raise TranscriptForkError(f"transcript file does not exist: {path}")

    predicate = _user_message_predicate(session.agent_type)

    # Read every line and collect genuine user messages with their file line
    # numbers, ordinals, and extracted text.
    all_lines: List[str] = []
    user_messages: List[Tuple[int, int, str]] = []  # (line_no, ordinal, text)
    user_count = 0

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            line_no = len(all_lines)
            all_lines.append(stripped)
            try:
                obj = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                # Keep unparseable lines in the prefix (defensive).
                continue
            if predicate(obj):
                text = _extract_user_text(obj, session.agent_type)
                user_messages.append((line_no, user_count, text))
                user_count += 1

    # Find the line to truncate at (raises TranscriptForkError if unmappable).
    target_line = _find_target_line(user_messages, turn_index, turn_text)

    # Keep everything strictly before the target line.
    kept = all_lines[:target_line]
    removed = len(all_lines) - target_line

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
