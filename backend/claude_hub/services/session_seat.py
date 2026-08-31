"""Fail-closed identity checks before injecting into a managed terminal.

A stored ``tmux_session`` string is not proof of ownership. After a rename or
stale state rewrite, that name can point at someone else's pane. ``/clear``
and review prompts must refuse in that case rather than paste blindly.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .ttyd_manager import _tmux_session_name


class SessionSeatMismatch(RuntimeError):
    """The session's stored tmux seat does not match its tab identity."""


def expected_tmux_session_name(tab_id: str) -> str:
    return _tmux_session_name(tab_id)


def should_clear_reviewer_context(
    *,
    user_requested: bool,
    previous_review_task_id: Optional[str],
    task_id: str,
) -> bool:
    """Whether to send ``/clear`` before a review prompt.

    Same reviewer + same task (reaper redispath / same-task re-review) never
    clears, even when the task still has ``clear_context=true``. A first
    assignment honors the user flag; a reviewer that last judged a different
    task still clears via the unrelated-prior-review heuristic.
    """

    if previous_review_task_id == task_id:
        return False
    return bool(user_requested) or previous_review_task_id is not None


def validate_session_seat(
    session: Any,
    *,
    processes: Mapping[str, Any] | None = None,
) -> str:
    """Return the expected tmux name or raise ``SessionSeatMismatch``.

    Checks:

    1. ``session.tmux_session`` equals ``claude-hub-{tab_id[:8]}``
    2. If this tab is live, its process tmux name matches
    3. No other live tab claims this tmux name
    """

    session_id = getattr(session, "id", "?")
    tab_id = getattr(session, "tab_id", "") or ""
    expected = expected_tmux_session_name(tab_id)
    stored = (getattr(session, "tmux_session", None) or "").strip()
    if stored != expected:
        raise SessionSeatMismatch(
            f"session {session_id} tmux_session={stored!r} != expected "
            f"{expected!r} for tab_id={tab_id}"
        )
    if not processes:
        return expected
    own = processes.get(tab_id)
    own_name = getattr(own, "tmux_session", None) if own is not None else None
    if own is not None and own_name not in (None, "", expected):
        raise SessionSeatMismatch(
            f"session {session_id} live tab {tab_id} tmux_session={own_name!r} "
            f"!= expected {expected!r}"
        )
    for other_id, other in processes.items():
        if other_id == tab_id:
            continue
        if getattr(other, "tmux_session", None) == stored:
            raise SessionSeatMismatch(
                f"session {session_id} seat {stored} is claimed by tab {other_id}"
            )
    return expected
