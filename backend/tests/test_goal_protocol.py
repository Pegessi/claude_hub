import re
from unittest.mock import Mock

import pytest

from claude_hub.services.agent_stream import goal_protocol, tailer
from claude_hub.services.agent_stream.tailer import _GoalProtocolSanitizer

_ENVELOPE = (
    '<goal-checkpoint>{"remaining":[]}</goal-checkpoint>'
    '<goal-status state="complete">done</goal-status>'
)


@pytest.mark.parametrize("split", range(1, 100))
def test_protocol_hidden_across_every_chunk_boundary_without_hiding_similar_tags(split):
    source = "Keep <goal-status-extra>visible</goal-status-extra>." + _ENVELOPE
    sanitizer = _GoalProtocolSanitizer()
    visible = sanitizer.feed(source[:split]) + sanitizer.feed(source[split:], final=True)
    assert visible == "Keep <goal-status-extra>visible</goal-status-extra>."


def test_unclosed_control_attributes_do_not_accumulate_unbounded_buffer():
    sanitizer = _GoalProtocolSanitizer()
    assert sanitizer.feed("Visible<goal-status ") == "Visible"
    for _ in range(100):
        assert sanitizer.feed("x" * 1000) == ""
    assert len(sanitizer._pending) < 32


_MID_EXAMPLE = 'Example: <goal-status state="complete">done</goal-status> appears mid-answer.\n'


@pytest.mark.parametrize("split", range(1, len(_MID_EXAMPLE) + len(_ENVELOPE)))
def test_trailing_only_keeps_mid_text_examples_and_drops_the_trailing_envelope(split):
    source = _MID_EXAMPLE + _ENVELOPE + "\n"
    sanitizer = _GoalProtocolSanitizer(trailing_only=True)
    visible = sanitizer.feed(source[:split]) + sanitizer.feed(source[split:], final=True)
    assert visible == _MID_EXAMPLE


@pytest.mark.parametrize(
    "text",
    [
        'Parse <goal-status state="complete"> openers carefully.\n\nRest of the answer.',
        "Use `<goal-checkpoint>` then the rest of the answer.",
        "```\n" + _ENVELOPE + "\n```\nAfter the block.",
        "Ends with an unfinished <goal-status state",
        '<goal-status state="complete">done</goal-status> and more text.',
        "Odd tags <goal-status …> and <goal-checkpoint >{}</goal-checkpoint> stay.",
    ],
)
def test_trailing_only_releases_anything_that_is_not_the_trailing_envelope(text):
    sanitizer = _GoalProtocolSanitizer(trailing_only=True)
    streamed = "".join(sanitizer.feed(char) for char in text)
    assert streamed + sanitizer.feed("", final=True) == text


def test_trailing_only_releases_prose_promptly_and_bounds_an_unclosed_status():
    sanitizer = _GoalProtocolSanitizer(trailing_only=True)
    assert sanitizer.feed("Use `<goal-checkpoint>` then") == "Use `<goal-checkpoint>` then"
    assert sanitizer.feed(' <goal-status state="complete">') == " "
    assert sanitizer.feed("x" * 5000).endswith("x" * 5000)
    assert sanitizer._pending == ""


def test_trailing_only_drops_a_status_without_checkpoint_and_keeps_text_before_it():
    sanitizer = _GoalProtocolSanitizer(trailing_only=True)
    visible = sanitizer.feed('Done.\n<goal-status state="needs_input">pick one</goal-status>')
    assert visible == "Done.\n"
    assert sanitizer.feed("  \n", final=True) == ""


@pytest.mark.parametrize(
    "name",
    [
        "_GOAL_STATUS_TERMINAL_RE",
        "_GOAL_TAIL_SCAN_CHARS",
        "_STRICT_STATUS_OPENER_RE",
        "_CHECKPOINT_OPENER",
        "_MAX_STATUS_BODY_CHARS",
    ],
)
def test_tailer_preserves_goal_parser_import_identity(name: str) -> None:
    assert getattr(tailer, name) is getattr(goal_protocol, name)


@pytest.mark.parametrize(
    ("text", "state"),
    [
        (_ENVELOPE, "complete"),
        (_ENVELOPE.upper() + " \n", "complete"),
        ('<goal-status state="needs_input">pick one</goal-status>', "complete"),
        ("<goal-check", "partial"),
        ("<goal-checkpoint>{", "partial"),
        ("<goal-checkpoint>{}</goal-checkpoint><goal-stat", "partial"),
        ('<goal-status state="complete">pending', "partial"),
        (_ENVELOPE + " visible", "no"),
        ("<goal-checkpoint>example</goal-checkpoint>", "no"),
        ('<goal-status state="complete"><goal-status', "no"),
        ('<goal-status state="complete">' + "x" * 4097, "no"),
        ("<goal-checkpoint>{" + "x" * goal_protocol._GOAL_TAIL_SCAN_CHARS, "no"),
    ],
    ids=[
        "complete",
        "case-insensitive",
        "status-only",
        "partial-opener",
        "partial-checkpoint",
        "partial-status-after-checkpoint",
        "partial-status-body",
        "following-prose",
        "checkpoint-prose",
        "nested-status",
        "unbounded-status",
        "unbounded-checkpoint",
    ],
)
def test_trailing_envelope_classification(text: str, state: str) -> None:
    assert goal_protocol._trailing_envelope_state(text) == state
    assert tailer._trailing_envelope_state(text) == state


@pytest.mark.parametrize("trailing_only", [False, True])
def test_sanitizers_keep_independent_stream_buffers(trailing_only: bool) -> None:
    first = _GoalProtocolSanitizer(trailing_only=trailing_only)
    second = _GoalProtocolSanitizer(trailing_only=trailing_only)

    assert first.feed("First. <goal-sta") == "First. "
    assert second.feed("Second.", final=True) == "Second."
    assert first.feed('tus state="complete">done</goal-status>', final=True) == ""


def test_legacy_sanitizer_keeps_module_identity() -> None:
    assert _GoalProtocolSanitizer is tailer._GoalProtocolSanitizer
    assert _GoalProtocolSanitizer.__module__ == tailer.__name__


@pytest.mark.parametrize(
    ("name", "value", "text", "initial_state"),
    [
        ("_GOAL_TAIL_SCAN_CHARS", 8, _ENVELOPE, "complete"),
        (
            "_MAX_STATUS_BODY_CHARS",
            1,
            '<goal-status state="complete">body',
            "partial",
        ),
        (
            "_CHECKPOINT_OPENER",
            "<goal-checkpoint blocked>",
            "<goal-checkpoint>{}</goal-checkpoint>",
            "partial",
        ),
        ("_STRICT_STATUS_OPENER_RE", re.compile(r"(?!)"), _ENVELOPE, "complete"),
    ],
    ids=["tail-window", "status-body", "checkpoint-opener", "status-pattern"],
)
def test_legacy_constant_patch_affects_classifier_and_existing_sanitizer(
    monkeypatch: pytest.MonkeyPatch, name: str, value: object, text: str, initial_state: str
) -> None:
    sanitizer = _GoalProtocolSanitizer(trailing_only=True)
    assert tailer._trailing_envelope_state(text) == initial_state
    assert sanitizer.feed(text) == ""

    monkeypatch.setattr(tailer, name, value)

    assert tailer._trailing_envelope_state(text) == "no"
    assert sanitizer.feed("") == text
    assert goal_protocol._trailing_envelope_state(text) == initial_state


def test_legacy_classifier_patch_affects_existing_sanitizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sanitizer = _GoalProtocolSanitizer(trailing_only=True)
    text = '<goal-status state="complete">done</goal-status>'
    assert sanitizer.feed(text) == ""
    classifier = Mock(return_value="no")
    monkeypatch.setattr(tailer, "_trailing_envelope_state", classifier)

    assert sanitizer.feed("") == text
    classifier.assert_called_once_with(text)
    assert goal_protocol._trailing_envelope_state(text) == "complete"


@pytest.mark.parametrize("replace_factory", [False, True])
def test_legacy_opener_patch_affects_existing_sanitizer(
    monkeypatch: pytest.MonkeyPatch, replace_factory: bool
) -> None:
    sanitizer = _GoalProtocolSanitizer(trailing_only=True)
    assert sanitizer.feed("Visible <goal-sta") == "Visible "
    if replace_factory:
        monkeypatch.setattr(tailer, "_GoalProtocolSanitizer", Mock(_OPENERS=("<custom",)))
    else:
        monkeypatch.setattr(tailer._GoalProtocolSanitizer, "_OPENERS", ("<custom",))

    assert sanitizer.feed("") == "<goal-sta"
    assert goal_protocol._GoalProtocolSanitizer._OPENERS == ("<goal-checkpoint", "<goal-status")


def test_goal_helper_does_not_own_runtime_state() -> None:
    for name in (
        "SessionTailer",
        "TailerManager",
        "_HARD_FAILED_SESSION_IDS",
        "_DELETION_FENCED_SESSION_IDS",
        "_PROVIDER_FENCED_SESSION_IDS",
        "_RETIRING_TAILER_IDS",
        "_TAILER_MANAGERS",
    ):
        assert not hasattr(goal_protocol, name)
