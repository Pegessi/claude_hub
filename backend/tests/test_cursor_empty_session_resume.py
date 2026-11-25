from __future__ import annotations

import importlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from claude_hub.models import (
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream.native import CursorNativeSession
from claude_hub.services.agent_stream.providers.contracts import ProviderRpcError
from claude_hub.services.agent_stream.providers.cursor_helpers import _is_empty_acp_shell


def session(cwd: Path, sid: str) -> ManagedSession:
    return ManagedSession(
        id=str(uuid.uuid4()),
        workspace_id=str(uuid.uuid4()),
        tab_id=str(uuid.uuid4()),
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CURSOR,
        status=ManagedSessionStatus.IDLE,
        title="test",
        workspace_path=str(cwd),
        tmux_session="test",
        target=ExecutionTarget.LOCAL,
        solo_mode=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        agent_session_id=sid,
        agent_session_id_verified=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "turns,required,db_exists,should_replace",
    [
        (0, False, False, True),
        (1, False, False, False),
        (0, True, False, False),
        (0, False, True, False),
    ],
)
async def test_empty_shell_recovery_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    turns: int,
    required: bool,
    db_exists: bool,
    should_replace: bool,
) -> None:
    old, new = str(uuid.uuid4()), str(uuid.uuid4())
    config = tmp_path / "config"
    shell = config / "acp-sessions" / old
    shell.mkdir(parents=True)
    (shell / "meta.json").write_text(json.dumps({"cwd": str(tmp_path)}))
    if db_exists:
        (shell / "store.db").write_bytes(b"corrupt")
    env = {"HOME": str(tmp_path), "CURSOR_CONFIG_DIR": str(config)}
    native = CursorNativeSession(session(tmp_path, old))
    native._required_conversation_id = old if required else None
    native._startup_request = AsyncMock(side_effect=ProviderRpcError("missing", code=-32602))  # type: ignore[method-assign]
    native._send_request = AsyncMock(return_value={"sessionId": new})  # type: ignore[method-assign]
    native._ensure_session_model = AsyncMock()  # type: ignore[method-assign]
    native._ensure_session_mode = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr(
        importlib.import_module("claude_hub.services.workspace_manager"),
        "STATE_ROOT",
        tmp_path / "hub",
    )
    if turns:
        from claude_hub.models import AgentStreamEventType
        from claude_hub.services.agent_stream.base import NormalizeContext
        from claude_hub.services.agent_stream.store import AgentStreamStore

        context = NormalizeContext(
            session_id=native.session.id,
            tab_id=native.session.tab_id,
            agent_type=AgentType.CURSOR,
            run_epoch=1,
            turn_id=str(uuid.uuid4()),
        )
        await AgentStreamStore(native.session.workspace_id, native.session.id).append(
            context.event(AgentStreamEventType.TURN_STARTED, {"summary": "prior"})
        )
    if should_replace:
        await native._initialize_session(env)
        assert native._conversation_id == new
        assert native._conversation_id_verified
        native._ensure_session_model.assert_awaited_once()
        native._ensure_session_mode.assert_awaited_once()
    else:
        with pytest.raises(RuntimeError, match="could not resume"):
            await native._initialize_session(env)
        native._send_request.assert_not_awaited()
        assert native._conversation_id == old


def test_shell_detection_requires_metadata_and_no_legacy_store(tmp_path: Path) -> None:
    sid = str(uuid.uuid4())
    env = {"HOME": str(tmp_path), "CURSOR_CONFIG_DIR": str(tmp_path / "config")}
    shell = tmp_path / "config" / "acp-sessions" / sid
    shell.mkdir(parents=True)
    (shell / "meta.json").write_text(json.dumps({"cwd": str(tmp_path)}))
    assert _is_empty_acp_shell(sid, str(tmp_path), env)
    from claude_hub.services._cursor_verify import _cwd_hash

    legacy = tmp_path / ".cursor" / "chats" / _cwd_hash(str(tmp_path)) / str(uuid.uuid4())
    legacy.mkdir(parents=True)
    (legacy / "store.db").write_bytes(b"corrupt")
    assert not _is_empty_acp_shell(sid, str(tmp_path), env)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_tailer_first_send_replaces_empty_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    import asyncio

    from claude_hub.services.agent_stream.cursor_cli_transcript import CursorCliTranscriptAdapter
    from claude_hub.services.agent_stream.store import AgentStreamStore
    from claude_hub.services.agent_stream.tailer import SessionTailer
    from tests.test_agent_stream_native import _InteractiveAcpProc

    monkeypatch.setattr(
        importlib.import_module("claude_hub.services.workspace_manager"),
        "STATE_ROOT",
        tmp_path / "hub",
    )
    old, new = str(uuid.uuid4()), str(uuid.uuid4())
    env = {"HOME": str(tmp_path), "CURSOR_CONFIG_DIR": str(tmp_path / "config"), "PATH": "/bin"}
    shell = tmp_path / "config" / "acp-sessions" / old
    shell.mkdir(parents=True)
    (shell / "meta.json").write_text(json.dumps({"cwd": str(tmp_path)}))

    class PromptProc(_InteractiveAcpProc):
        def _on_stdin(self, msg: dict) -> None:
            if msg.get("method") == "session/prompt":
                self.requests.append(msg)
                target = tmp_path / "config" / "acp-sessions" / new
                target.mkdir(parents=True, exist_ok=True)
                (target / "store.db").write_bytes(b"store")
                self.stdout.push_json(
                    {"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}}
                )
            else:
                super()._on_stdin(msg)

    sess = session(tmp_path, old)
    if not legacy:
        sess.agent_session_id = None
        sess.agent_session_id_verified = False
    sess.env = {"CURSOR_MODEL": "first-model"}
    persisted: list[str] = []
    native = CursorNativeSession(sess, conversation_id_persist=persisted.append)
    first = _InteractiveAcpProc(session_id=old)
    second = PromptProc(session_id=new)
    native._build_provider_env = AsyncMock(return_value=env)  # type: ignore[method-assign]
    native._spawn_subprocess = AsyncMock(side_effect=[second] if legacy else [first, second])  # type: ignore[method-assign]
    store = AgentStreamStore(sess.workspace_id, sess.id)
    tailer = SessionTailer(
        workspace_id=sess.workspace_id,
        session_id=sess.id,
        adapter=CursorCliTranscriptAdapter(),
        session_getter=lambda: sess,
        store=store,
        native_transport=native,
    )
    queue = await tailer.subscribe()
    try:
        for _ in range(100):
            if native._started:
                break
            await asyncio.sleep(0.01)
        assert native._started
        if not legacy:
            native.update_env({"CURSOR_MODEL": "second-model"})
        await tailer.send_message("hello", [], client_turn_id=str(uuid.uuid4()))
        assert persisted[-1] == new
        assert any(
            r.get("method") == "session/prompt" and r["params"]["sessionId"] == new
            for r in second.requests
        )
        assert not any(r.get("method") == "session/load" for r in second.requests)
        if not legacy:
            assert any(
                r.get("method") == "session/set_model" and r["params"]["modelId"] == "second-model"
                for r in second.requests
            )
        assert await store.count_turns(strict=True) == 1
    finally:
        tailer.unsubscribe(queue)
        await native.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [False, True])
async def test_pending_turn_is_excluded_only_without_provider_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: bool
) -> None:
    from claude_hub.models import AgentStreamEventType
    from claude_hub.services.agent_stream.base import NormalizeContext
    from claude_hub.services.agent_stream.providers.cursor_helpers import _has_no_prior_turns
    from claude_hub.services.agent_stream.store import AgentStreamStore

    monkeypatch.setattr(
        importlib.import_module("claude_hub.services.workspace_manager"),
        "STATE_ROOT",
        tmp_path / "hub",
    )
    sess = session(tmp_path, str(uuid.uuid4()))
    turn = str(uuid.uuid4())
    ctx = NormalizeContext(
        session_id=sess.id,
        tab_id=sess.tab_id,
        agent_type=AgentType.CURSOR,
        run_epoch=1,
        turn_id=turn,
    )
    store = AgentStreamStore(sess.workspace_id, sess.id)
    await store.append(ctx.event(AgentStreamEventType.TURN_STARTED, {"summary": "hello"}))
    if extra:
        await store.append(ctx.event(AgentStreamEventType.TEXT_DELTA, {"text": "response"}))
    assert await _has_no_prior_turns(sess, turn) is not extra
    assert not await _has_no_prior_turns(sess, str(uuid.uuid4()))
