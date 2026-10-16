"""A MASO failure must read as a statement, never as a Python repr.

MASO sends ``agent_error`` as ``str(exception)``. When the provider's failure
frame is the exception argument that string is a repr of a mapping, which is
unreadable and may carry no description at all. Hub cannot invent the cause, so
it must say what it actually knows and keep the frame as evidence.
"""

from typing import Any, Dict, List

from claude_hub.models import AgentStreamEvent, AgentStreamEventType, AgentType
from claude_hub.services.agent_stream.base import NormalizeContext
from claude_hub.services.agent_stream.maso_events import MasoEventAdapter
from claude_hub.services.agent_stream.redaction import redact_event

# The exact string the provider produced in the reported session: an SSE error
# frame whose description fields were never set.
DESCRIPTIONLESS_FRAME = "{'sequence_number': 1, 'type': 'error'}"


def normalize(**data: Any) -> List[AgentStreamEvent]:
    ctx = NormalizeContext(
        session_id="s",
        tab_id="t",
        agent_type=AgentType.MASO,
        run_epoch=1,
        turn_id="turn-1",
    )
    agent = data.pop("agent_id", "main")
    return MasoEventAdapter().normalize_line(
        {"type": "agent_error", "root_agent_id": "main", "data": {"agent_id": agent, **data}},
        ctx,
    )


def failure(**data: Any) -> Dict[str, Any]:
    """The payload a reader actually receives, after server-side redaction."""
    events = normalize(**data)
    assert len(events) == 1
    return redact_event(events[0]).payload


def test_descriptionless_provider_frame_is_explained_instead_of_echoed() -> None:
    events = normalize(error=DESCRIPTIONLESS_FRAME, stop_reason="error")
    assert events[0].type is AgentStreamEventType.ERROR
    payload = redact_event(events[0]).payload
    assert payload["message"] == (
        "MASO run failed and reported no error description. "
        "Retry the turn; if it repeats, check the MASO service logs."
    )
    # The frame stays as secondary evidence, as JSON rather than a repr.
    assert payload["detail"] == '{"sequence_number": 1, "type": "error"}'
    assert payload["stop_reason"] == "error"


def test_provider_description_is_recovered_from_a_stringified_frame() -> None:
    payload = failure(error="{'code': 2005, 'message': 'Rate limit exceeded'}")
    assert payload["message"] == "MASO run failed: Rate limit exceeded"
    assert payload["detail"] == '{"code": 2005, "message": "Rate limit exceeded"}'


def test_nested_provider_description_is_recovered() -> None:
    payload = failure(error="{'error': {'code': 500, 'message': 'model overloaded'}}")
    assert payload["message"] == "MASO run failed: model overloaded"


def test_ordinary_error_text_is_preserved_verbatim() -> None:
    payload = failure(error="Connection refused by the model gateway")
    assert payload["message"] == "MASO run failed: Connection refused by the model gateway"
    # Prose is already the whole description; there is nothing left to attach.
    assert "detail" not in payload


def test_prose_that_merely_resembles_a_mapping_is_still_prose() -> None:
    payload = failure(error="{unterminated frame")
    assert payload["message"] == "MASO run failed: {unterminated frame"
    assert "detail" not in payload
    # Nesting beyond the parser's limit is a SyntaxError, not a crash.
    deep = "{" + '"a": {' * 500 + "}" * 500 + "}"
    assert failure(error=deep)["message"] == f"MASO run failed: {deep}"
    # A set literal parses but is not a failure frame.
    assert failure(error="{'offline'}")["message"] == "MASO run failed: {'offline'}"


def test_non_string_error_never_leaks_a_python_repr() -> None:
    for value in ({"sequence_number": 1, "type": "error"}, ["a", "b"], 12345, True):
        payload = failure(error=value)
        assert payload["message"].startswith("MASO run failed and reported no error description.")
        assert "'" not in payload["message"]
        assert "'" not in payload["detail"]
    assert failure(error=["a", "b"])["detail"] == '["a", "b"]'
    assert failure(error=12345)["detail"] == "12345"


def test_a_missing_error_field_reports_the_absence_without_inventing_a_cause() -> None:
    payload = failure(stop_reason="error")
    assert payload["message"].startswith("MASO run failed and reported no error description.")
    assert "detail" not in payload


def test_stop_reason_is_named_only_when_it_adds_something() -> None:
    # MASO pins stop_reason to "error" at this hook, so repeating it is noise.
    assert "stop reason" not in failure(error="boom", stop_reason="error")["message"]
    # A different value means MASO's contract moved; the reader must see it.
    unexpected = failure(error="boom", stop_reason="max_turns")
    assert unexpected["message"] == "MASO run failed (stop reason: max_turns): boom"
    assert unexpected["stop_reason"] == "max_turns"


def test_absent_or_unusable_stop_reason_is_omitted_rather_than_guessed() -> None:
    # Fields may be missing or disagree; never synthesize one that was not sent.
    assert "stop_reason" not in failure(error="boom")
    assert "stop_reason" not in failure(error="boom", stop_reason=None)
    assert "stop_reason" not in failure(error="boom", stop_reason=7)
    assert "stop reason" not in failure(error="boom", stop_reason=7)["message"]


def test_child_failure_stays_a_status_carrying_the_same_explanation() -> None:
    events = normalize(agent_id="child", error=DESCRIPTIONLESS_FRAME, stop_reason="error")
    assert events[0].type is AgentStreamEventType.STATUS
    payload = redact_event(events[0]).payload
    # A child failure is context in the child's timeline, not a terminal error,
    # and the parent Chat cannot be retried on its behalf.
    assert payload["text"] == "MASO sub-agent failed and reported no error description."
    assert payload["provider_status"] == "error"
    assert payload["detail"] == '{"sequence_number": 1, "type": "error"}'
    assert events[0].payload["subagent_thread"] == "child"


def test_redaction_keeps_the_failure_readable() -> None:
    # The reported event carried ``redacted: true``; that marker must not be
    # read as "the description was removed".
    events = normalize(error=DESCRIPTIONLESS_FRAME)
    redacted = redact_event(events[0])
    assert redacted.redacted is True
    assert redacted.payload == events[0].payload
