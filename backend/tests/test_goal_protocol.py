import pytest

from claude_hub.services.agent_stream.tailer import _GoalProtocolSanitizer


@pytest.mark.parametrize("split", range(1, 100))
def test_protocol_hidden_across_every_chunk_boundary_without_hiding_similar_tags(split):
    source = (
        "Keep <goal-status-extra>visible</goal-status-extra>."
        '<goal-checkpoint>{"remaining":[]}</goal-checkpoint>'
        '<goal-status state="complete">done</goal-status>'
    )
    sanitizer = _GoalProtocolSanitizer()
    visible = sanitizer.feed(source[:split]) + sanitizer.feed(source[split:], final=True)
    assert visible == "Keep <goal-status-extra>visible</goal-status-extra>."


def test_unclosed_control_attributes_do_not_accumulate_unbounded_buffer():
    sanitizer = _GoalProtocolSanitizer()
    assert sanitizer.feed("Visible<goal-status ") == "Visible"
    for _ in range(100):
        assert sanitizer.feed("x" * 1000) == ""
    assert len(sanitizer._pending) < 32
