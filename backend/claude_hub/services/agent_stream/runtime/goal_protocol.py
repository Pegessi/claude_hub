"""Bounded Goal protocol parsing for streamed assistant text.

This module owns only parsing and display buffers. SessionTailer retains raw
assistant text, completion timing, persistence, and provider lifecycle state.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

# Mirrors the Goal controller's terminal signal: a turn whose raw assistant
# text ends with a complete ``goal-status`` block has produced the model-side
# end of turn even if the provider never follows with a completion record.
# Keep this aligned with the controller's trailing-signal contract: the same
# raw text is handed to the Goal observer on a normal completion, so synthesis
# must not accept an envelope the controller would itself reject.
_GOAL_STATUS_TERMINAL_RE = re.compile(
    r'<goal-status\s+state=["\'](?:continue|complete|blocked|needs_input)["\']\s*>'
    r".*?</goal-status>\s*$",
    re.IGNORECASE | re.DOTALL,
)
# Only scan the bounded tail of accumulated assistant text; the terminal signal
# is required to be the trailing block of the turn. This must stay equal to the
# Goal controller's own protocol window
# (``_MAX_CHECKPOINT_JSON_CHARS + 4096`` in goal_run/controller.py), so the
# tailer never recognizes an envelope that the controller cannot later see.
_GOAL_TAIL_SCAN_CHARS = 64 * 1024 + 4096


_STRICT_STATUS_OPENER_RE = re.compile(r'<goal-status\s+state=["\'][a-z_]+["\']\s*>', re.IGNORECASE)
_CHECKPOINT_OPENER = "<goal-checkpoint>"
# A trailing status block is a one-line signal; an unclosed opener with more
# text than this is prose that mentions the tag.
_MAX_STATUS_BODY_CHARS = 4096


def _trailing_envelope_state(
    text: str,
    *,
    tail_scan_chars: int = _GOAL_TAIL_SCAN_CHARS,
    checkpoint_opener: str = _CHECKPOINT_OPENER,
    status_opener_re: re.Pattern[str] = _STRICT_STATUS_OPENER_RE,
    max_status_body_chars: int = _MAX_STATUS_BODY_CHARS,
) -> str:
    """Classify ``text`` (starting at a protocol opener) as a trailing envelope.

    Returns ``"complete"`` when it is an optional checkpoint followed by a
    status block and only whitespace, ``"partial"`` while more text could still
    make it one, and ``"no"`` otherwise.  The shape matches the frontend's
    ``stripTrailingGoalProtocol`` and the controller's trailing signal.
    """
    if len(text) > tail_scan_chars:
        return "no"
    rest = text
    lowered = rest.lower()
    if lowered.startswith("<goal-checkpoint"):
        if len(rest) < len(checkpoint_opener):
            return "partial" if checkpoint_opener.startswith(lowered) else "no"
        if not lowered.startswith(checkpoint_opener):
            return "no"
        body = lowered[len(checkpoint_opener) :]
        if body.lstrip() and not body.lstrip().startswith("{"):
            return "no"
        end = body.find("</goal-checkpoint>")
        if "<goal-checkpoint" in (body if end < 0 else body[:end]):
            return "no"
        if end < 0:
            return "partial"
        rest = rest[len(checkpoint_opener) + end + len("</goal-checkpoint>") :].lstrip()
        lowered = rest.lower()
        if not lowered.startswith("<goal-status"):
            return "partial" if "<goal-status".startswith(lowered) else "no"
    opener = status_opener_re.match(rest)
    if opener is None:
        return "partial" if ">" not in rest and len(rest) <= 64 else "no"
    body = lowered[opener.end() :]
    end = body.find("</goal-status>")
    if "<goal-status" in (body if end < 0 else body[:end]):
        return "no"
    if end < 0:
        return "partial" if len(body) <= max_status_body_chars else "no"
    return "complete" if not body[end + len("</goal-status>") :].strip() else "no"


class _GoalProtocolSanitizer:
    """Incrementally remove Goal control blocks from visible assistant text.

    Provider deltas may split either tag at any byte boundary.  The sanitizer
    therefore retains only a short possible opener/closer suffix between
    calls, while the tailer separately retains the complete raw assistant text
    for the in-process Goal observer.

    Goal turns drop every control block.  With ``trailing_only`` (ordinary
    turns in a Chat that has run a Goal), text from an opener is held until it
    is known to be the reply's trailing envelope, which is dropped; anything
    else is released unchanged.
    """

    _OPENERS = ("<goal-checkpoint", "<goal-status")
    _OPENING_TAG = re.compile(r"<goal-(checkpoint|status)(?=[\s>])", re.IGNORECASE)
    _CLOSERS = {"checkpoint": "</goal-checkpoint>", "status": "</goal-status>"}

    def __init__(self, *, trailing_only: bool = False) -> None:
        self._pending = ""
        self._inside: Optional[str] = None
        self._trailing_only = trailing_only

    @staticmethod
    def _possible_opener_suffix(text: str, *, openers: Optional[Tuple[str, ...]] = None) -> int:
        lowered = text.lower()
        best = 0
        for opener in _GoalProtocolSanitizer._OPENERS if openers is None else openers:
            for length in range(1, min(len(lowered), len(opener)) + 1):
                if lowered.endswith(opener[:length]):
                    best = max(best, length)
        return best

    def _envelope_state(self, text: str) -> str:
        return _trailing_envelope_state(text)

    def _feed_trailing(self, final: bool) -> str:
        visible: List[str] = []
        while self._pending:
            opener = self._OPENING_TAG.search(self._pending)
            if opener is None:
                keep = 0 if final else self._possible_opener_suffix(self._pending)
                emit_end = len(self._pending) - keep
                visible.append(self._pending[:emit_end])
                self._pending = self._pending[emit_end:]
                break
            visible.append(self._pending[: opener.start()])
            self._pending = self._pending[opener.start() :]
            state = self._envelope_state(self._pending)
            if state == "complete" and final:
                self._pending = ""
                break
            if state == "no" or final:
                visible.append(self._pending[: opener.end()])
                self._pending = self._pending[opener.end() :]
                continue
            break
        return "".join(visible)

    def feed(self, text: str, *, final: bool = False) -> str:
        self._pending += text
        if self._trailing_only:
            return self._feed_trailing(final)
        visible: List[str] = []
        while self._pending:
            lowered = self._pending.lower()
            if self._inside is not None:
                closer = self._CLOSERS[self._inside]
                close_at = lowered.find(closer)
                if close_at < 0:
                    # Raw text is retained by ``_assistant_text``; only keep a
                    # possible split closing tag here so malformed/large blocks
                    # cannot grow this display-only buffer without bound.
                    keep = min(len(self._pending), len(closer) - 1)
                    self._pending = self._pending[-keep:] if keep else ""
                    break
                self._pending = self._pending[close_at + len(closer) :]
                self._inside = None
                continue

            opener = self._OPENING_TAG.search(self._pending)
            if opener:
                visible.append(self._pending[: opener.start()])
                self._pending = self._pending[opener.end() :]
                # The delimiter establishes a control block. Its opening
                # attributes need not accumulate while awaiting a closing tag.
                self._inside = opener.group(1).lower()
                continue

            if final:
                # Do not reveal a truncated control opener at EOF. Ordinary
                # prose before that prefix remains visible.
                keep = self._possible_opener_suffix(self._pending)
                visible.append(
                    self._pending[: len(self._pending) - keep] if keep else self._pending
                )
                self._pending = ""
                break
            keep = self._possible_opener_suffix(self._pending)
            emit_end = len(self._pending) - keep
            visible.append(self._pending[:emit_end])
            self._pending = self._pending[emit_end:]
            break

        return "".join(visible)
