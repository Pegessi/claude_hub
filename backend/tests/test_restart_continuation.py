"""Actual stream/mailbox/native guards, with provider execution replaced in memory."""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from claude_hub.models import AgentType, ChatMode, ExecutionTarget, SessionKind
from claude_hub.services import restart_continuation as recovery
from claude_hub.services import service_restart as restart
from claude_hub.services.agent_stream.native import (
    ClaudeNativeSession,
    CodexNativeSession,
    CursorNativeSession,
    ProviderSession,
)


class MemoryProvider(ProviderSession):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.inputs: list[str] = []

    def _build_command(self) -> list[str]:
        return []

    async def _send_text(self, text: str) -> None:
        pass

    async def send_message(self, text: str, images: list[bytes]) -> None:
        self.inputs.append(text)
        await super().send_message(text, images)


@pytest.fixture
async def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from claude_hub.api import agent_stream as api
    from claude_hub.services import goal_run, ttyd_manager, workspace_manager
    from claude_hub.services.agent_stream import tailer as tailer_module
    from claude_hub.services.goal_run.controller import GoalRunController
    from claude_hub.services.goal_run.store import GoalRunStore

    state_root = tmp_path / "state"
    state_root.mkdir()
    wm_module = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(wm_module, "STATE_ROOT", state_root)
    monkeypatch.setattr(api, "_tab_tailer_manager", None)
    monkeypatch.setattr(api, "_native_input_shutting_down", False)
    monkeypatch.setattr(workspace_manager, "sessions", {})
    monkeypatch.setattr(workspace_manager, "scheduled_task_runs", {})
    monkeypatch.setattr(
        goal_run,
        "goal_run_controller",
        GoalRunController(GoalRunStore(tmp_path / "goals.json")),
    )
    monkeypatch.setattr(goal_run, "_tab_admission_locks", {})
    tab = SimpleNamespace(
        id="restart-tab",
        name="Chat",
        cwd=str(tmp_path),
        env={},
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
        chat_mode=ChatMode.DEFAULT,
        target=ExecutionTarget.LOCAL,
        remote_cwd=None,
        remote_profile_id=None,
        remote_reconnect=True,
        solo_mode=True,
        cursor_transport="native",
        cursor_data_dir=None,
        cursor_cli_version=None,
        cursor_transcript_path=None,
        cursor_transcript_schema=None,
        agent_session_id="verified-conversation",
        agent_session_id_verified=True,
        archived=False,
        workspace_id=None,
        workspace_role=None,
    )
    monkeypatch.setattr(ttyd_manager, "get_tab", lambda tab_id: tab if tab_id == tab.id else None)
    providers: list[MemoryProvider] = []

    def factory(*args: Any, **kwargs: Any) -> MemoryProvider:
        provider = MemoryProvider(*args, **kwargs)
        providers.append(provider)
        return provider

    monkeypatch.setattr(tailer_module, "create_native_session", factory)
    mailbox = restart.RestartStore(tmp_path / "runtime")
    mailbox.initialize("launcher")
    monkeypatch.setattr(restart, "get_store", lambda: mailbox)
    monkeypatch.setenv("CLAUDE_HUB_LAUNCHER_ID", "launcher")
    monkeypatch.setattr(restart, "INSTANCE_ID", "before-restart")
    with mailbox.locked() as state:
        state["operation"] = {
            "id": "restart-op",
            "instance_id": "before-restart",
            "status": "restarting",
            "resume_running_chats": True,
        }

    async def work(text: str = "完成任务", origin: str = "web", turn_id: str = "original-turn"):
        session = api._terminal_tab_session_or_404(tab.id)
        manager = api._get_tab_tailer_manager()
        await manager.send_message(session, text, [], turn_id, turn_metadata={"origin": origin})
        return manager.get_tailer(session.id)

    async def replace():
        manager = api._tab_tailer_manager
        if manager is not None:
            manager.quiesce()
        await recovery.capture_interrupted_chats()
        if manager is not None:
            await manager.stop_all()
        api._tab_tailer_manager = None
        monkeypatch.setattr(restart, "INSTANCE_ID", "after-restart")

    harness = SimpleNamespace(
        tab=tab,
        api=api,
        mailbox=mailbox,
        providers=providers,
        work=work,
        replace=replace,
        monkeypatch=monkeypatch,
    )
    try:
        yield harness
    finally:
        if api._tab_tailer_manager is not None:
            await api._tab_tailer_manager.stop_all()


@pytest.mark.parametrize("text", ["请完成中文任务", "Finish this English task"])
async def test_planned_restart_sends_fixed_prompt_once_in_same_conversation(setup, text):
    h = setup
    await h.work(text)
    await h.replace()
    await asyncio.gather(
        recovery.continue_interrupted_chats(), recovery.continue_interrupted_chats()
    )
    await recovery.continue_interrupted_chats()
    assert len(h.providers) == 2
    assert h.providers[1].inputs == ["Please continue."]
    assert h.providers[1]._conversation_id == "verified-conversation"
    public = restart.public_status(h.mailbox.read())
    assert "chat_recovery" not in public["operation"]
    assert public["operation"]["continuation"]["started"] == 1


@pytest.mark.parametrize(
    "reason",
    [
        "idle",
        "question",
        "capacity",
        "plan",
        "managed",
        "unverified",
        "goal",
        "scheduled",
        "feishu",
        "stop",
    ],
)
async def test_noneligible_chat_is_not_captured(setup, reason):
    h = setup
    origin = (
        "scheduled_task" if reason == "scheduled" else "feishu" if reason == "feishu" else "web"
    )
    tailer = await h.work(origin=origin)
    if reason == "idle":
        h.providers[0]._end_turn()
    elif reason == "question":
        tailer._blocking_approval_call_ids.add("question")
    elif reason == "capacity":
        tailer._waiting_for_model_capacity = True
    elif reason == "plan":
        h.tab.chat_mode = ChatMode.PLAN
    elif reason == "managed":
        h.tab.workspace_id = "managed-workspace"
    elif reason == "unverified":
        h.tab.agent_session_id_verified = False
    elif reason == "goal":
        from claude_hub.services.goal_run import get_goal_manager

        h.monkeypatch.setattr(
            get_goal_manager(),
            "current",
            lambda _: SimpleNamespace(
                status="paused", dispatch_state=SimpleNamespace(value="idle")
            ),
        )
    elif reason == "stop":
        await tailer.cancel_turn("original-turn")
    await h.replace()
    assert h.mailbox.read()["operation"]["chat_recovery"]["targets"] == []
    await recovery.continue_interrupted_chats()
    assert len(h.providers) == 1


@pytest.mark.parametrize(
    "change",
    [
        "model",
        "global_model",
        "conversation",
        "archive",
        "claimed",
        "user_stop",
        "new_turn",
    ],
)
async def test_startup_rechecks_identity_and_prior_claims(setup, change):
    h = setup
    await h.work()
    await h.replace()
    if change == "model":
        h.tab.env["MODEL"] = "different"
    elif change == "global_model":
        h.monkeypatch.setenv("ANTHROPIC_MODEL", "different-default")
    elif change == "conversation":
        h.tab.agent_session_id = "another-conversation"
    elif change == "archive":
        h.tab.archived = True
    elif change == "claimed":
        with h.mailbox.locked() as state:
            state["operation"]["chat_recovery"]["targets"][0]["state"] = "claimed"
    elif change == "user_stop":
        recovery.cancel_pending_continuation(h.tab.id, "original-turn")
    elif change == "new_turn":
        await h.work("new manual message", turn_id="new-manual")
    await recovery.continue_interrupted_chats()
    assert all("Please continue." not in provider.inputs for provider in h.providers)


@pytest.mark.parametrize(
    "flag,status", [(False, "restarting"), (True, "preparing"), (True, "succeeded")]
)
async def test_only_opted_in_shutdown_is_captured(setup, flag, status):
    h = setup
    await h.work()
    with h.mailbox.locked() as state:
        state["operation"].update(resume_running_chats=flag, status=status)
    await h.replace()
    assert "chat_recovery" not in h.mailbox.read()["operation"]


@pytest.mark.parametrize("bad", [None, [], {"targets": None}, {"targets": [None]}])
async def test_bad_recovery_never_breaks_stop_or_replays(setup, bad):
    h = setup
    await h.replace()
    with h.mailbox.locked() as state:
        state["operation"]["chat_recovery"] = bad
    await recovery.continue_interrupted_chats()
    assert recovery.cancel_pending_continuation(h.tab.id, None) is None
    restart.public_status(h.mailbox.read())
    assert not h.providers


async def test_generic_stop_targets_new_manual_turn_after_continuation(setup):
    h = setup
    await h.work()
    await h.replace()
    await recovery.continue_interrupted_chats()
    session = h.api._terminal_tab_session_or_404(h.tab.id)
    manager = h.api._get_tab_tailer_manager()
    await manager.cancel_turn(session)
    await manager.send_message(
        session, "new work", [], "new-manual", turn_metadata={"origin": "web"}
    )
    result = await h.api.cancel_tab_stream_turn(h.tab.id, None, SimpleNamespace())
    assert result["cancelled"] is True
    await manager.get_tailer(session.id)._await_turn_teardown()
    assert not h.providers[-1].turn_in_flight


@pytest.mark.parametrize("status", [[], {}, None])
async def test_malformed_restart_status_does_not_block_manual_stop(setup, status):
    h = setup
    await h.work()
    with h.mailbox.locked() as state:
        state["operation"]["status"] = status
    await recovery.continue_interrupted_chats()
    result = await h.api.cancel_tab_stream_turn(h.tab.id, None, SimpleNamespace())
    assert result["cancelled"] is True


async def test_stop_during_async_validation_prevents_delivery(setup):
    h = setup
    await h.work()
    await h.replace()
    original = recovery._verify_interruption

    async def stopped(store, target):
        await original(store, target)
        recovery.cancel_pending_continuation(h.tab.id, "original-turn")

    h.monkeypatch.setattr(recovery, "_verify_interruption", stopped)
    await recovery.continue_interrupted_chats()
    assert all("Please continue." not in provider.inputs for provider in h.providers)


async def test_stop_during_turn_publication_prevents_provider_delivery(setup):
    from claude_hub.services.agent_stream.tailer import SessionTailer

    h = setup
    await h.work()
    await h.replace()
    original = SessionTailer._publish

    async def stopped(tailer, event):
        result = await original(tailer, event)
        if event.type == "turn_started":
            recovery.cancel_pending_continuation(h.tab.id, "original-turn")
        return result

    h.monkeypatch.setattr(SessionTailer, "_publish", stopped)
    await recovery.continue_interrupted_chats()
    assert all("Please continue." not in provider.inputs for provider in h.providers)


async def test_shutdown_barrier_rejects_observer_dispatch(setup):
    h = setup
    await h.work()
    manager = h.api._get_tab_tailer_manager()
    manager.quiesce()
    session = h.api._terminal_tab_session_or_404(h.tab.id)
    with pytest.raises(RuntimeError, match="shutting down"):
        await manager.send_message(session, "observer retry", [], "observer")


@pytest.mark.parametrize("change", ["config", "conversation", "launcher", "archive"])
async def test_identity_change_during_turn_publication_prevents_delivery(setup, change):
    from claude_hub.services.agent_stream.tailer import SessionTailer

    h = setup
    await h.work()
    await h.replace()
    original = SessionTailer._publish

    async def changed(tailer, event):
        result = await original(tailer, event)
        if event.type == "turn_started":
            if change == "config":
                h.tab.env["ANTHROPIC_BASE_URL"] = "https://changed-provider.invalid"
            elif change == "conversation":
                h.tab.agent_session_id = "another-conversation"
            elif change == "archive":
                h.tab.archived = True
            elif change == "launcher":
                with h.mailbox.locked() as state:
                    state["launcher_id"] = "another-launcher"
        return result

    h.monkeypatch.setattr(SessionTailer, "_publish", changed)
    await recovery.continue_interrupted_chats()
    assert all("Please continue." not in provider.inputs for provider in h.providers)


@pytest.mark.parametrize("boundary", ["environment", "terminate", "spawn"])
@pytest.mark.parametrize("change", [None, "stop", "config", "conversation", "shutdown"])
@pytest.mark.parametrize("provider_type", [AgentType.CLAUDE, AgentType.CURSOR])
async def test_oneshot_rechecks_intent_after_async_preparation(
    setup, boundary, change, provider_type
):
    """Use actual Claude/Cursor stdin paths; replace external process I/O."""
    from claude_hub.services.agent_stream import tailer as tailer_module

    h = setup
    h.tab.agent_type = provider_type
    await h.work()
    await h.replace()
    writes = []
    processes = []
    providers = []
    changed = False

    def mutate():
        nonlocal changed
        if changed:
            return
        changed = True
        if change == "stop":
            recovery.cancel_pending_continuation(h.tab.id, "original-turn")
        elif change == "config":
            h.tab.env["ANTHROPIC_BASE_URL"] = "https://changed-provider.invalid"
        elif change == "conversation":
            h.tab.agent_session_id = "another-conversation"
        elif change == "shutdown":
            h.api._quiesce_tailer_managers()

    async def environment(provider):
        await asyncio.sleep(0)
        if boundary == "environment":
            mutate()
        return {"CLAUDE_HUB_PROVIDER_NETWORK_MODE": "inherit"}

    original_terminate = ProviderSession._terminate_process

    async def terminate(provider):
        await original_terminate(provider)
        if boundary == "terminate":
            mutate()

    async def spawn(*args, **kwargs):
        await asyncio.sleep(0)
        if boundary == "spawn":
            mutate()
        process = SimpleNamespace(
            stdin=SimpleNamespace(write=writes.append, drain=AsyncMock(), close=lambda: None),
            stdout=asyncio.StreamReader(),
            stderr=asyncio.StreamReader(),
            returncode=None,
            wait=AsyncMock(return_value=0),
        )
        process.terminate = lambda: setattr(process, "returncode", 0)
        process.kill = process.terminate
        processes.append(process)
        return process

    def factory(*args, **kwargs):
        provider_class = (
            ClaudeNativeSession if provider_type == AgentType.CLAUDE else CursorNativeSession
        )
        provider = provider_class(*args, **kwargs)
        providers.append(provider)
        return provider

    h.monkeypatch.setattr(tailer_module, "create_native_session", factory)
    h.monkeypatch.setattr(ClaudeNativeSession, "_build_provider_env", environment)
    h.monkeypatch.setattr(ProviderSession, "_build_provider_env", environment)
    h.monkeypatch.setattr(ProviderSession, "_terminate_process", terminate)
    h.monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    # The separate Cursor database tests own store verification; this test
    # isolates input admission through the actual Cursor one-shot lifecycle.
    h.monkeypatch.setattr(recovery, "_cursor_database_exists", lambda _: True)
    await recovery.continue_interrupted_chats()
    if change is None:
        assert len(writes) == 1
        assert "Please continue." in writes[0].decode()
    else:
        assert writes == []
        assert all(process.returncode == 0 for process in processes)
    assert providers[0]._delivery_admission is None
    assert providers[0]._required_conversation_id is None
    await recovery.continue_interrupted_chats()
    assert len(writes) == int(change is None)


async def test_shutdown_barrier_rejects_lazy_manager_creation(setup):
    h = setup
    h.api._quiesce_tailer_managers()
    for getter in (h.api._get_tab_tailer_manager, h.api._get_tailer_manager):
        with pytest.raises(RuntimeError, match="shutting down"):
            getter()
    assert h.api._tab_tailer_manager is None
    assert not h.providers


async def test_retry_during_provider_start_cannot_move_continuation_to_new_runtime(setup):
    from claude_hub.services.agent_stream import tailer as tailer_module

    h = setup
    await h.work()
    await h.replace()
    entered = asyncio.Event()
    release = asyncio.Event()
    providers = []
    original_start = ProviderSession.start
    spawn = AsyncMock(side_effect=AssertionError("provider must not receive a continuation"))

    async def start(provider):
        await original_start(provider)
        if provider is providers[0] and not entered.is_set():
            entered.set()
            await release.wait()

    def factory(*args, **kwargs):
        provider = ClaudeNativeSession(*args, **kwargs)
        providers.append(provider)
        return provider

    h.monkeypatch.setattr(tailer_module, "create_native_session", factory)
    h.monkeypatch.setattr(ClaudeNativeSession, "start", start)
    h.monkeypatch.setattr(ProviderSession, "_spawn_oneshot", spawn)
    pending = asyncio.create_task(recovery.continue_interrupted_chats())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        session = h.api._terminal_tab_session_or_404(h.tab.id)
        replacement = await h.api._get_tab_tailer_manager().retry(session)
        assert replacement.native_transport is not providers[0]
        release.set()
        await asyncio.wait_for(pending, 2)
        spawn.assert_not_awaited()
        assert h.mailbox.read()["operation"]["chat_recovery"]["targets"][0]["state"] == "skipped"
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_shutdown_during_turn_publication_prevents_delivery(setup):
    from claude_hub.services.agent_stream.tailer import SessionTailer

    h = setup
    original = SessionTailer._publish

    async def quiesce(tailer, event):
        result = await original(tailer, event)
        if event.type == "turn_started":
            h.api._quiesce_tailer_managers()
        return result

    h.monkeypatch.setattr(SessionTailer, "_publish", quiesce)
    with pytest.raises(RuntimeError, match="shutting down"):
        await h.work()
    assert h.providers[0].inputs == []


@pytest.mark.parametrize("age", [-1, recovery.MAX_RECOVERY_AGE_SECONDS + 1])
async def test_expired_or_future_recovery_requires_manual_attention(setup, age):
    h = setup
    await h.work()
    await h.replace()
    with h.mailbox.locked() as state:
        state["operation"]["chat_recovery"]["captured_at"] = recovery.time.time() - age
    await recovery.continue_interrupted_chats()
    assert len(h.providers) == 1
    assert restart.public_status(h.mailbox.read())["operation"]["continuation"]["attention"] == 1


async def test_prior_process_claim_is_reported_without_replay(setup):
    h = setup
    await h.work()
    await h.replace()
    with h.mailbox.locked() as state:
        state["operation"]["chat_recovery"]["targets"][0].update(
            state="claimed", claimed_by_instance_id="an-earlier-recovery-process"
        )
    await recovery.continue_interrupted_chats()
    assert len(h.providers) == 1
    assert restart.public_status(h.mailbox.read())["operation"]["continuation"]["attention"] == 1


@pytest.mark.parametrize(
    "reply", [None, {}, {"thread": {"id": "different"}}, RuntimeError("not found")]
)
async def test_strict_codex_resume_cannot_start_blank_thread(setup, reply):
    session = setup.api._terminal_tab_session_or_404(setup.tab.id)
    session.agent_type = AgentType.CODEX
    provider = CodexNativeSession(session)
    provider.require_resumed_conversation(session.agent_session_id)
    request = (
        AsyncMock(side_effect=reply)
        if isinstance(reply, Exception)
        else AsyncMock(return_value=reply)
    )
    setup.monkeypatch.setattr(provider, "_send_request", request)
    try:
        await provider._initialize_thread()
        provider.check_resumed_conversation()
    except RuntimeError:
        pass
    else:
        pytest.fail("strict resume accepted a missing/different conversation")
    assert [call.args[0] for call in request.await_args_list] == ["thread/resume"]


@pytest.mark.parametrize(
    "store_kind",
    [
        "matching",
        "missing",
        "metadata_only",
        "wrong",
        "duplicate",
        "symlink",
        "conflicting_root",
    ],
)
def test_cursor_requires_unique_database_in_effective_workspace(setup, tmp_path, store_kind):
    from claude_hub.services._cursor_verify import _cwd_hash

    h = setup
    root = tmp_path / "cursor #? home"
    h.tab.env["CURSOR_HOME"] = str(root)
    session = h.api._terminal_tab_session_or_404(h.tab.id)
    provider = MemoryProvider(session)
    bucket = root / "chats" / _cwd_hash(session.workspace_path)
    chat = bucket / "chat-dir"
    chat.mkdir(parents=True)

    def database(path, sid):
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE meta (key INTEGER, value TEXT)")
            db.execute(
                "INSERT INTO meta VALUES (0, ?)",
                (json.dumps({"agentId": sid}).encode().hex(),),
            )

    if store_kind == "metadata_only":
        (chat / "meta.json").write_text(json.dumps({"cwd": session.workspace_path}))
    elif store_kind != "missing":
        database(
            chat / "store.db",
            "wrong" if store_kind == "wrong" else session.agent_session_id,
        )
        if store_kind == "duplicate":
            other = bucket / "other"
            other.mkdir()
            database(other / "store.db", session.agent_session_id)
        elif store_kind == "symlink":
            (chat / "store.db").rename(tmp_path / "outside.db")
            (chat / "store.db").symlink_to(tmp_path / "outside.db")
        elif store_kind == "conflicting_root":
            provider.session.env["CURSOR_DATA_DIR"] = str(tmp_path / "other-root")
    assert recovery._cursor_database_exists(provider) is (store_kind == "matching")
