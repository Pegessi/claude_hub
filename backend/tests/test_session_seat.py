"""Seat checks and reviewer /clear policy. No live Hub state or tmux."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from claude_hub.services.session_seat import (
    SessionSeatMismatch,
    expected_tmux_session_name,
    should_clear_reviewer_context,
    validate_session_seat,
)


def test_expected_tmux_name_uses_first_eight_tab_chars() -> None:
    assert expected_tmux_session_name("abcdef12-rest") == "claude-hub-abcdef12"


def test_validate_session_seat_accepts_matching_name() -> None:
    tab_id = "aabbccdd-xxxx"
    session = SimpleNamespace(
        id="sess-1",
        tab_id=tab_id,
        tmux_session="claude-hub-aabbccdd",
    )
    assert validate_session_seat(session) == "claude-hub-aabbccdd"


def test_validate_session_seat_rejects_renamed_target() -> None:
    session = SimpleNamespace(
        id="reviewer-2",
        tab_id="18bfa748-reviewer",
        tmux_session="claude-hub-bea8f4d8",
    )
    with pytest.raises(SessionSeatMismatch, match="tmux_session="):
        validate_session_seat(session)


def test_validate_session_seat_rejects_other_tab_claiming_name() -> None:
    tab_id = "11111111-reviewer"
    session = SimpleNamespace(
        id="reviewer-2",
        tab_id=tab_id,
        tmux_session="claude-hub-11111111",
    )
    processes = {
        tab_id: SimpleNamespace(tmux_session="claude-hub-11111111"),
        "bea8f4d8-main": SimpleNamespace(tmux_session="claude-hub-11111111"),
    }
    with pytest.raises(SessionSeatMismatch, match="claimed by tab"):
        validate_session_seat(session, processes=processes)


@pytest.mark.parametrize(
    ("user_requested", "previous", "task_id", "expected"),
    [
        (True, None, "task-a", True),
        (False, None, "task-a", False),
        (True, "other-task", "task-a", True),
        (False, "other-task", "task-a", True),
        (True, "task-a", "task-a", False),
        (False, "task-a", "task-a", False),
    ],
)
def test_should_clear_reviewer_context(
    user_requested: bool,
    previous: str | None,
    task_id: str,
    expected: bool,
) -> None:
    assert (
        should_clear_reviewer_context(
            user_requested=user_requested,
            previous_review_task_id=previous,
            task_id=task_id,
        )
        is expected
    )
