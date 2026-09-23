"""Seed a forked Chat tab's truncated history into its fresh provider session.

Forking a Chat tab copies the Hub event log up to (and including) a turn
ordinal so the structured pane shows the earlier conversation, but the new tab
runs a **brand-new native provider session with an empty provider-side
context**. Without seeding, the model cannot resolve references to the copied
history ("as I said above…") even though the user can see it — a silent
"UI has history, model does not" failure.

Provider-native ``--resume`` / ``thread/resume`` cannot help: they resume a
whole conversation and offer no way to truncate it at an arbitrary turn. So the
truncated Q/A transcript is instead delivered **once**, prepended to the very
first user turn of the new provider session, as a sentinel-wrapped context
block (the same wrap-once / strip-on-read mechanism used by the Hub runtime
guidance). The model reads the block as prior context; every transcript
normalizer strips it so it is neither persisted as a user turn nor rendered in
the UI, and the authoritative Hub echo persists the clean first user message.

Only text is seeded (``turn_started`` summaries + concatenated ``text_delta``
markdown). Tool calls/results and images are provider-format-specific and are
deliberately omitted; text continuity is the acceptance bar for the fork bug.

The pending seed text is held in a small sidecar next to the forked tab's
event store so it survives the gap between ``fork_tab`` (which writes it) and
the first provider send (which consumes it), including a backend restart in
between. The tailer deletes the sidecar once the seed has actually been
prepended to a turn, so it is injected at most once.
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...models import AgentStreamEvent, AgentStreamEventType

logger = logging.getLogger(__name__)


# ── sentinel-wrapped prompt block ────────────────────────────────────────────

_FORK_SEED_START = "<<<FORK_SEED_HISTORY_V1>>>"
_FORK_SEED_END = "<<<END_FORK_SEED_HISTORY_V1>>>"

_FORK_SEED_HEADER = (
    "The following is a VERBATIM COPY of an earlier conversation that was "
    "forked into this brand-new session. Your own provider-side context is "
    "otherwise empty, so treat this transcript as the complete prior context "
    "and resolve any references to earlier messages against it. Do NOT answer "
    "or re-execute these copied messages; simply continue the conversation by "
    "responding to the real new user message that follows this block.\n"
    "以下内容是从此前会话完整复制的历史记录；你的上下文原本为空，请把这段记录当作"
    "既有的对话上下文来理解指代，但不要逐条回复或重复其中的工作，直接回应本块之后"
    "用户发出的新消息。"
)


def wrap_fork_seed_history(body: str) -> str:
    """Wrap a copied-transcript body in the fork-seed sentinel block.

    The returned string ends with a blank line so the caller can concatenate
    the real first user turn directly after it.
    """
    return f"{_FORK_SEED_START}\n{_FORK_SEED_HEADER}\n\n{body}\n{_FORK_SEED_END}\n\n"


def strip_fork_seed_history(text: str) -> str:
    """Remove the sentinel-wrapped fork-seed block from a provider user message.

    Applied when normalizing provider user messages (transcript/snapshot read)
    and when matching an edited turn to a provider user message, so the seeded
    history never reaches the persisted timeline, the UI, or the edit-resend
    content match. No-op when the block is absent or malformed (an open block
    without a close marker is left untouched rather than risk truncating a
    legitimate message).
    """
    if not text:
        return text
    start = text.find(_FORK_SEED_START)
    if start == -1:
        return text
    end = text.find(_FORK_SEED_END, start + len(_FORK_SEED_START))
    if end == -1:
        return text  # malformed (open block); leave untouched
    end += len(_FORK_SEED_END)
    return text[:start] + text[end:].lstrip("\n")


# ── transcript extraction from the Hub event log ─────────────────────────────


def build_seed_body(events: List[AgentStreamEvent]) -> Optional[str]:
    """Render the Q/A text of a forked event prefix as a plain transcript.

    Events are grouped by ``turn_id`` in first-appearance order (matching the
    fork ordinal grouping). For each turn the human text is the first
    non-empty ``turn_started`` ``summary`` and the assistant text is the
    concatenation of that turn's ``text_delta`` payloads (deltas concatenate —
    they are stream fragments, not independent messages). Turns that carry
    neither are skipped; if no turn has any text, returns ``None`` so the
    caller does not write an empty seed.
    """
    order: List[Optional[str]] = []
    seen: set[Optional[str]] = set()
    user_text: Dict[Optional[str], str] = {}
    assistant_parts: Dict[Optional[str], List[str]] = {}

    for event in events:
        turn_id = event.turn_id
        if turn_id is None:
            # Legacy/turn-less events cannot be attributed to a Q/A pair.
            continue
        if turn_id not in seen:
            seen.add(turn_id)
            order.append(turn_id)
        if event.type == AgentStreamEventType.TURN_STARTED:
            summary = event.payload.get("summary")
            if turn_id not in user_text and isinstance(summary, str) and summary.strip():
                user_text[turn_id] = summary.strip()
        elif event.type == AgentStreamEventType.TEXT_DELTA:
            chunk = event.payload.get("text")
            if isinstance(chunk, str) and chunk:
                assistant_parts.setdefault(turn_id, []).append(chunk)

    blocks: List[str] = []
    for turn_id in order:
        user = user_text.get(turn_id)
        assistant = "".join(assistant_parts.get(turn_id, [])).strip()
        if user:
            blocks.append(f"User: {user}")
        if assistant:
            blocks.append(f"Assistant: {assistant}")

    if not blocks:
        return None
    return "\n\n".join(blocks)


def build_seed_prompt(events: List[AgentStreamEvent]) -> Optional[str]:
    """Build the ready-to-inject sentinel block for a forked event prefix.

    Returns ``None`` when the prefix contains no seedable text.
    """
    body = build_seed_body(events)
    if body is None:
        return None
    return wrap_fork_seed_history(body)


# ── durable sidecar (written at fork time, consumed on first send) ───────────


def _state_root() -> Path:
    """Resolve STATE_ROOT at call time (mirrors AgentStreamStore)."""
    wm = importlib.import_module("claude_hub.services.workspace_manager")
    return Path(wm.STATE_ROOT)


def seed_sidecar_path(workspace_id: str, session_id: str) -> Path:
    """Absolute path of the pending-seed sidecar for one stream session."""
    directory = _state_root() / workspace_id / "agent_streams"
    return directory / f"{session_id}.fork-seed.json"


def write_seed_sidecar(workspace_id: str, session_id: str, body: str) -> None:
    """Persist the seed body for ``session_id`` to consume on its first turn."""
    path = seed_sidecar_path(workspace_id, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "body": body}
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    tmp_path.replace(path)


def read_seed_sidecar(workspace_id: str, session_id: str) -> Optional[str]:
    """Return the pending seed body, or ``None`` if absent/unreadable.

    A corrupt or legacy sidecar is ignored (fail-open: the fork still works as
    a UI-history fork, matching pre-fix behavior).
    """
    path = seed_sidecar_path(workspace_id, session_id)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            payload: Any = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        logger.warning("ignoring unreadable fork-seed sidecar %s", path)
        return None
    if not isinstance(payload, dict):
        return None
    body = payload.get("body")
    return body if isinstance(body, str) and body else None


def discard_seed_sidecar(workspace_id: str, session_id: str) -> None:
    """Delete a pending-seed sidecar. Never raises (best-effort, at-most-once)."""
    path = seed_sidecar_path(workspace_id, session_id)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("failed to discard fork-seed sidecar %s", path, exc_info=True)
    # Also clean a write temp if a crash left one.
    try:
        path.with_suffix(path.suffix + ".tmp").unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
