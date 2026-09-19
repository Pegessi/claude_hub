"""Question protocol regressions from Cursor 2026.09.15 and TraeX 0.205.1."""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from claude_hub.models import (
    AgentStreamEventType,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream.base import NormalizeContext
from claude_hub.services.agent_stream.codex_jsonl import CodexJsonlAdapter, TraexJsonlAdapter
from claude_hub.services.agent_stream.cursor_cli_transcript import CursorCliTranscriptAdapter
from claude_hub.services.agent_stream.native import (
    CodexNativeSession,
    TraexNativeSession,
    codex_normalize_questions,
)


def _ctx(agent_type: AgentType = AgentType.CURSOR) -> NormalizeContext:
    return NormalizeContext(
        session_id="question-session",
        tab_id="question-tab",
        agent_type=agent_type,
        run_epoch=1,
        turn_id="question-turn",
    )


def _session(agent_type: AgentType) -> ManagedSession:
    now = datetime.now(timezone.utc)
    return ManagedSession(
        id="question-session",
        workspace_id="question-workspace",
        tab_id="question-tab",
        role=WorkspaceSessionRole.WORKER,
        agent_type=agent_type,
        status=ManagedSessionStatus.IDLE,
        title="question fixture",
        workspace_path="/tmp",
        tmux_session="question-tmux",
        target=ExecutionTarget.LOCAL,
        solo_mode=False,
        created_at=now,
        updated_at=now,
    )


def _cursor_question(subtype: str = "started") -> dict[str, Any]:
    # JSON.stringify(protobuf ToolCall) uses the oneof's camelCase member.
    return {
        "type": "tool_call",
        "subtype": subtype,
        "call_id": "ask-1",
        "tool_call": {
            "askQuestionToolCall": {
                "args": {
                    "title": "Scope",
                    "questions": [
                        {
                            "id": "scope",
                            "prompt": "Which components?",
                            "allowMultiple": True,
                            "options": [
                                {
                                    "id": "ui",
                                    "label": "Frontend",
                                    "description": "Include the question card",
                                },
                                {"id": "api", "label": "Backend"},
                            ],
                        }
                    ],
                },
                "result": {"rejected": {"reason": "Questions skipped in headless mode"}},
            }
        },
    }


def test_cursor_native_tool_records_emit_one_card_despite_headless_rejection() -> None:
    adapter, ctx = CursorCliTranscriptAdapter(), _ctx()
    started = adapter.normalize_line(_cursor_question(), ctx)
    completed = adapter.normalize_line(_cursor_question("completed"), ctx)
    assert [e.type for e in started] == [
        AgentStreamEventType.TOOL_CALL_STARTED,
        AgentStreamEventType.APPROVAL_REQUIRED,
    ]
    card = started[1]
    assert card.call_id == "ask-1"
    assert card.payload["tool_call_id"] == "ask-1"
    question = card.payload["questions"][0]
    assert question["allow_multiple"] is True
    assert question["options"][0] == {
        "id": "ui",
        "label": "Frontend",
        "description": "Include the question card",
    }
    assert [e.type for e in completed] == [AgentStreamEventType.TOOL_CALL_COMPLETED]
    assert completed[0].call_id == card.call_id
    assert adapter.normalize_line(_cursor_question(), ctx) == []


def test_cursor_completion_can_supply_question_missing_from_started_record() -> None:
    adapter, ctx = CursorCliTranscriptAdapter(), _ctx()
    incomplete = _cursor_question()
    incomplete["tool_call"]["askQuestionToolCall"]["args"] = {}
    first = adapter.normalize_line(incomplete, ctx)
    assert [e.type for e in first] == [AgentStreamEventType.TOOL_CALL_STARTED]
    completed = adapter.normalize_line(_cursor_question("completed"), ctx)
    assert [e.type for e in completed] == [
        AgentStreamEventType.APPROVAL_REQUIRED,
        AgentStreamEventType.TOOL_CALL_COMPLETED,
    ]


def test_cursor_free_text_question_and_legacy_tool_snapshot_share_card_identity() -> None:
    adapter, ctx = CursorCliTranscriptAdapter(), _ctx()
    raw = _cursor_question()
    args = raw["tool_call"]["askQuestionToolCall"]["args"]
    args["questions"] = [{"id": "details", "prompt": "Any constraints?"}]
    events = adapter.normalize_line(raw, ctx)
    assert events[1].payload["questions"][0]["options"] == []
    replay = {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "id": "ask-1", "name": "AskQuestion", "input": args}]
        },
    }
    assert adapter.normalize_line(replay, ctx) == []


@pytest.mark.parametrize("options", [None, []])
@pytest.mark.parametrize(
    "transport_type,adapter_type,agent_type",
    [
        (CodexNativeSession, CodexJsonlAdapter, AgentType.CODEX),
        (TraexNativeSession, TraexJsonlAdapter, AgentType.TRAEX),
    ],
)
async def test_free_text_question_stays_pending_and_answer_uses_same_rpc(
    options: Any, transport_type: Any, adapter_type: Any, agent_type: AgentType
) -> None:
    transport = transport_type(_session(agent_type))
    transport._send_jsonrpc_response = AsyncMock()
    request = {
        "id": 42,
        "method": "item/tool/requestUserInput",
        "params": {
            "itemId": "input-1",
            "isBlocking": True,
            "questions": [
                {
                    "id": "details",
                    "question": "Any constraints?",
                    "header": "Constraints",
                    "isSecret": True,
                    "options": options,
                }
            ],
        },
    }
    await transport._handle_server_request(request)
    transport._send_jsonrpc_response.assert_not_awaited()
    assert 42 in transport._pending_questions
    notification = await transport.read_line()
    events = adapter_type().normalize_line(notification, _ctx(agent_type))
    question = events[1].payload["questions"][0]
    assert question["options"] == []
    assert question["is_secret"] is True
    assert await transport.answer_pending_question(
        [{"questionId": "details", "selected": ["Local development only"]}]
    )
    transport._send_jsonrpc_response.assert_awaited_once_with(
        42, result={"answers": {"details": {"answers": ["Local development only"]}}}
    )
    assert transport._pending_questions == {}


def test_codex_question_option_descriptions_survive_normalization() -> None:
    questions = codex_normalize_questions(
        [
            {
                "id": "scope",
                "question": "Choose scope",
                "options": [{"label": "UI", "description": "Question cards"}],
            }
        ]
    )
    assert questions[0]["options"] == [{"id": "UI", "label": "UI", "description": "Question cards"}]


@pytest.mark.parametrize(
    "params,expected",
    [
        (
            {"state": "queued", "position": 3, "message": None},
            "Queued for model capacity (position 3).",
        ),
        (
            {"state": "waiting", "position": None, "message": "Provider queue is busy"},
            "Provider queue is busy",
        ),
        (
            {"state": "ready", "position": None, "message": None},
            "Model is ready; starting the response.",
        ),
    ],
)
def test_traex_queue_status_is_visible(params: dict[str, Any], expected: str) -> None:
    events = TraexJsonlAdapter().normalize_line(
        {"method": "queue/status", "params": params}, _ctx(AgentType.TRAEX)
    )
    assert [event.type for event in events] == [AgentStreamEventType.STATUS]
    assert events[0].payload == {
        "text": expected,
        "provider_status": "queue/status",
        "snapshot": True,
    }


def test_traex_loop_recovery_is_visible() -> None:
    events = TraexJsonlAdapter().normalize_line(
        {
            "method": "model/loopDetectedRecovering",
            "params": {"reason": "repeated output", "attempt": 2, "maxAttempts": 4},
        },
        _ctx(AgentType.TRAEX),
    )
    assert events[0].type == AgentStreamEventType.STATUS
    assert events[0].payload["text"] == (
        "Response loop detected; recovering (attempt 2/4): repeated output"
    )
