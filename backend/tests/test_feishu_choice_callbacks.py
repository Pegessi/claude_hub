"""Exercise the real encrypted HTTPS card adapter and durable answer intake."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import secrets
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from fastapi import APIRouter, FastAPI

from claude_hub.api import feishu_bot as bot_api
from claude_hub.api.routes.feishu_bot import card_actions
from claude_hub.models.chat_message import ChatQuestion, QuestionMirror, utc_now
from claude_hub.services.chat_message_inbox import ChatMessageInbox, InboxEvidence, InboxTarget
from claude_hub.services.feishu_bot import FeishuBotConfig, FeishuMessageEvent
from claude_hub.services.feishu_bot_pool import FeishuBotPoolStore, OwnerIdentity

TOKEN = "test-verification-token"
KEY = "test-encrypt-key"
ACTION = "test-opaque-action-token-256-bits"
OTHER = "test-opaque-other-token-256-bits"
PATH = "/api/feishu/bot/bots/{bot}/card-actions"


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.pool = FeishuBotPoolStore(tmp_path / "pool.json", tmp_path / "legacy.json")
        self.bot = self.pool.create_bot(
            name="Test", config=FeishuBotConfig("app-test", "secret-test"), environ={}
        )
        current = self.pool.effective(self.bot, {})
        self.pool.configure_callbacks(
            self.bot,
            callback_enabled=True,
            expected_revision=current.revision,
            verification_token=TOKEN,
            encrypt_key=KEY,
            expected_tenant_key="tenant-test",
            environ={},
        )
        owner = OwnerIdentity(open_id="local", email="local@localhost", kind="local")
        current = self.pool.effective(self.bot, {})
        code, _, _ = self.pool.issue_code(
            self.bot,
            owner=owner,
            tab_id="tab-test",
            workspace_id=None,
            expected_revision=current.revision,
            environ={},
        )
        claim, word = self.pool.claim_code(
            self.bot, code=code, sender_open_id="ou-owner", chat_id="oc-p2p", environ={}
        )
        current = self.pool.effective(self.bot, {})
        self.binding = self.pool.activate_claim(
            self.bot,
            pairing_id=claim.pairing_id,
            confirm_word=word,
            actor=owner,
            expected_revision=current.revision,
            environ={},
        )
        current = self.pool.effective(self.bot, {})

        async def ordinary(*_: Any) -> None:
            pytest.fail("Question answer reached ordinary message sender")

        async def evidence(*_: Any) -> InboxEvidence:
            return InboxEvidence("absent")

        self.inbox = ChatMessageInbox(
            tmp_path / "inbox.json", lambda _: InboxTarget("incarnation"), ordinary, evidence
        )
        now = utc_now()
        self.question = ChatQuestion(
            question_ref="question-test",
            tab_id="tab-test",
            session_id="terminal-tab-tab-test",
            incarnation="incarnation",
            turn_id="turn-original",
            call_id="call-original",
            run_epoch=1,
            question_id="0",
            prompt="Run or skip?",
            options=["Run", "Skip"],
            mode="native",
            created_at=now,
            expires_at=now + timedelta(hours=24),
            mirror=QuestionMirror(
                bot_id=self.bot,
                app_id="app-test",
                tenant_key="tenant-test",
                pairing_id=self.binding.pairing_id,
                generation=current.generation,
                revision=current.revision,
                chat_id="oc-p2p",
                sender_open_id="ou-owner",
                send_uuid="test-send-uuid",
                message_id="om-card",
                state="sent",
                displayed_state="open",
                action_digests={
                    hashlib.sha256(ACTION.encode()).hexdigest(): {
                        "operation": "question_answer",
                        "selected": "Run",
                    },
                    hashlib.sha256(OTHER.encode()).hexdigest(): {"operation": "question_text_hint"},
                },
            ),
        )
        self.inbox.questions[self.question.question_ref] = self.question
        self.inbox._save()
        monkeypatch.setattr(bot_api, "_pool", self.pool)
        monkeypatch.setattr(bot_api, "_binding_still_routes", lambda *_: True)
        monkeypatch.setattr(bot_api, "_message_matches_binding_time", lambda *_: True)
        monkeypatch.setattr(card_actions.answers, "inbox", lambda: self.inbox)
        monkeypatch.setattr(card_actions.answers, "live", lambda _: (None, None, None))
        card_actions._rates.clear()
        router = APIRouter(prefix="/api/feishu/bot")
        card_actions.register_card_routes(router)
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://sandbox.invalid"
        )

    def event(self, event_id: str = "evt-one") -> dict[str, Any]:
        return {
            "schema": "2.0",
            "header": {
                "event_id": event_id,
                "token": TOKEN,
                "create_time": str(int(time.time() * 1_000_000)),
                "event_type": "card.action.trigger",
                "app_id": "app-test",
                "tenant_key": "tenant-test",
            },
            "event": {
                "operator": {"open_id": "ou-owner", "tenant_key": "tenant-test"},
                "host": "im_message",
                "context": {"open_message_id": "om-card", "open_chat_id": "oc-p2p"},
                "action": {
                    "tag": "button",
                    "value": {"v": 1, "hub_action": "question_answer", "hub_token": ACTION},
                },
            },
        }

    def signed(
        self, payload: Any, *, key: str = KEY, timestamp: str | None = None
    ) -> tuple[bytes, dict[str, str]]:
        iv = b"test-only-iv1234"
        encrypted = AES.new(hashlib.sha256(key.encode()).digest(), AES.MODE_CBC, iv).encrypt(
            pad(json.dumps(payload).encode(), 16)
        )
        raw = json.dumps({"encrypt": base64.b64encode(iv + encrypted).decode()}).encode()
        stamp = timestamp or str(int(time.time()))
        nonce = secrets.token_hex(12)
        signature = hashlib.sha256((stamp + nonce + key).encode() + raw).hexdigest()
        return raw, {
            "content-type": "application/json",
            "x-lark-request-timestamp": stamp,
            "x-lark-request-nonce": nonce,
            "x-lark-signature": signature,
        }

    async def post(self, payload: Any, **kwargs: Any) -> httpx.Response:
        raw, headers = self.signed(payload, **kwargs)
        return await self.client.post(PATH.format(bot=self.bot), content=raw, headers=headers)

    def reply(self, **changes: Any) -> FeishuMessageEvent:
        values: dict[str, Any] = {
            "event_id": "message-event",
            "message_id": "om-reply",
            "app_id": "app-test",
            "sender_open_id": "ou-owner",
            "chat_id": "oc-p2p",
            "text": "Do it tomorrow",
            "message_created_at_ms": int(time.time() * 1000),
            "parent_id": "om-card",
        }
        values.update(changes)
        return FeishuMessageEvent(**values)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_encrypted_url_challenge_under_one_second_never_claims(harness: Harness) -> None:
    before = harness.inbox.path.read_bytes()
    start = time.perf_counter()
    response = await harness.post(
        {"type": "url_verification", "challenge": "challenge-test", "token": TOKEN}
    )
    assert time.perf_counter() - start < 1
    assert response.status_code == 200 and response.json() == {"challenge": "challenge-test"}
    assert harness.inbox.path.read_bytes() == before
    assert not harness.inbox.messages and not harness.inbox.callback_receipts


@pytest.mark.asyncio
async def test_https_click_is_durable_before_three_second_receipt_and_slow_provider(
    harness: Harness,
) -> None:
    release = asyncio.Event()
    submitted: list[dict[str, Any]] = []

    async def slow_provider(item: Any, before: Any) -> None:
        before()
        submitted.append(json.loads(item.message))
        await release.wait()

    harness.inbox.answer_submit = slow_provider
    start = time.perf_counter()
    response = await harness.post(harness.event())
    assert time.perf_counter() - start < 3
    assert response.status_code == 200
    question = harness.question
    assert question.state == "claimed" and question.answer_id
    on_disk = json.loads(harness.inbox.path.read_text())
    assert on_disk["questions"][0]["answer_id"] == question.answer_id
    assert on_disk["messages"][0]["kind"] == "question_answer"
    task = asyncio.create_task(harness.inbox.pump_answer(question.answer_id))
    await asyncio.sleep(0)
    assert not task.done()
    assert submitted == [
        {"type": "ask_question_response", "answers": [{"questionId": "0", "selected": ["Run"]}]}
    ]
    release.set()
    await task
    assert (
        question.state == "resolved"
        and harness.inbox.messages[question.answer_id].state == "completed"
    )


@pytest.mark.asyncio
async def test_duplicate_event_is_idempotent_and_changed_same_event_conflicts(
    harness: Harness,
) -> None:
    event = harness.event()
    assert (await harness.post(event)).status_code == 200
    assert (await harness.post(event)).status_code == 200
    changed = copy.deepcopy(event)
    changed["event"]["action"]["value"]["hub_token"] = OTHER
    assert (await harness.post(changed)).status_code == 409
    assert len(harness.inbox.messages) == 1


@pytest.mark.parametrize(
    "field",
    [
        "app",
        "tenant",
        "operator_tenant",
        "operator",
        "chat",
        "message",
        "host",
        "group",
        "event_time",
        "action",
    ],
    ids=str,
)
@pytest.mark.asyncio
async def test_authenticated_wrong_context_never_claims(harness: Harness, field: str) -> None:
    event = harness.event()
    if field in {"app", "tenant"}:
        event["header"]["app_id" if field == "app" else "tenant_key"] = "wrong"
    elif field in {"operator", "operator_tenant"}:
        event["event"]["operator"]["open_id" if field == "operator" else "tenant_key"] = "wrong"
    elif field in {"chat", "message", "group"}:
        event["event"]["context"]["open_message_id" if field == "message" else "open_chat_id"] = (
            "oc-group" if field == "group" else "wrong"
        )
    elif field == "host":
        event["event"]["host"] = "url_preview"
    elif field == "event_time":
        event["header"]["create_time"] = "0"
    else:
        event["event"]["action"]["value"]["hub_token"] = "unknown-action-token"
    assert (await harness.post(event)).status_code == 403
    assert harness.question.state == "open" and not harness.inbox.messages


@pytest.mark.parametrize(
    "kind",
    ["signature", "token", "encrypted_key", "replay", "malformed", "oversize", "plaintext", "http"],
    ids=str,
)
@pytest.mark.asyncio
async def test_bad_auth_or_transport_never_claims(harness: Harness, kind: str) -> None:
    event = harness.event()
    if kind == "token":
        event["header"]["token"] = "wrong-token"
    raw, headers = harness.signed(
        event,
        key="wrong-key" if kind == "encrypted_key" else KEY,
        timestamp="0" if kind == "replay" else None,
    )
    if kind == "signature":
        headers["x-lark-signature"] = "0" * 64
    elif kind == "malformed":
        raw = b'{"encrypt":"bad-ciphertext"}'
    elif kind == "oversize":
        raw = b"x" * 65537
    elif kind == "plaintext":
        raw = json.dumps(event).encode()
    url = PATH.format(bot=harness.bot)
    if kind == "http":
        url = "http://sandbox.invalid" + url
    response = await harness.client.post(url, content=raw, headers=headers)
    assert response.status_code == (413 if kind == "oversize" else 400 if kind == "http" else 403)
    assert harness.question.state == "open" and not harness.inbox.messages
    assert KEY not in response.text and TOKEN not in response.text


@pytest.mark.parametrize(
    "kind", ["rotation", "disabled", "unbound", "revoked_owner", "expired"], ids=str
)
@pytest.mark.asyncio
async def test_binding_changes_and_expiry_warn_without_execution(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    if kind == "rotation":
        entry = harness.pool.effective(harness.bot)
        harness.pool.configure_callbacks(
            harness.bot,
            callback_enabled=True,
            expected_revision=entry.revision,
            verification_token=TOKEN + "-rotated",
        )
    elif kind == "disabled":
        entry = harness.pool.effective(harness.bot)
        harness.pool.configure_callbacks(
            harness.bot, callback_enabled=False, expected_revision=entry.revision
        )
    elif kind == "unbound":
        monkeypatch.setattr(bot_api, "_binding_still_routes", lambda *_: False)
    elif kind == "revoked_owner":
        monkeypatch.setattr(bot_api, "_owner_is_authorized", lambda *_: False)
    else:
        harness.question.expires_at = utc_now() - timedelta(seconds=1)
    response = await harness.post(harness.event())
    assert response.status_code in {200, 403}
    assert not harness.inbox.messages


@pytest.mark.asyncio
async def test_other_hint_does_not_claim_and_correlated_text_is_exact_answer(
    harness: Harness,
) -> None:
    event = harness.event()
    event["event"]["action"]["value"].update(hub_token=OTHER, hub_action="question_text_hint")
    assert (await harness.post(event)).status_code == 200
    assert not harness.inbox.messages
    assert await card_actions.intercept_reply(harness.bot, harness.reply())
    assert harness.question.answer_id
    item = harness.inbox.messages[harness.question.answer_id]
    assert json.loads(item.message)["answers"] == [
        {"questionId": "0", "selected": ["Do it tomorrow"]}
    ]
    assert await card_actions.intercept_reply(harness.bot, harness.reply())
    assert len(harness.inbox.messages) == 1


@pytest.mark.parametrize(
    "kind", ["group", "sender", "chat", "stale_time", "uncorrelated", "unknown_ref"], ids=str
)
@pytest.mark.asyncio
async def test_bad_or_uncorrelated_text_does_not_answer(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    changes: dict[str, Any] = {}
    if kind == "group":
        changes["chat_type"] = "group"
    elif kind == "sender":
        changes["sender_open_id"] = "ou-other"
    elif kind == "chat":
        changes["chat_id"] = "oc-other"
    elif kind == "stale_time":
        monkeypatch.setattr(bot_api, "_message_matches_binding_time", lambda *_: False)
    elif kind == "uncorrelated":
        changes["parent_id"] = None
    else:
        changes.update(parent_id=None, text="/answer unknown answer")
    intercepted = await card_actions.intercept_reply(harness.bot, harness.reply(**changes))
    assert intercepted is (kind != "uncorrelated")
    assert not harness.inbox.messages


@pytest.mark.asyncio
async def test_card_text_and_web_admission_race_has_one_winner(harness: Harness) -> None:
    from claude_hub.models import User
    from claude_hub.models.chat_message import QuestionAnswerCreate

    results = await asyncio.gather(
        harness.post(harness.event()),
        card_actions.intercept_reply(harness.bot, harness.reply()),
        card_actions.answers.admit(
            harness.question,
            User(open_id="local", name="Web", email="local@localhost"),
            QuestionAnswerCreate(call_id="web-answer", selected=["Skip"]),
        ),
        return_exceptions=True,
    )
    assert len(results) == 3 and len(harness.inbox.messages) == 1
    assert harness.question.state == "claimed" and harness.question.answer_id


@pytest.mark.asyncio
async def test_loopback_tls_challenge_click_reply_and_duplicate_receipts(
    harness: Harness, tmp_path: Path
) -> None:
    """A real TLS socket uses only generated credentials and a fake provider."""
    import socket
    import ssl
    import subprocess
    import threading

    import uvicorn

    certificate, private_key = tmp_path / "callback.crt", tmp_path / "callback.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    private_key.chmod(0o600)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            harness.app,
            host="127.0.0.1",
            port=port,
            loop="asyncio",
            lifespan="off",
            access_log=False,
            log_config=None,
            ssl_certfile=str(certificate),
            ssl_keyfile=str(private_key),
        )
    )
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    release = asyncio.Event()
    provider_started = asyncio.Event()
    calls: list[dict[str, Any]] = []

    async def provider(item: Any, before: Any) -> None:
        before()
        calls.append(json.loads(item.message))
        provider_started.set()
        await release.wait()

    harness.inbox.answer_submit = provider
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            assert (
                thread.is_alive() and time.monotonic() < deadline
            ), "owned TLS server failed to start"
            await asyncio.sleep(0.01)
        trust = ssl.create_default_context(cafile=str(certificate))
        async with httpx.AsyncClient(
            base_url=f"https://127.0.0.1:{port}", verify=trust, trust_env=False
        ) as client:
            challenge = {"type": "url_verification", "token": TOKEN, "challenge": "tls-challenge"}
            raw, headers = harness.signed(challenge)
            start = time.perf_counter()
            response = await client.post(PATH.format(bot=harness.bot), content=raw, headers=headers)
            assert time.perf_counter() - start < 1
            assert response.status_code == 200 and response.json() == {"challenge": "tls-challenge"}
            assert not harness.inbox.messages
            event = harness.event("tls-click")
            raw, headers = harness.signed(event)
            start = time.perf_counter()
            response = await client.post(PATH.format(bot=harness.bot), content=raw, headers=headers)
            assert time.perf_counter() - start < 3
            assert response.status_code == 200
            answer_id = harness.question.answer_id
            assert answer_id
            task = asyncio.create_task(harness.inbox.pump_answer(answer_id))
            await asyncio.wait_for(provider_started.wait(), 1)
            assert not task.done()
            # Repeat through TLS while the provider is deliberately blocked.
            duplicate = await client.post(
                PATH.format(bot=harness.bot), content=raw, headers=headers
            )
            assert duplicate.status_code == 200 and len(harness.inbox.messages) == 1
            assert len(calls) == 1
            release.set()
            await task
            assert harness.question.state == "resolved"
            # A separate question proves correlated text intake without inference.
            second = harness.question.model_copy(deep=True)
            second.question_ref, second.call_id = "reply-question", "reply-call"
            second.state, second.answer_id = "open", None
            assert second.mirror is not None
            second.mirror.message_id = "om-reply-card"
            second.mirror.action_digests = {}
            harness.inbox.questions[second.question_ref] = second
            harness.inbox._save()
            assert await card_actions.intercept_reply(
                harness.bot, harness.reply(parent_id="om-reply-card", message_id="om-tls-text")
            )
            assert second.answer_id
            await harness.inbox.pump_answer(second.answer_id)
            assert calls[-1]["answers"] == [{"questionId": "0", "selected": ["Do it tomorrow"]}]
    finally:
        release.set()
        server.should_exit = True
        await asyncio.to_thread(thread.join, 5)
        listener.close()
        assert not thread.is_alive(), "owned TLS server did not stop"


@pytest.mark.asyncio
async def test_open_question_and_callback_receipt_survive_rehydration(harness: Harness) -> None:
    async def ordinary(*_: Any) -> None:
        pytest.fail("Answer must not become ordinary input")

    async def evidence(*_: Any) -> InboxEvidence:
        return InboxEvidence("absent")

    path = harness.inbox.path
    harness.inbox = ChatMessageInbox(path, lambda _: InboxTarget("incarnation"), ordinary, evidence)
    harness.question = harness.inbox.questions["question-test"]
    assert harness.question.state == "open"
    event = harness.event("rehydrated-event")
    assert (await harness.post(event)).status_code == 200
    answer_id = harness.question.answer_id
    harness.inbox = ChatMessageInbox(path, lambda _: InboxTarget("incarnation"), ordinary, evidence)
    harness.question = harness.inbox.questions["question-test"]
    assert harness.question.answer_id == answer_id
    assert (await harness.post(event)).status_code == 200
    assert len(harness.inbox.messages) == 1


@pytest.mark.asyncio
async def test_nonce_replay_allows_identical_body_and_rejects_changed_body(
    harness: Harness,
) -> None:
    raw, headers = harness.signed(harness.event())
    first = await harness.client.post(PATH.format(bot=harness.bot), content=raw, headers=headers)
    assert first.status_code == 200
    same = await harness.client.post(PATH.format(bot=harness.bot), content=raw, headers=headers)
    assert same.status_code == 200
    changed, _ = harness.signed(harness.event("another-event"))
    headers["x-lark-signature"] = hashlib.sha256(
        (headers["x-lark-request-timestamp"] + headers["x-lark-request-nonce"] + KEY).encode()
        + changed
    ).hexdigest()
    rejected = await harness.client.post(
        PATH.format(bot=harness.bot), content=changed, headers=headers
    )
    assert rejected.status_code == 409
    assert len(harness.inbox.messages) == 1


@pytest.mark.asyncio
async def test_mirror_send_stores_only_digests_and_terminal_patch_has_no_actions(
    harness: Harness, monkeypatch
):
    sent, patched = [], []

    class Client:
        def __init__(self, config):
            pass

        async def get_tenant_token(self):
            return "test-token"

        async def send_message_with_identity(self, chat_id, msg_type, content, **kwargs):
            sent.append((chat_id, msg_type, content, kwargs))
            return "om-new-card"

        async def patch_message_card(self, message_id, card):
            patched.append((message_id, card))

    monkeypatch.setattr(card_actions, "FeishuBotClient", Client)
    q = harness.question
    q.mirror = None
    assert await card_actions.mirror_question(q)
    assert len(sent) == 1
    assert q.mirror.message_id == "om-new-card"
    controls = [e for e in sent[0][2]["body"]["elements"] if e["tag"] == "button"]
    tokens = [e["behaviors"][0]["value"]["hub_token"] for e in controls]
    assert len(set(tokens)) == 3
    assert all(token not in harness.inbox.path.read_text() for token in tokens)
    q.state = "resolved"
    assert await card_actions.mirror_question(q)
    assert patched[0][0] == "om-new-card"
    assert all("behaviors" not in e for e in patched[0][1]["body"]["elements"])
    await card_actions.mirror_question(q)
    assert len(sent) == 1 and len(patched) == 1


@pytest.mark.asyncio
async def test_uncertain_mirror_send_is_fenced_across_reload(harness: Harness, monkeypatch):
    calls = []

    class Client:
        def __init__(self, config):
            pass

        async def get_tenant_token(self):
            return "test-token"

        async def send_message_with_identity(self, *args, **kwargs):
            calls.append(kwargs)
            raise TimeoutError

    monkeypatch.setattr(card_actions, "FeishuBotClient", Client)
    q = harness.question
    q.mirror = None
    assert not await card_actions.mirror_question(q)
    assert q.mirror.state == "uncertain"
    clone = ChatMessageInbox(
        harness.inbox.path, harness.inbox.target, harness.inbox.submit, harness.inbox.evidence
    )
    harness.inbox = clone
    assert not await card_actions.mirror_question(clone.questions[q.question_ref])
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_remirror_replacement_invalidates_action_before_admission(harness: Harness):
    from claude_hub.models import User
    from claude_hub.models.chat_message import QuestionAnswerCreate
    from claude_hub.services.chat_message_inbox import InboxError

    q = harness.question
    old_uuid = q.mirror.send_uuid
    q.mirror.send_uuid = "new-mirror-generation"
    with pytest.raises(InboxError, match="replaced"):
        await card_actions.answers.admit(
            q,
            User(open_id="local", name="Feishu", email=""),
            QuestionAnswerCreate(call_id="late-click", selected=["Run"]),
            source="feishu",
            mirror_send_uuid=old_uuid,
        )
    assert not harness.inbox.messages
