import pytest

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
