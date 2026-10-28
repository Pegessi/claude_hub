"""Question-answer durability is independent of ordinary message FIFO."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from claude_hub.models.chat_message import (
    ChatMessageCreate,
    ChatQuestion,
    QuestionAnswerCreate,
    utc_now,
)
from claude_hub.services.chat_message_inbox import (
    ChatMessageInbox,
    InboxError,
    InboxEvidence,
    InboxTarget,
)


class Harness:
    def __init__(self, path: Path) -> None:
        self.ordinary_calls: list[str] = []
        self.answer_calls: list[str] = []

        async def ordinary(item: Any, before: Any) -> None:
            self.ordinary_calls.append(item.call_id)
            raise InboxError(409, "busy")

        async def evidence(_: Any) -> InboxEvidence:
            return InboxEvidence("absent")

        self.inbox = ChatMessageInbox(
            path, lambda _: InboxTarget("incarnation"), ordinary, evidence
        )

        async def answer(item: Any, before: Any) -> None:
            before()
            self.answer_calls.append(item.call_id)

        self.inbox.answer_submit = answer

    def question(self, ref: str = "q-one") -> ChatQuestion:
        now = utc_now()
        question = ChatQuestion(
            question_ref=ref,
            tab_id="tab",
            session_id="terminal-tab-tab",
            incarnation="incarnation",
            turn_id="turn-original",
            call_id=f"call-{ref}",
            run_epoch=1,
            question_id="0",
            prompt="Choose",
            options=["Run", "Skip"],
            mode="native",
            created_at=now,
            expires_at=now + timedelta(hours=24),
        )
        self.inbox.questions[ref] = question
        self.inbox._save()
        return question

    async def claim(
        self, ref: str = "q-one", call: str = "answer-one", selected: str = "Run"
    ) -> Any:
        return await self.inbox.claim_answer(
            ref, "owner", "Web", QuestionAnswerCreate(call_id=call, selected=[selected])
        )


@pytest.mark.asyncio
async def test_question_claim_is_atomic_persistent_and_payload_fingerprinted(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path / "inbox.json")
    h.question()
    same = await asyncio.gather(*(h.claim() for _ in range(10)))
    assert len({receipt.message_id for receipt in same}) == 1
    assert len(h.inbox.messages) == 1
    clone = Harness(h.inbox.path)
    receipt = await clone.claim()
    assert receipt.message_id == same[0].message_id
    with pytest.raises(InboxError) as conflict:
        await clone.claim(selected="Skip")
    assert conflict.value.status == 409
    with pytest.raises(InboxError):
        await clone.claim(call="second-answer")
    assert json.loads(next(iter(clone.inbox.messages.values())).message)["answers"] == [
        {"questionId": "0", "selected": ["Run"]}
    ]


@pytest.mark.asyncio
async def test_competing_channels_claim_one_question_once(tmp_path: Path) -> None:
    h = Harness(tmp_path / "inbox.json")
    q = h.question()
    results = await asyncio.gather(
        *(h.claim(call=call) for call in ["web", "card", "text"]), return_exceptions=True
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert len(h.inbox.messages) == 1 and q.state == "claimed"


@pytest.mark.asyncio
async def test_question_answer_bypasses_held_ordinary_fifo_without_dispatching_it(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path / "inbox.json")
    await h.inbox.enqueue(
        "tab", "owner", "CLI", ChatMessageCreate(message="ordinary", call_id="ordinary-one")
    )
    h.question()
    answer = await h.claim()
    # A blocked native turn can hold an ordinary submission lock indefinitely;
    # resolving its question must not need this same lock.
    lock = h.inbox._pump_locks.setdefault("tab", asyncio.Lock())
    await lock.acquire()
    try:
        await asyncio.wait_for(h.inbox.pump_answer(answer.message_id), 1)
    finally:
        lock.release()
    assert h.answer_calls == ["answer-one"] and not h.ordinary_calls
    ordinary = next(m for m in h.inbox.messages.values() if m.kind == "message")
    assert ordinary.state == "queued"


@pytest.mark.parametrize(
    "after_fence", [False, True], ids=["proven-before-write", "possibly-written"]
)
@pytest.mark.asyncio
async def test_restart_recovers_only_proven_unsubmitted_answer(
    tmp_path: Path, after_fence: bool
) -> None:
    h = Harness(tmp_path / "inbox.json")
    q = h.question()
    answer = await h.claim()
    h.inbox._change(answer.message_id, "dispatching")
    if after_fence:
        h.inbox._before_delivery(answer.message_id)
    clone = Harness(h.inbox.path)
    await clone.inbox.reconcile(restarting=True)
    recovered = clone.inbox.messages[answer.message_id]
    assert recovered.state == ("uncertain" if after_fence else "queued")
    if after_fence:
        assert clone.inbox.questions[q.question_ref].state == "uncertain"
        await clone.inbox.pump_answer(answer.message_id)
        assert not clone.answer_calls
    else:
        await clone.inbox.pump_answer(answer.message_id)
        assert clone.answer_calls == ["answer-one"]


@pytest.mark.parametrize(
    "after_fence", [False, True], ids=["rejected-before-write", "unknown-write"]
)
@pytest.mark.asyncio
async def test_submit_failure_preserves_no_replay_fence(tmp_path: Path, after_fence: bool) -> None:
    h = Harness(tmp_path / "inbox.json")
    q = h.question()
    answer = await h.claim()

    async def failed(_: Any, before: Any) -> None:
        if after_fence:
            before()
        raise RuntimeError("write failed")

    h.inbox.answer_submit = failed
    await h.inbox.pump_answer(answer.message_id)
    assert h.inbox.messages[answer.message_id].state == ("uncertain" if after_fence else "failed")
    assert q.state == ("uncertain" if after_fence else "stale")
    await h.inbox.pump_answer(answer.message_id)
    assert not h.answer_calls


@pytest.mark.asyncio
async def test_answer_backpressure_does_not_claim_question(tmp_path: Path) -> None:
    h = Harness(tmp_path / "inbox.json")
    q = h.question()
    h.inbox.max_bytes = 3
    with pytest.raises(InboxError) as overloaded:
        await h.claim(selected="large answer")
    assert overloaded.value.status == 429 and q.state == "open"
    assert not h.inbox.messages


def test_v1_migration_preserves_exact_backup_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "inbox.json"
    original = '{"version":1,"messages":[],"conversations":{},"epochs":{}}'
    path.write_text(original)
    h = Harness(path)
    assert json.loads(path.read_text())["version"] == 2
    backup = path.with_suffix(".v1-backup.json")
    assert backup.read_text() == original and backup.stat().st_mode & 0o777 == 0o600
    Harness(path)
    assert backup.read_text() == original and not h.inbox.messages


@pytest.mark.asyncio
async def test_answer_receipt_expiry_retains_tombstone_but_removes_question_content(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path / "inbox.json")
    q = h.question()
    answer = await h.claim()
    await h.inbox.pump_answer(answer.message_id)
    item = h.inbox.messages[answer.message_id]
    item.updated_at = utc_now() - timedelta(days=31)
    q.created_at = utc_now() - timedelta(days=32)
    q.expires_at = utc_now() - timedelta(days=31)
    h.inbox._save()
    await h.inbox.reconcile()
    assert item.expired and not item.message
    assert not q.prompt and not q.options
    with pytest.raises(InboxError) as expired:
        await h.claim()
    assert expired.value.status == 410


@pytest.mark.parametrize(
    "state", ["unsupported-version", "duplicate-question", "dangling-answer"], ids=str
)
def test_corrupt_question_state_fails_closed_without_overwriting(
    tmp_path: Path, state: str
) -> None:
    path = tmp_path / "inbox.json"
    h = Harness(path)
    q = h.question()
    data = json.loads(path.read_text())
    if state == "unsupported-version":
        data["version"] = 99
    elif state == "duplicate-question":
        data["questions"].append(q.model_dump(mode="json"))
    else:
        data["questions"][0].update(state="claimed", answer_id="missing-answer")
    original = json.dumps(data)
    path.write_text(original)
    with pytest.raises((RuntimeError, ValueError)):
        Harness(path)
    assert path.read_text() == original


@pytest.mark.parametrize("call", ["", " "], ids=["empty", "blank"])
def test_answer_identity_must_be_nonblank(call: str) -> None:
    with pytest.raises(ValidationError):
        QuestionAnswerCreate(call_id=call, selected=["Run"])
