"""Bot revocation is checked at native submission, not before an awaited preflight."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from claude_hub.api import agent_stream as stream_api
from claude_hub.api import feishu_bot as bot_api
from claude_hub.models import (
    AgentStreamEvent,
    AgentStreamEventType,
    AgentType,
    ExecutionTarget,
    SessionKind,
    User,
)
from claude_hub.services import goal_run, ttyd_manager, workspace_manager
from claude_hub.services.feishu_bot import FeishuBotConfig, FeishuMessageEvent
from claude_hub.services.feishu_bot_pool import (
    BOT_ENV_KEYS,
    OWNER_KIND_OAUTH,
    FeishuBotPoolStore,
    OwnerIdentity,
)


@pytest.fixture(autouse=True)
def no_live_http(monkeypatch):
    def reject(*args, **kwargs):
        pytest.fail("Live HTTP is forbidden in admission tests")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    for name in BOT_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_HUB_PUBLIC_BASE_URL", "https://hub.example.test")
    clock = [1_800_000_000.0]
    pool = FeishuBotPoolStore(
        path=tmp_path / "secrets" / "pool.json",
        legacy_path=tmp_path / "absent-legacy.json",
        now=lambda: clock[0],
    )
    config = FeishuBotConfig(
        app_id="cli-bot",
        app_secret="test-secret",
        verification_token="test-token",
        encrypt_key="test-encrypt",
    )
    bot_id = pool.create_bot(name="test Bot", config=config)
    owner = OwnerIdentity(open_id="ou-hub", email="hub@example.test", kind=OWNER_KIND_OAUTH)
    target = {"alive": True, "workspace_id": "ws-target"}
    authorized = [True]
    replies = []

    class Client:
        def __init__(self, candidate):
            self.config = candidate

        async def get_tenant_token(self):
            return "test-tenant-token"

        async def reply_text(self, message_id, text, *, access_token=None):
            replies.append((message_id, text))

    def validate_target(tab_id, requested_workspace_id):
        if not target["alive"]:
            raise HTTPException(status_code=404, detail="chat_tab_not_found")
        actual = target["workspace_id"]
        if requested_workspace_id is not None and requested_workspace_id != actual:
            raise HTTPException(status_code=403, detail="chat_tab_workspace_mismatch")
        return actual

    monkeypatch.setattr(bot_api, "_pool", pool)
    monkeypatch.setattr(bot_api, "_now", lambda: clock[0])
    monkeypatch.setattr(bot_api, "_dedup", SimpleNamespace(finish=Mock()))
    monkeypatch.setattr(bot_api, "_bot_gates", {})
    monkeypatch.setattr(bot_api, "FeishuBotClient", Client)
    monkeypatch.setattr(bot_api, "_validate_bind_target", validate_target)
    monkeypatch.setattr(bot_api, "_owner_is_authorized", lambda owner: authorized[0])
    return SimpleNamespace(
        pool=pool,
        bot_id=bot_id,
        owner=owner,
        target=target,
        clock=clock,
        authorized=authorized,
        replies=replies,
    )


def revision(env):
    return env.pool.snapshot().get(env.bot_id).revision


def claim(env):
    code, _, _ = env.pool.issue_code(
        env.bot_id,
        owner=env.owner,
        tab_id="tab-target",
        workspace_id=env.target["workspace_id"],
        expected_revision=revision(env),
    )
    return env.pool.claim_code(env.bot_id, code=code, sender_open_id="ou-bot", chat_id="oc-bot")


def activate(env):
    pending, word = claim(env)
    return env.pool.activate_claim(
        env.bot_id,
        pairing_id=pending.pairing_id,
        confirm_word=word,
        actor=env.owner,
        expected_revision=revision(env),
    )


def message():
    return FeishuMessageEvent(
        event_id="evt-1",
        message_id="om-1",
        app_id="cli-bot",
        sender_open_id="ou-bot",
        chat_id="oc-bot",
        text="bounded work",
        message_created_at_ms=1_800_000_000_000,
    )


def complete(queue, turn_id):
    queue.put_nowait(
        AgentStreamEvent(
            stream_sequence=1,
            session_id="native-session",
            tab_id="tab-target",
            agent_type=AgentType.CLAUDE,
            type=AgentStreamEventType.TURN_COMPLETED,
            turn_id=turn_id,
            payload={"status": "completed", "assistant_text": "test answer"},
            created_at=datetime.now(),
        )
    )


def install_stream(monkeypatch, on_send, *, subscribe_release=None, goal_lock=None):
    queue = asyncio.Queue()
    subscribed = asyncio.Event()
    session = SimpleNamespace(id="native-session")
    unsubscribed = []
    lock = goal_lock or asyncio.Lock()

    class Manager:
        async def subscribe(self, session_arg):
            assert session_arg is session
            subscribed.set()
            if subscribe_release is not None:
                await subscribe_release.wait()
            return queue

        def unsubscribe(self, session_id, queue_arg):
            unsubscribed.append((session_id, queue_arg))

    manager = Manager()

    async def send(session_arg, payload, manager_arg, **kwargs):
        assert session_arg is session and manager_arg is manager
        await on_send(payload, queue)

    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda tab_id: session)
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: manager)
    monkeypatch.setattr(stream_api, "_send_to_native", send)
    monkeypatch.setattr(goal_run, "get_goal_admission_lock", lambda tab_id: lock)
    monkeypatch.setattr(
        goal_run, "get_goal_manager", lambda: SimpleNamespace(current=lambda tab_id: None)
    )
    return SimpleNamespace(
        queue=queue, subscribed=subscribed, unsubscribed=unsubscribed, goal_lock=lock
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_at", ["subscribe", "goal"])
@pytest.mark.parametrize("change", ["unbind", "disable", "rotate", "owner"])
async def test_revocation_during_preflight_prevents_native_submission(
    env, monkeypatch, wait_at, change
):
    activate(env)
    effective = env.pool.effective(env.bot_id)
    submitted = []

    async def send(payload, queue):
        submitted.append(payload.client_turn_id)
        complete(queue, payload.client_turn_id)

    subscribe_release = asyncio.Event()
    goal_lock = asyncio.Lock()
    held_goal = wait_at == "goal"
    if held_goal:
        await goal_lock.acquire()
        subscribe_release.set()
    stream = install_stream(
        monkeypatch, send, subscribe_release=subscribe_release, goal_lock=goal_lock
    )
    job = asyncio.create_task(bot_api._handle_message_event(env.bot_id, message(), effective))
    try:
        await asyncio.wait_for(stream.subscribed.wait(), 2)
        async with bot_api._gate(env.bot_id):
            if change == "unbind":
                env.pool.release_binding(env.bot_id, expected_revision=revision(env))
            elif change == "disable":
                env.pool.update_bot(env.bot_id, expected_revision=revision(env), enabled=False)
            elif change == "rotate":
                env.pool.rotate_secrets(
                    env.bot_id,
                    expected_revision=revision(env),
                    app_secret="new-secret",
                    verification_token="new-token",
                    encrypt_key="new-encrypt",
                )
            else:
                env.authorized[0] = False
        if held_goal:
            goal_lock.release()
            held_goal = False
        subscribe_release.set()
        await asyncio.wait_for(job, 2)
        assert submitted == []
        assert env.replies == []
        assert len(stream.unsubscribed) == 1
    finally:
        if held_goal:
            goal_lock.release()
        subscribe_release.set()
        if not job.done():
            job.cancel()
        await asyncio.gather(job, return_exceptions=True)


@pytest.mark.asyncio
async def test_gate_covers_submission_but_not_model_completion(env, monkeypatch):
    activate(env)
    effective = env.pool.effective(env.bot_id)
    send_entered = asyncio.Event()
    submit_release = asyncio.Event()
    unbind_entered = asyncio.Event()
    submitted_turns = []
    gate_states = []

    async def send(payload, queue):
        entry = bot_api._bot_gates.get(env.bot_id)
        gate_states.append(bool(entry is not None and entry.lock.locked()))
        submitted_turns.append(payload.client_turn_id)
        send_entered.set()
        await submit_release.wait()

    stream = install_stream(monkeypatch, send)
    job = asyncio.create_task(bot_api._handle_message_event(env.bot_id, message(), effective))
    deleting = None

    async def unbind():
        unbind_entered.set()
        async with bot_api._gate(env.bot_id):
            env.pool.release_binding(env.bot_id, expected_revision=revision(env))

    try:
        await asyncio.wait_for(send_entered.wait(), 2)
        assert gate_states == [True]
        deleting = asyncio.create_task(unbind())
        await asyncio.wait_for(unbind_entered.wait(), 2)
        await asyncio.sleep(0)
        assert not deleting.done()
        submit_release.set()
        await asyncio.wait_for(deleting, 2)
        assert not job.done(), "model completion must still be awaited outside the Bot gate"
        complete(stream.queue, submitted_turns[0])
        await asyncio.wait_for(job, 2)
        assert len(submitted_turns) == 1
        assert env.replies == []
    finally:
        submit_release.set()
        pending = [item for item in (job, deleting) if item is not None]
        for item in pending:
            if not item.done():
                item.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_guard_checks_exact_pairing_even_with_current_config(env):
    old_binding = activate(env)
    env.pool.release_binding(env.bot_id, expected_revision=revision(env))
    new_binding = activate(env)
    assert new_binding.pairing_id != old_binding.pairing_id
    with pytest.raises(HTTPException) as raised:
        async with bot_api._admit_binding_dispatch(
            env.pool.effective(env.bot_id), old_binding, message()
        ):
            pytest.fail("stale pairing was admitted")
    assert raised.value.status_code == 409


@pytest.mark.asyncio
async def test_saved_none_workspace_is_not_a_wildcard(env):
    env.target["workspace_id"] = None
    binding = activate(env)
    effective = env.pool.effective(env.bot_id)
    env.target["workspace_id"] = "another-workspace"
    with pytest.raises(HTTPException) as raised:
        async with bot_api._admit_binding_dispatch(effective, binding, message()):
            pytest.fail("a changed workspace was admitted")
    assert raised.value.detail == "chat_tab_workspace_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["start", "activate"])
async def test_pairing_rechecks_target_after_waiting_for_gate(env, monkeypatch, action):
    body = {"tab_id": "tab-target", "workspace_id": "ws-target", "expected_revision": revision(env)}
    if action == "activate":
        pending, word = claim(env)
        body = {
            "pairing_id": pending.pairing_id,
            "confirm_word": word,
            "expected_revision": revision(env),
        }
    before = env.pool.path.read_bytes()
    original_gate = bot_api._gate
    queued = asyncio.Event()

    @asynccontextmanager
    async def observed_gate(bot_id):
        queued.set()
        async with original_gate(bot_id):
            yield

    monkeypatch.setattr(bot_api, "_gate", observed_gate)
    app = FastAPI()
    app.include_router(bot_api.router)
    app.dependency_overrides[bot_api.require_hub_user] = lambda: User(
        open_id=env.owner.open_id, name="Hub user", email=env.owner.email
    )
    request_task = None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        try:
            async with original_gate(env.bot_id):
                request_task = asyncio.create_task(
                    client.post(f"/api/feishu/bot/bots/{env.bot_id}/pair/{action}", json=body)
                )
                await asyncio.wait_for(queued.wait(), 2)
                env.target["alive"] = False
            response = await asyncio.wait_for(request_task, 2)
            assert response.status_code == 404
            assert response.json() == {"detail": "chat_tab_not_found"}
            assert env.pool.path.read_bytes() == before
            assert env.pool.snapshot().get(env.bot_id).binding is None
        finally:
            if request_task is not None and not request_task.done():
                request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_web_default_submission_has_no_external_guard(monkeypatch):
    submitted = []

    async def send(payload, queue):
        submitted.append(payload.text)

    install_stream(monkeypatch, send)
    result = await stream_api._dispatch_tab_stream_input(
        "tab-target",
        stream_api.AgentStreamSendRequest(text="web text", client_turn_id="web-turn"),
        turn_metadata={"origin": "web"},
    )
    assert result == "web-turn"
    assert submitted == ["web text"]


@pytest.mark.asyncio
async def test_clock_rollback_during_subscribe_is_rechecked_at_submission(env, monkeypatch):
    activate(env)
    effective = env.pool.effective(env.bot_id)
    submitted = []
    release = asyncio.Event()

    async def send(payload, queue):
        submitted.append(payload.client_turn_id)
        complete(queue, payload.client_turn_id)

    stream = install_stream(monkeypatch, send, subscribe_release=release)
    job = asyncio.create_task(bot_api._handle_message_event(env.bot_id, message(), effective))
    try:
        await asyncio.wait_for(stream.subscribed.wait(), 2)
        env.clock[0] -= 1
        release.set()
        await asyncio.wait_for(job, 2)
        assert submitted == []
        assert env.replies == []
        assert len(stream.unsubscribed) == 1
    finally:
        release.set()
        if not job.done():
            job.cancel()
        await asyncio.gather(job, return_exceptions=True)


def _install_chat_tab(
    monkeypatch,
    *,
    workspace_id="ws-1",
    session_kind=SessionKind.CHAT,
    target=ExecutionTarget.LOCAL,
    workspace_role=None,
    archived=False,
    with_process=True,
):
    tab = SimpleNamespace(
        id="tab-1",
        session_kind=session_kind,
        target=target,
        workspace_role=workspace_role,
        workspace_id=workspace_id,
    )
    monkeypatch.setattr(ttyd_manager, "get_tab", lambda tab_id: tab if tab_id == "tab-1" else None)
    if with_process:
        monkeypatch.setitem(ttyd_manager.processes, "tab-1", SimpleNamespace(archived=archived))
    else:
        monkeypatch.delitem(ttyd_manager.processes, "tab-1", raising=False)
    if workspace_id is not None:
        monkeypatch.setitem(
            workspace_manager.workspaces, workspace_id, SimpleNamespace(id=workspace_id)
        )


def test_deleted_workspace_keeps_the_rebinding_reason(monkeypatch) -> None:
    """The reason header is what tells the user to re-pair instead of retrying."""

    _install_chat_tab(monkeypatch)
    monkeypatch.delitem(workspace_manager.workspaces, "ws-1")

    with pytest.raises(HTTPException) as raised:
        bot_api._validate_bind_target("tab-1", "ws-1")

    assert raised.value.status_code == 409
    assert raised.value.detail == "chat_tab_workspace_missing"
    assert raised.value.headers == {stream_api.CHAT_ERROR_REASON_HEADER: "binding_target_missing"}


def test_a_different_workspace_is_rejected(monkeypatch) -> None:
    _install_chat_tab(monkeypatch, workspace_id="ws-1")

    with pytest.raises(HTTPException) as raised:
        bot_api._validate_bind_target("tab-1", "ws-other")

    assert raised.value.status_code == 403
    assert raised.value.detail == "chat_tab_workspace_mismatch"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"session_kind": SessionKind.TERMINAL}, id="terminal-session"),
        pytest.param({"target": ExecutionTarget.REMOTE}, id="remote-tab"),
        pytest.param({"workspace_role": "reviewer"}, id="workspace-role"),
    ],
)
def test_ineligible_tabs_are_refused(monkeypatch, overrides) -> None:
    _install_chat_tab(monkeypatch, **overrides)

    with pytest.raises(HTTPException) as raised:
        bot_api._validate_bind_target("tab-1", "ws-1")

    assert raised.value.status_code == 400
    assert raised.value.detail == "chat_tab_not_eligible"


@pytest.mark.parametrize(
    "overrides,tab_id",
    [
        pytest.param({}, "tab-missing", id="unknown-tab"),
        pytest.param({"with_process": False}, "tab-1", id="no-process"),
        pytest.param({"archived": True}, "tab-1", id="archived"),
    ],
)
def test_unusable_tabs_are_not_found(monkeypatch, overrides, tab_id) -> None:
    _install_chat_tab(monkeypatch, **overrides)

    with pytest.raises(HTTPException) as raised:
        bot_api._validate_bind_target(tab_id, None)

    assert raised.value.status_code == 404
    assert raised.value.detail == "chat_tab_not_found"


def test_an_unscoped_tab_resolves_to_no_workspace(monkeypatch) -> None:
    _install_chat_tab(monkeypatch, workspace_id=None)

    assert bot_api._validate_bind_target("tab-1", None) is None


@pytest.mark.asyncio
async def test_no_confirmation_word_is_sent_when_the_hub_owner_lost_authorization(env):
    """The confirmation word must not be sent after owner revocation.

    The code is still consumed, so the Hub side has to issue a new one. That
    cost is deliberate: the alternative is deciding, after the claim is already
    written, that a de-authorized owner may still be told the word.
    """

    code, _expires, _revision = env.pool.issue_code(
        env.bot_id,
        owner=env.owner,
        tab_id="tab-target",
        workspace_id=env.target["workspace_id"],
        expected_revision=revision(env),
    )
    env.authorized[0] = False
    effective = env.pool.effective(env.bot_id)

    delivered = await bot_api._handle_pairing_code(
        env.bot_id,
        replace(message(), text=code),
        effective,
        bot_api.FeishuBotClient(effective.config),
        code,
    )

    assert delivered is False
    assert env.replies == []
    entry = env.pool.snapshot().get(env.bot_id)
    assert entry.binding is None
    # The claim exists, so the burned code cannot be silently reused.
    assert len(entry.claims) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason,fragment",
    [
        pytest.param("chat_busy", "正在处理其他消息", id="busy"),
        pytest.param(None, "请在网页检查 Chat 状态后重试", id="unknown-409"),
    ],
)
async def test_chat_conflict_answers_the_original_message_without_claiming_rebinding(
    env, monkeypatch, reason, fragment
):
    """A busy or unavailable Chat is a retry, not a broken pairing.

    Telling the user to re-pair here would have them tear down a binding that
    still works, so that wording stays reserved for binding_target_missing.
    """

    activate(env)
    effective = env.pool.effective(env.bot_id)

    async def conflict(*args, **kwargs):
        headers = {stream_api.CHAT_ERROR_REASON_HEADER: reason} if reason else None
        raise HTTPException(status_code=409, detail="conflict", headers=headers)

    monkeypatch.setattr(bot_api, "dispatch_tab_chat_and_wait", conflict)

    await bot_api._handle_message_event(env.bot_id, message(), effective)

    assert len(env.replies) == 1
    message_id, text = env.replies[0]
    assert message_id == "om-1"
    assert fragment in text
    assert "重新配对" not in text
