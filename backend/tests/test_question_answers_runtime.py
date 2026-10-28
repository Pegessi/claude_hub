"""Exact-answer runtime ownership, web equivalence and optional inbox failure."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from claude_hub.api import agent_stream as stream
from claude_hub.models import AgentStreamEvent, AgentStreamEventType, AgentType, User
from claude_hub.models.chat_message import QuestionAnswerCreate, utc_now
from claude_hub.services.agent_stream.question_answers import QuestionAnswers
from claude_hub.services.chat_message_inbox import (
    ChatMessageInbox,
    InboxError,
    InboxEvidence,
    InboxTarget,
)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    service = QuestionAnswers()
    snapshot = {
        "questions": [
            {"id": "0", "prompt": "Run now?", "options": [{"id": "Run now"}, {"id": "Skip"}]}
        ],
        "mode": "native",
        "kind": "ask_question",
        "incarnation": "runtime-A",
        "run_epoch": 7,
    }
    session = SimpleNamespace(id="terminal-tab-tab", tab_id="tab")
    tailer = SimpleNamespace(
        question_snapshot=lambda turn, call: (
            snapshot if (turn, call) == ("turn-A", "call-A") else None
        )
    )
    manager = SimpleNamespace(
        answer_pending_question=AsyncMock(return_value=True),
        turn_in_flight=AsyncMock(return_value=False),
        resolve_answered_questions=AsyncMock(),
    )
    monkeypatch.setattr(service, "context", lambda sid: (session, manager, tailer))

    async def submit(item, before):
        await service.submit(item, before)

    inbox = ChatMessageInbox(
        tmp_path / "inbox.json",
        lambda tab: InboxTarget("inc"),
        AsyncMock(),
        AsyncMock(return_value=InboxEvidence("absent")),
    )
    inbox.answer_submit = submit
    monkeypatch.setattr(service, "inbox", lambda: inbox)
    monkeypatch.setattr(stream.ttyd_manager, "get_tab", lambda tab: SimpleNamespace(archived=False))
    from claude_hub.api.routes.agent_stream import dispatch

    monkeypatch.setattr(dispatch, "require_tab_input_available", lambda tab: None)
    return service, inbox, session, manager, snapshot


def payload(**overrides):
    values = dict(
        text=json.dumps(
            {
                "type": "ask_question_response",
                "answers": [{"questionId": "0", "selected": ["Run now"]}],
            }
        ),
        client_turn_id="web-call",
        question_turn_id="turn-A",
        question_call_id="call-A",
        attachments=[],
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_web_and_cli_admit_same_exact_response(runtime):
    service, inbox, _, manager, _ = runtime
    result = await service.web(
        "terminal-tab-tab", payload(), User(open_id="local", name="local", email="")
    )
    receipt = result["receipt"]
    assert receipt["state"] == "queued"
    await inbox.pump_answer(receipt["message_id"])
    manager.answer_pending_question.assert_awaited_once()
    args, kwargs = manager.answer_pending_question.call_args
    assert args[2] == "turn-A"
    assert kwargs["expected_call_id"] == "call-A"
    assert json.loads(args[1])["answers"] == [{"questionId": "0", "selected": ["Run now"]}]
    assert inbox.questions[next(iter(inbox.questions))].state == "resolved"
    retry = await service.web(
        "terminal-tab-tab", payload(), User(open_id="local", name="local", email="")
    )
    assert retry["receipt"]["message_id"] == receipt["message_id"]


@pytest.mark.asyncio
async def test_stale_call_never_falls_through(runtime):
    service, _, _, manager, _ = runtime
    with pytest.raises(HTTPException) as exc:
        await service.web(
            "terminal-tab-tab",
            payload(question_call_id="old-call"),
            User(open_id="local", name="local", email=""),
        )
    assert exc.value.status_code == 409
    manager.answer_pending_question.assert_not_called()


@pytest.mark.asyncio
async def test_provider_false_is_failed_not_new_turn(runtime):
    service, inbox, _, manager, _ = runtime
    manager.answer_pending_question.return_value = False
    q = await service.capture("terminal-tab-tab", "turn-A", "call-A")
    receipt = await service.admit(
        q,
        User(open_id="local", name="local", email=""),
        QuestionAnswerCreate(call_id="click", selected=["Skip"]),
    )
    await inbox.pump_answer(receipt.message_id)
    assert inbox.messages[receipt.message_id].state == "uncertain"
    manager.resolve_answered_questions.assert_not_called()


@pytest.mark.asyncio
async def test_reincarnation_before_write_rejects_without_submission(runtime):
    service, inbox, _, manager, snapshot = runtime
    q = await service.capture("terminal-tab-tab", "turn-A", "call-A")
    receipt = await service.admit(
        q,
        User(open_id="local", name="local", email=""),
        QuestionAnswerCreate(call_id="answer", selected=["Skip"]),
    )
    snapshot["incarnation"] = "replacement"
    await inbox.pump_answer(receipt.message_id)
    assert inbox.messages[receipt.message_id].state == "failed"
    assert not inbox.messages[receipt.message_id].submission_started
    manager.answer_pending_question.assert_not_called()


@pytest.mark.asyncio
async def test_placeholder_completion_keeps_followup_stop_invalidates(runtime):
    service, inbox, session, _, snapshot = runtime
    snapshot["mode"] = "followup"
    q = await service.capture(session.id, "turn-A", "call-A")

    def event(status):
        return AgentStreamEvent(
            stream_sequence=1,
            session_id=session.id,
            tab_id="tab",
            agent_type=AgentType.CLAUDE,
            type=AgentStreamEventType.TURN_COMPLETED,
            turn_id="turn-A",
            payload={"status": status},
            created_at=utc_now(),
        )

    await service.observe(event("completed"))
    assert q.state == "open"
    await service.observe(event("cancelled"))
    assert q.state == "stale"


@pytest.mark.parametrize("flag", ["is_secret", "allow_multiple"])
@pytest.mark.asyncio
async def test_unsupported_remote_shape_never_registered(runtime, flag):
    service, inbox, _, _, snapshot = runtime
    snapshot["questions"][0][flag] = True
    with pytest.raises(InboxError, match="Web Chat"):
        await service.capture("terminal-tab-tab", "turn-A", "call-A")
    assert not inbox.questions


@pytest.mark.asyncio
async def test_corrupt_optional_inbox_keeps_exact_native_web_working(runtime, monkeypatch):
    service, _, _, manager, _ = runtime

    def broken():
        raise RuntimeError("invalid inbox")

    monkeypatch.setattr(service, "inbox", broken)
    assert (
        await service.web(
            "terminal-tab-tab", payload(), User(open_id="local", name="local", email="")
        )
    )["answered_question"]
    manager.answer_pending_question.assert_awaited_once()


@pytest.mark.asyncio
async def test_claim_blocks_ordinary_turn_but_not_own_answer(runtime):
    service, inbox, _, _, _ = runtime
    q = await service.capture("terminal-tab-tab", "turn-A", "call-A")
    r = await service.admit(
        q,
        User(open_id="local", name="local", email=""),
        QuestionAnswerCreate(call_id="answer", selected=["Skip"]),
    )
    with pytest.raises(HTTPException, match="owns"):
        service.guard_input(q.session_id, "unrelated")
    service.guard_input(q.session_id, r.turn_id)


@pytest.mark.asyncio
async def test_followup_uses_existing_goal_admission_without_reacquiring(runtime, monkeypatch):
    service, inbox, _, manager, snapshot = runtime
    snapshot["mode"] = "followup"
    from claude_hub.api.routes.agent_stream import dispatch
    from claude_hub.services.goal_run import get_goal_admission_lock

    async def goal_dispatch(tab, payload, bindings, **kwargs):
        assert get_goal_admission_lock(tab).locked()
        assert kwargs["goal_locked"]
        return payload.client_turn_id

    monkeypatch.setattr(dispatch, "dispatch_tab_stream_input", goal_dispatch)
    q = await service.capture("terminal-tab-tab", "turn-A", "call-A")
    r = await service.admit(
        q,
        User(open_id="local", name="local", email=""),
        QuestionAnswerCreate(call_id="answer", selected=["Skip"]),
    )
    await asyncio.wait_for(inbox.pump_answer(r.message_id), 1)
    assert inbox.messages[r.message_id].state == "completed"
    manager.resolve_answered_questions.assert_awaited_once()


@pytest.mark.asyncio
async def test_managed_followup_keeps_exact_mailbox_owner(runtime, monkeypatch):
    service, inbox, session, manager, snapshot = runtime
    session.id = "managed-A"
    snapshot["mode"] = "followup"

    @asynccontextmanager
    async def guard(value, **kwargs):
        assert value is session
        yield value

    monkeypatch.setattr(stream._agent_stream_bindings, "managed_stream_interaction", guard)
    mailbox = AsyncMock()
    monkeypatch.setattr(stream.workspace_manager, "send_chat_session_message_locked", mailbox)
    q = await service.capture(session.id, "turn-A", "call-A")
    r = await service.admit(
        q,
        User(open_id="local", name="local", email=""),
        QuestionAnswerCreate(call_id="same-call", selected=["Skip"]),
    )
    await inbox.pump_answer(r.message_id)
    assert inbox.messages[r.message_id].state == "completed"
    args, kwargs = mailbox.call_args
    assert args[0] == "managed-A"
    assert kwargs["question_key"] == '["turn-A","call-A"]'
    assert kwargs["call_id"] == "same-call"


def test_actual_runtime_snapshot_rejects_newer_followup_epoch_and_stopped_native():
    from claude_hub.services.agent_stream.runtime.input import InputMixin
    from claude_hub.services.agent_stream.runtime.values import _PendingApproval

    session = SimpleNamespace(agent_type=AgentType.CLAUDE)
    runtime = SimpleNamespace(
        _require_startable_incarnation=lambda: None,
        _pending_approvals={"call": _PendingApproval("call", "turn", 1, questions=({"id": "0"},))},
        _session_getter=lambda: session,
        _run_epoch=1,
        _active_turn_id="turn",
        _native_transport=SimpleNamespace(turn_in_flight=True),
        question_incarnation="inc",
    )
    assert InputMixin.question_snapshot(runtime, "turn", "call") is not None
    runtime._run_epoch = 2
    assert InputMixin.question_snapshot(runtime, "turn", "call") is None
    session.agent_type = AgentType.CODEX
    runtime._native_transport.turn_in_flight = False
    assert InputMixin.question_snapshot(runtime, "turn", "call") is None


@pytest.mark.asyncio
async def test_native_response_pipe_failure_is_not_reported_as_answered():
    from claude_hub.services.agent_stream.providers.codex_rpc import CodexProtocolSession

    class BrokenWriter:
        def write(self, value):
            raise BrokenPipeError

    owner = SimpleNamespace(_process=SimpleNamespace(stdin=BrokenWriter()))
    with pytest.raises(RuntimeError, match="uncertain"):
        await CodexProtocolSession._send_jsonrpc_response(owner, 42, result={"answers": {}})


@pytest.mark.asyncio
async def test_ordinary_admission_and_answer_claim_share_goal_lock(runtime):
    service, inbox, _, _, snapshot = runtime
    from claude_hub.services.goal_run import get_goal_admission_lock

    q = await service.capture("terminal-tab-tab", "turn-A", "call-A")
    async with get_goal_admission_lock("tab"):
        claim = asyncio.create_task(
            service.admit(
                q,
                User(open_id="local", name="local", email=""),
                QuestionAnswerCreate(call_id="answer", selected=["Skip"]),
            )
        )
        await asyncio.sleep(0)
        assert not claim.done()
        # The ordinary turn admitted first: it advances runtime ownership before
        # releasing its existing admission lock. No answer may claim the old epoch.
        snapshot["run_epoch"] += 1
    with pytest.raises(InboxError, match="pending"):
        await claim
    assert not inbox.messages


@pytest.mark.asyncio
async def test_feishu_binding_gate_covers_awaited_probe_and_submit(runtime, monkeypatch):
    service, inbox, _, _, snapshot = runtime
    from claude_hub.api import feishu_bot as bot
    from claude_hub.api.routes.agent_stream import dispatch
    from claude_hub.api.routes.feishu_bot import card_actions
    from claude_hub.models.chat_message import QuestionMirror

    snapshot["mode"] = "followup"
    q = await service.capture("terminal-tab-tab", "turn-A", "call-A")
    q.mirror = QuestionMirror(
        bot_id="owned-gate-test",
        app_id="app",
        tenant_key="tenant",
        pairing_id="pair",
        generation=1,
        revision=1,
        chat_id="chat",
        sender_open_id="sender",
        send_uuid="send",
    )
    changed = False
    entered, release = asyncio.Event(), asyncio.Event()

    async def busy_probe(session):
        entered.set()
        await release.wait()
        return False

    _, _, _, manager, _ = runtime
    manager.turn_in_flight.side_effect = busy_probe
    monkeypatch.setattr(card_actions, "current_binding", lambda q: (None, None))

    async def deliver(*args, **kwargs):
        assert not changed
        assert bot._bot_gates["owned-gate-test"].lock.locked()

    monkeypatch.setattr(dispatch, "dispatch_tab_stream_input", deliver)
    receipt = await service.admit(
        q,
        User(open_id="local", name="Feishu", email=""),
        QuestionAnswerCreate(call_id="answer", selected=["Skip"]),
        source="feishu",
        mirror_send_uuid="send",
    )
    submit = asyncio.create_task(inbox.pump_answer(receipt.message_id))
    await entered.wait()

    async def rotate():
        nonlocal changed
        async with bot._gate("owned-gate-test"):
            changed = True

    rotation = asyncio.create_task(rotate())
    await asyncio.sleep(0)
    assert not changed
    release.set()
    await submit
    await rotation
    assert changed and inbox.messages[receipt.message_id].state == "completed"


@pytest.mark.asyncio
async def test_mirror_drain_does_not_starve_questions_after_eight(runtime, monkeypatch):
    service, inbox, _, _, _ = runtime
    from claude_hub.api.routes.feishu_bot import card_actions

    original = await service.capture("terminal-tab-tab", "turn-A", "call-A")
    inbox.questions.clear()
    for index in range(20):
        question = original.model_copy(update={"question_ref": f"question-{index}"}, deep=True)
        # Already-checked unbound questions must not consume the worker budget.
        question.mirror_checked = index < 8
        inbox.questions[question.question_ref] = question
    mirrored = set()

    async def mirror(question):
        mirrored.add(question.question_ref)
        question.mirror_checked = True

    monkeypatch.setattr(card_actions, "mirror_question", mirror)
    for _ in range(3):
        await service.tick()
        await asyncio.gather(*tuple(inbox._question_tasks.values()))
    assert mirrored == {f"question-{index}" for index in range(8, 20)}


@pytest.mark.asyncio
async def test_managed_handoff_requires_persisted_exact_fingerprint(runtime, tmp_path, monkeypatch):
    from claude_hub.models import ManagedSession

    service, inbox, _, _, _ = runtime
    q = await service.capture("terminal-tab-tab", "turn-A", "call-A")
    r = await service.admit(
        q,
        User(open_id="local", name="Local", email=""),
        QuestionAnswerCreate(call_id="owned-answer", selected=["Skip"]),
    )
    item = inbox.messages[r.message_id]
    now = utc_now()
    owner = ManagedSession(
        id="managed-owner",
        workspace_id="workspace",
        tab_id="tab",
        role="orchestrator",
        agent_type="claude",
        session_kind="chat",
        status="idle",
        runtime_status="idle",
        title="Chat",
        workspace_path=str(tmp_path),
        tmux_session="",
        target="local",
        created_at=now,
        updated_at=now,
        pending_call_ids=[item.call_id],
        pending_messages={item.call_id: item.message},
        chat_question_answer_calls={'["turn-A","call-A"]': item.call_id},
        call_payload_fingerprints={
            item.call_id: stream.workspace_manager._compute_payload_fingerprint(item.message, [])
        },
    )
    q.session_id = owner.id
    q.owner_created_at = str(owner.created_at)
    q.owner_workspace_id = owner.workspace_id
    path = tmp_path / "mailbox.json"
    monkeypatch.setattr(stream.workspace_manager, "_workspace_state_file", lambda _: path)
    # An in-memory owner, or an absent/truncated store, cannot prove transfer.
    assert not service.managed_handoff_persisted(q, item)
    path.write_text('{"sessions": [')
    assert not service.managed_handoff_persisted(q, item)
    path.write_text(json.dumps({"sessions": [owner.model_dump(mode="json")]}))
    assert service.managed_handoff_persisted(q, item)
    altered = owner.model_copy(deep=True)
    altered.call_payload_fingerprints[item.call_id] = "different-payload"
    path.write_text(json.dumps({"sessions": [altered.model_dump(mode="json")]}))
    assert not service.managed_handoff_persisted(q, item)


@pytest.mark.asyncio
async def test_corrupt_inbox_native_managed_web_keeps_owner_guard(runtime, monkeypatch):
    service, _, session, manager, _ = runtime
    session.id = "managed-corrupt-inbox"
    guarded = False

    @asynccontextmanager
    async def ownership(value, **kwargs):
        nonlocal guarded
        assert value is session
        guarded = True
        try:
            yield value
        finally:
            guarded = False

    async def native(*args, **kwargs):
        assert guarded
        assert args[2] == "turn-A"
        assert kwargs["expected_call_id"] == "call-A"
        return True

    def unavailable():
        raise ValueError("corrupt optional inbox")

    monkeypatch.setattr(service, "inbox", unavailable)
    monkeypatch.setattr(stream._agent_stream_bindings, "managed_stream_interaction", ownership)
    manager.answer_pending_question.side_effect = native
    result = await service.web(session.id, payload(), User(open_id="local", name="Local", email=""))
    assert result == {"ok": True, "answered_question": True}
    assert not guarded


@pytest.mark.asyncio
async def test_retiring_runtime_expires_question_without_stopping_other_mirrors(
    runtime, monkeypatch
):
    from claude_hub.api.routes.feishu_bot import card_actions
    from claude_hub.services.agent_stream.runtime.input import InputMixin
    from claude_hub.services.agent_stream.runtime.values import StructuredSourceUnavailable

    service, inbox, _, _, _ = runtime
    question = await service.capture("terminal-tab-tab", "turn-A", "call-A")

    def retiring():
        raise StructuredSourceUnavailable("runtime teardown is in progress")

    owner = SimpleNamespace(_require_startable_incarnation=retiring)
    tailer = service.context(question.session_id)[2]
    monkeypatch.setattr(
        tailer,
        "question_snapshot",
        lambda turn, call: InputMixin.question_snapshot(owner, turn, call),
    )
    monkeypatch.setattr(card_actions, "mirror_question", AsyncMock())
    await service.tick()
    assert question.state == "stale"
    assert not inbox._question_tasks
