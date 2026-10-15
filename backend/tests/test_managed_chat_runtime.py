from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from claude_hub.api import agent_stream as agent_stream_api
from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    User,
    WorkspaceAttachmentCreate,
    WorkspaceSessionRole,
)
from claude_hub.services import managed_chat
from claude_hub.services.agent_stream.tailer import TailerManager, discard_session_stream

_PNG = (
    b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + (1).to_bytes(4, "big") + (1).to_bytes(4, "big")
)


def _user() -> User:
    return User(open_id="test-user", name="Test User", email="test@example.com")


def _session() -> ManagedSession:
    now = datetime.now()
    return ManagedSession(
        id="managed-chat-1",
        workspace_id="workspace-1",
        tab_id="tab-1",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        title="worker",
        workspace_path="/tmp/workspace",
        tmux_session="managed-chat-1",
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )


class _FakeManager:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.on_send: Any = None
        self.tailer: Any = None
        self.cancelled: list[str | None] = []
        self.forgotten: list[str] = []
        self.forget_require_fenced: list[bool] = []
        self.forget_error: Exception | None = None

    async def send_message(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))
        if self.on_send is not None:
            await self.on_send()

    async def cancel_turn(
        self, session: ManagedSession, expected_turn_id: str | None = None
    ) -> bool:
        self.cancelled.append(expected_turn_id)
        return True

    async def forget_session(self, session_id: str, *, require_fenced: bool = False) -> bool:
        self.forgotten.append(session_id)
        self.forget_require_fenced.append(require_fenced)
        if self.forget_error is not None:
            raise self.forget_error
        return True

    def get_tailer(self, session_id: str) -> Any:
        return self.tailer


@pytest.fixture
def isolated_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeManager:
    import importlib

    workspace_manager_module = importlib.import_module("claude_hub.services.workspace_manager")

    fake = _FakeManager()
    monkeypatch.setattr(workspace_manager_module, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(managed_chat, "_manager", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_send_persists_attempt_before_handoff_and_accepts_once(
    isolated_bridge: _FakeManager,
) -> None:
    session = _session()

    async def verify_sending() -> None:
        assert managed_chat.delivery_state(session, "call-1") == "sending"

    isolated_bridge.on_send = verify_sending
    await managed_chat.send_message(session, "hello", [], "call-1")
    assert managed_chat.delivery_state(session, "call-1") == "accepted"
    assert isolated_bridge.calls[0][0][3] == "call-1"

    await managed_chat.send_message(session, "hello", [], "call-1")
    assert len(isolated_bridge.calls) == 1

    with pytest.raises(ValueError, match="different Chat payload"):
        await managed_chat.send_message(session, "changed", [], "call-1")


@pytest.mark.asyncio
async def test_send_failure_becomes_uncertain_and_is_never_replayed(
    isolated_bridge: _FakeManager,
) -> None:
    session = _session()

    async def fail() -> None:
        raise RuntimeError("transport failed")

    isolated_bridge.on_send = fail
    with pytest.raises(RuntimeError, match="transport failed"):
        await managed_chat.send_message(session, "hello", [], "call-2")
    assert managed_chat.delivery_state(session, "call-2") == "uncertain"

    with pytest.raises(managed_chat.ManagedChatDeliveryUncertain):
        await managed_chat.send_message(session, "hello", [], "call-2")
    assert len(isolated_bridge.calls) == 1


@pytest.mark.asyncio
async def test_cold_restart_classifies_sending_evidence_as_uncertain(
    isolated_bridge: _FakeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    evidence = {
        "version": 1,
        "attempts": {
            "call-3": {
                "payload_sha256": "digest",
                "process_token": "previous-process",
                "state": "sending",
                "turn_id": "call-3",
            }
        },
    }
    path = managed_chat._attempt_path(session)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(evidence), encoding="utf-8")

    assert managed_chat.delivery_state(session, "call-3") == "uncertain"
    assert managed_chat.runtime_status(session) == AgentRuntimeStatus.ATTENTION


@pytest.mark.asyncio
async def test_cold_restart_accepted_unfinished_turn_requires_attention(
    isolated_bridge: _FakeManager,
) -> None:
    session = _session()
    evidence = {
        "version": 1,
        "attempts": {
            "call-orphan": {
                "payload_sha256": "digest",
                "process_token": "previous-process",
                "state": "accepted",
                "turn_id": "call-orphan",
            }
        },
    }
    attempt_path = managed_chat._attempt_path(session)
    attempt_path.parent.mkdir(parents=True)
    attempt_path.write_text(json.dumps(evidence), encoding="utf-8")
    stream_path = managed_chat.AgentStreamStore(session.workspace_id, session.id).path
    stream_path.write_text(
        json.dumps(
            {
                "type": "turn_started",
                "turn_id": "call-orphan",
                "run_epoch": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert managed_chat.is_turn_running(session) is True
    assert managed_chat.runtime_status(session) == AgentRuntimeStatus.ATTENTION


@pytest.mark.asyncio
async def test_structured_image_attachment_uses_native_bytes(
    isolated_bridge: _FakeManager,
) -> None:
    session = _session()
    encoded = __import__("base64").b64encode(_PNG).decode()
    attachment = WorkspaceAttachmentCreate(
        filename="image.png",
        mime_type="image/png",
        data_url=f"data:image/png;base64,{encoded}",
    )

    await managed_chat.send_message(session, "inspect", [attachment], "call-image")

    args, kwargs = isolated_bridge.calls[0]
    assert args[2] == [_PNG]
    assert kwargs["previews"] == [_PNG]


@pytest.mark.asyncio
async def test_cancel_targets_only_expected_turn(isolated_bridge: _FakeManager) -> None:
    session = _session()

    assert await managed_chat.cancel(session, expected_turn_id="turn-7") is True
    assert isolated_bridge.cancelled == ["turn-7"]


@pytest.mark.asyncio
async def test_reset_rejects_uncertain_without_erasing_maso_sidecar(
    isolated_bridge: _FakeManager,
) -> None:
    session = _session()
    attempt = {
        "version": 1,
        "attempts": {
            "call-4": {
                "payload_sha256": "digest",
                "process_token": "old-process",
                "state": "uncertain",
                "turn_id": "call-4",
            }
        },
    }
    path = managed_chat._attempt_path(session)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(attempt), encoding="utf-8")
    maso_path = managed_chat._maso_delivery_path(session)
    maso_path.write_text('{"uncertain":true}', encoding="utf-8")

    with pytest.raises(managed_chat.ManagedChatDeliveryUncertain):
        await managed_chat.reset_context(session)
    assert maso_path.exists()
    assert isolated_bridge.forgotten == []


@pytest.mark.asyncio
async def test_reset_rotates_both_identities_and_keeps_history(
    isolated_bridge: _FakeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    session.agent_session_id = "provider-old"
    session.agent_session_id_verified = True
    tab = SimpleNamespace(
        agent_session_id="provider-old",
        agent_session_id_verified=True,
    )
    monkeypatch.setattr(managed_chat.ttyd_manager, "processes", {session.tab_id: tab})
    monkeypatch.setattr(managed_chat.ttyd_manager, "_save_state", lambda: None)
    fake_workspace_manager = SimpleNamespace(
        sessions={session.id: session}, _save_state=lambda: None
    )
    monkeypatch.setattr(managed_chat, "_workspace_manager", lambda: fake_workspace_manager)
    store_path = managed_chat.AgentStreamStore(session.workspace_id, session.id).path
    store_path.parent.mkdir(parents=True)
    store_path.write_text("{}\n", encoding="utf-8")

    await managed_chat.reset_context(session)

    assert isolated_bridge.forgotten == [session.id]
    assert session.agent_session_id is None
    assert session.agent_session_id_verified is False
    assert tab.agent_session_id is None
    assert tab.agent_session_id_verified is False
    assert store_path.read_text(encoding="utf-8") == "{}\n"


def test_runtime_reads_attempt_sidecar_once(
    isolated_bridge: _FakeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    reads = 0
    original = managed_chat._read_attempts

    def counted_read(current: ManagedSession) -> dict[str, Any]:
        nonlocal reads
        reads += 1
        return original(current)

    monkeypatch.setattr(managed_chat, "_read_attempts", counted_read)

    managed_chat.runtime_status(session)
    assert reads == 1


def test_durable_lifecycle_scan_is_cached_until_file_changes(
    isolated_bridge: _FakeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    stream_path = managed_chat.AgentStreamStore(session.workspace_id, session.id).path
    stream_path.parent.mkdir(parents=True)
    stream_path.write_text(
        json.dumps({"type": "turn_completed", "turn_id": "done", "run_epoch": 1}) + "\n",
        encoding="utf-8",
    )
    assert managed_chat._durable_turn_state(session) == "terminal"

    def fail_open(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("cached lifecycle should not reopen an unchanged event log")

    monkeypatch.setattr(Path, "open", fail_open)
    assert managed_chat._durable_turn_state(session) == "terminal"


@pytest.mark.asyncio
async def test_invalid_delivery_sidecar_shape_fails_closed(
    isolated_bridge: _FakeManager,
) -> None:
    session = _session()
    path = managed_chat._attempt_path(session)
    path.parent.mkdir(parents=True)
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(managed_chat.ManagedChatDeliveryUncertain, match="invalid"):
        managed_chat.delivery_state(session, "call-invalid")
    assert managed_chat.runtime_status(session) == AgentRuntimeStatus.ATTENTION


@pytest.mark.asyncio
async def test_stream_error_does_not_terminalize_unfinished_turn(
    isolated_bridge: _FakeManager,
) -> None:
    session = _session()
    stream_path = managed_chat.AgentStreamStore(session.workspace_id, session.id).path
    stream_path.parent.mkdir(parents=True)
    stream_path.write_text(
        "\n".join(
            json.dumps(event)
            for event in (
                {"type": "turn_started", "turn_id": "call-error", "run_epoch": 1},
                {"type": "error", "turn_id": "call-error", "run_epoch": 1},
            )
        )
        + "\n",
        encoding="utf-8",
    )

    assert managed_chat.runtime_status(session) == AgentRuntimeStatus.ATTENTION
    assert managed_chat.is_turn_running(session) is True


@pytest.mark.asyncio
async def test_cold_terminal_turn_overrides_stale_working_status(
    isolated_bridge: _FakeManager,
) -> None:
    session = _session()
    session.runtime_status = AgentRuntimeStatus.WORKING
    stream_path = managed_chat.AgentStreamStore(session.workspace_id, session.id).path
    stream_path.parent.mkdir(parents=True)
    stream_path.write_text(
        "\n".join(
            json.dumps(event)
            for event in (
                {"type": "turn_started", "turn_id": "call-done", "run_epoch": 1},
                {"type": "turn_completed", "turn_id": "call-done", "run_epoch": 1},
            )
        )
        + "\n",
        encoding="utf-8",
    )

    assert managed_chat.runtime_status(session) == AgentRuntimeStatus.IDLE
    assert managed_chat.is_turn_running(session) is False


@pytest.mark.asyncio
async def test_semantic_ack_reconciles_uncertain_attempt_and_allows_reset(
    isolated_bridge: _FakeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    session.delivered_call_ids = ["call-acked"]
    attempt_path = managed_chat._attempt_path(session)
    attempt_path.parent.mkdir(parents=True)
    attempt_path.write_text(
        json.dumps(
            {
                "version": 1,
                "attempts": {
                    "call-acked": {
                        "payload_sha256": "digest",
                        "process_token": "old-process",
                        "state": "uncertain",
                        "turn_id": "call-acked",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    tab = SimpleNamespace(agent_session_id="provider-old", agent_session_id_verified=True)
    session.agent_session_id = "provider-old"
    session.agent_session_id_verified = True
    monkeypatch.setattr(managed_chat.ttyd_manager, "processes", {session.tab_id: tab})
    monkeypatch.setattr(managed_chat.ttyd_manager, "_save_state", lambda: None)
    fake_workspace_manager = SimpleNamespace(
        sessions={session.id: session}, _save_state=lambda: None
    )
    monkeypatch.setattr(managed_chat, "_workspace_manager", lambda: fake_workspace_manager)

    assert managed_chat.delivery_state(session, "call-acked") == "accepted"
    assert managed_chat.runtime_status(session) == AgentRuntimeStatus.IDLE
    await managed_chat.reset_context(session)
    assert isolated_bridge.forgotten == [session.id]


@pytest.mark.asyncio
async def test_reset_stop_failure_preserves_old_provider_identity(
    isolated_bridge: _FakeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    session.agent_session_id = "provider-old"
    session.agent_session_id_verified = True
    tab = SimpleNamespace(agent_session_id="provider-old", agent_session_id_verified=True)
    fake_workspace_manager = SimpleNamespace(
        sessions={session.id: session}, _save_state=lambda: None
    )
    monkeypatch.setattr(managed_chat, "_workspace_manager", lambda: fake_workspace_manager)
    monkeypatch.setattr(managed_chat.ttyd_manager, "processes", {session.tab_id: tab})
    monkeypatch.setattr(managed_chat.ttyd_manager, "_save_state", lambda: None)
    evidence_path = managed_chat.AgentStreamStore(session.workspace_id, session.id).path
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text("{}\n", encoding="utf-8")
    isolated_bridge.forget_error = RuntimeError("provider teardown failed")

    with pytest.raises(RuntimeError, match="provider teardown failed"):
        await managed_chat.reset_context(session)

    assert isolated_bridge.forget_require_fenced == [True]
    assert session.agent_session_id == "provider-old"
    assert session.agent_session_id_verified is True
    assert tab.agent_session_id == "provider-old"
    assert tab.agent_session_id_verified is True
    assert evidence_path.exists()


@pytest.mark.asyncio
async def test_reset_refetches_live_session_after_runtime_stop(
    isolated_bridge: _FakeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _session()
    stale.agent_session_id = "stale-provider"
    stale.agent_session_id_verified = True
    live = stale.model_copy(deep=True)
    live.agent_session_id = "live-provider"
    tab = SimpleNamespace(agent_session_id="live-provider", agent_session_id_verified=True)
    fake_workspace_manager = SimpleNamespace(sessions={live.id: live}, _save_state=lambda: None)
    monkeypatch.setattr(managed_chat, "_workspace_manager", lambda: fake_workspace_manager)
    monkeypatch.setattr(managed_chat.ttyd_manager, "processes", {live.tab_id: tab})
    monkeypatch.setattr(managed_chat.ttyd_manager, "_save_state", lambda: None)

    await managed_chat.reset_context(stale)

    assert live.agent_session_id is None
    assert live.agent_session_id_verified is False
    assert stale.agent_session_id == "stale-provider"


@pytest.mark.asyncio
async def test_explicit_discard_removes_receipts_before_reusing_session_id(
    isolated_bridge: _FakeManager,
) -> None:
    session = _session()
    await managed_chat.send_message(session, "old payload", [], "reused-call")
    stream_path = managed_chat.AgentStreamStore(session.workspace_id, session.id).path
    stream_path.write_text(
        "\n".join(
            json.dumps(event)
            for event in (
                {"type": "turn_started", "turn_id": "reused-call", "run_epoch": 1},
                {"type": "turn_completed", "turn_id": "reused-call", "run_epoch": 1},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    assert managed_chat._durable_turn_state(session) == "terminal"
    assert session.id in managed_chat._turn_state_cache

    await discard_session_stream(session.workspace_id, session.id)

    assert managed_chat.delivery_state(session, "reused-call") == "not_sent"
    assert session.id not in managed_chat._turn_state_cache
    isolated_bridge.calls.clear()
    await managed_chat.send_message(session, "new payload", [], "reused-call")
    assert len(isolated_bridge.calls) == 1


@pytest.mark.asyncio
async def test_explicit_discard_preserves_unfenced_uncertain_evidence(
    isolated_bridge: _FakeManager,
) -> None:
    session = _session()
    attempt_path = managed_chat._attempt_path(session)
    attempt_path.parent.mkdir(parents=True)
    attempt_path.write_text(
        json.dumps(
            {
                "version": 1,
                "attempts": {
                    "uncertain-call": {
                        "payload_sha256": "digest",
                        "process_token": "old-process",
                        "state": "uncertain",
                        "turn_id": "uncertain-call",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    stream_path = managed_chat.AgentStreamStore(session.workspace_id, session.id).path
    stream_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(managed_chat.ManagedChatDeliveryUncertain):
        await discard_session_stream(session.workspace_id, session.id)
    assert attempt_path.exists()
    assert stream_path.exists()


@pytest.mark.asyncio
async def test_explicit_discard_preserves_evidence_when_runtime_stop_fails(
    isolated_bridge: _FakeManager,
) -> None:
    session = _session()
    attempt_path = managed_chat._attempt_path(session)
    attempt_path.parent.mkdir(parents=True)
    attempt_path.write_text(json.dumps({"version": 1, "attempts": {}}), encoding="utf-8")

    class FailingTailer:
        async def stop(self, *, raise_errors: bool = False) -> None:
            assert raise_errors is True
            raise RuntimeError("provider stop failed")

    manager = TailerManager(session_getter=lambda session_id: None)
    manager._tailers[session.id] = FailingTailer()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="provider stop failed"):
        await discard_session_stream(session.workspace_id, session.id)
    assert attempt_path.exists()
    assert manager.get_tailer(session.id) is not None
    manager._tailers.clear()


@pytest.mark.asyncio
async def test_explicit_discard_accepts_semantically_acked_uncertain_call(
    isolated_bridge: _FakeManager,
) -> None:
    session = _session()
    attempt_path = managed_chat._attempt_path(session)
    attempt_path.parent.mkdir(parents=True)
    attempt_path.write_text(
        json.dumps(
            {
                "version": 1,
                "attempts": {
                    "acked-call": {
                        "payload_sha256": "digest",
                        "process_token": "old-process",
                        "state": "uncertain",
                        "turn_id": "acked-call",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    await discard_session_stream(
        session.workspace_id,
        session.id,
        acknowledged_call_ids={"acked-call"},
    )
    assert not attempt_path.exists()


def test_maso_uncertainty_survives_even_with_local_runtime_fence(
    isolated_bridge: _FakeManager,
) -> None:
    session = _session()
    path = managed_chat._maso_delivery_path(session)
    path.parent.mkdir(parents=True)
    path.write_text('{"uncertain":true}', encoding="utf-8")

    with pytest.raises(managed_chat.ManagedChatDeliveryUncertain):
        managed_chat.validate_delivery_state_discard(
            session.workspace_id,
            session.id,
            acknowledged_call_ids=set(),
            provider_fenced=True,
        )
    assert path.exists()


@pytest.mark.asyncio
async def test_managed_stream_api_routes_send_through_workspace_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    calls: list[tuple[Any, ...]] = []

    async def send_chat_session_message(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(agent_stream_api, "_session_or_404", lambda session_id: session)
    monkeypatch.setattr(
        agent_stream_api.workspace_manager,
        "send_chat_session_message",
        send_chat_session_message,
    )
    encoded = __import__("base64").b64encode(_PNG).decode()
    payload = agent_stream_api.AgentStreamSendRequest(
        text="inspect",
        client_turn_id="api-call",
        attachments=[
            agent_stream_api.AgentStreamAttachment(
                filename="image.png",
                mime_type="image/png",
                data_url=f"data:image/png;base64,{encoded}",
            )
        ],
    )

    assert await agent_stream_api.send_stream_input(session.id, payload, _user()) == {"ok": True}
    args, kwargs = calls[0]
    assert args == (session.id, "inspect")
    assert kwargs["call_id"] == "api-call"
    assert isinstance(kwargs["attachments"][0], WorkspaceAttachmentCreate)


@pytest.mark.asyncio
async def test_managed_stream_api_cancel_uses_workspace_stop_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    calls: list[tuple[str, str | None]] = []

    async def cancel_chat_session(session_id: str, *, expected_turn_id: str | None = None) -> bool:
        calls.append((session_id, expected_turn_id))
        return True

    monkeypatch.setattr(agent_stream_api, "_session_or_404", lambda session_id: session)
    monkeypatch.setattr(
        agent_stream_api.workspace_manager,
        "cancel_chat_session",
        cancel_chat_session,
    )

    result = await agent_stream_api.cancel_stream_turn(
        session.id,
        agent_stream_api.StreamCancelRequest(expected_turn_id="turn-9"),
        _user(),
    )
    assert result == {"ok": True, "cancelled": True}
    assert calls == [(session.id, "turn-9")]
