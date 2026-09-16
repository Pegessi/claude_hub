"""TraeX (``traex``) agent integration.

The transport shares Codex JSON-RPC framing with explicit TraeX turn, model
and permission contracts. Tests cover native lifecycle and tool notifications,
terminal launch, and the Chat-only structured/discovery boundary.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentStreamEventType,
    AgentType,
    ChatMode,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream.base import NormalizeContext
from claude_hub.services.agent_stream.codex_jsonl import (
    CodexJsonlAdapter,
    TraexJsonlAdapter,
)
from claude_hub.services.agent_stream.native import (
    CodexNativeSession,
    TraexNativeSession,
    create_native_session,
    native_provider_binary,
)
from claude_hub.services.agent_stream.registry import (
    get_adapter,
    get_adapter_for_session,
    supports_structured,
)
from claude_hub.services.ttyd_manager import TTYDManager, TTYDProcess, get_agent_command
from tests.test_agent_stream_native import _FakeProcess, _written_requests

# ``from claude_hub.services import ttyd_manager`` resolves to the manager
# singleton exported by the package __init__, not the submodule; import the
# module explicitly so monkeypatching hits the real globals.
ttyd_manager_module = importlib.import_module("claude_hub.services.ttyd_manager")


def _managed_session(agent_type: AgentType = AgentType.TRAEX) -> ManagedSession:
    return ManagedSession(
        id="sess-traex",
        workspace_id="ws-1",
        tab_id="tab-traex",
        role=WorkspaceSessionRole.WORKER,
        agent_type=agent_type,
        status=ManagedSessionStatus.IDLE,
        title="traex",
        workspace_path="/tmp",
        tmux_session="tmux-traex",
        target=ExecutionTarget.LOCAL,
        solo_mode=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


# ── binary mapping ──────────────────────────────────────────────────────────


def test_get_agent_command_traex() -> None:
    assert get_agent_command(AgentType.TRAEX) == "traex"


def test_native_provider_binary_traex() -> None:
    assert native_provider_binary(AgentType.TRAEX) == "traex"


# ── native Chat transport ───────────────────────────────────────────────────


def test_create_native_session_traex_is_codex_subclass() -> None:
    transport = create_native_session(_managed_session())
    assert isinstance(transport, TraexNativeSession)
    # It deliberately reuses the whole Codex JSON-RPC engine.
    assert isinstance(transport, CodexNativeSession)


def test_traex_native_build_command_has_no_stdio_flag() -> None:
    transport = TraexNativeSession(_managed_session())
    # traex's default listener is stdio:// and it rejects codex's --stdio flag.
    assert transport._build_command() == ["traex", "app-server"]
    assert transport.adapter_id == "traex-native"
    # A persistent app-server: EOF must remain fatal.
    assert transport.eof_is_fatal is True


def test_registry_traex_uses_traex_adapter_that_reuses_codex_normalization() -> None:
    assert supports_structured(AgentType.TRAEX) is True
    adapter_cls = get_adapter(AgentType.TRAEX)
    assert adapter_cls is TraexJsonlAdapter
    # Live notification normalization is inherited unchanged from Codex.
    assert issubclass(TraexJsonlAdapter, CodexJsonlAdapter)

    adapter = get_adapter_for_session(_managed_session())
    assert isinstance(adapter, TraexJsonlAdapter)


def test_traex_adapter_disables_transcript_discovery() -> None:
    # Terminal tabs must not be matched to a Codex rollout under ~/.codex, and
    # edit-resend must fail closed with "transcript not located".
    adapter = TraexJsonlAdapter()
    assert adapter.supports_transcript_discovery is False
    assert adapter.discover_source(_managed_session()) is None


# ── terminal TUI launch ─────────────────────────────────────────────────────


def _traex_process(
    solo_mode: bool = False, session_kind: SessionKind = SessionKind.TERMINAL
) -> TTYDProcess:
    return TTYDProcess(
        tab_id="tab-traex-launch",
        port=13099,
        name="TraeX",
        agent_type=AgentType.TRAEX,
        solo_mode=solo_mode,
        session_kind=session_kind,
    )


def test_traex_launch_command_fresh_without_resume() -> None:
    assert _traex_process()._traex_launch_command() == "traex"


def test_traex_launch_command_solo_adds_codex_style_bypass_flags() -> None:
    assert _traex_process(solo_mode=True)._traex_launch_command() == (
        "traex --ask-for-approval never --sandbox danger-full-access"
    )


def test_traex_ttyd_command_wraps_shell_non_solo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    cmd = _traex_process()._build_ttyd_command(session_exists=False)

    assert cmd[-3:-1] == ["/bin/zsh", "-c"]
    assert "traex; exec /bin/zsh" in cmd[-1]
    assert "--ask-for-approval" not in cmd[-1]


def test_traex_ttyd_command_solo_keeps_flags_in_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    cmd = _traex_process(solo_mode=True)._build_ttyd_command(session_exists=False)

    assert cmd[-3:-1] == ["/bin/zsh", "-c"]
    assert "traex --ask-for-approval never --sandbox danger-full-access; exec /bin/zsh" in cmd[-1]


def test_traex_existing_session_reattaches_without_relaunch_or_flags() -> None:
    # tmux new-session -A attaches the live session; the trailing token is
    # ignored and must carry neither a resume nor solo bypass flags.
    last = _traex_process(solo_mode=True)._build_ttyd_command(session_exists=True)[-1]
    assert last.endswith("traex")
    assert "resume" not in last
    assert "danger-full-access" not in last
    assert "exec /bin" not in last


# ── Chat kind: tmux must own only an inert shell, never launch traex ─────────


def test_traex_chat_process_shell_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    chat = _traex_process(session_kind=SessionKind.CHAT)
    assert chat.shell == "/bin/zsh"
    assert chat.shell != "traex"


def test_traex_chat_ttyd_command_never_launches_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    for session_exists in (False, True):
        cmd = _traex_process(session_kind=SessionKind.CHAT)._build_ttyd_command(
            session_exists=session_exists
        )
        assert "traex app-server" not in cmd[-1]
        assert cmd[-1].rstrip().endswith("/bin/zsh") or "CLAUDE_HUB_TAB_ID" in cmd[-1]


async def test_traex_chat_ensure_tmux_does_not_launch_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    captured: dict[str, list[str]] = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"", b"")

    async def _fake_exec(*args: object, **_kwargs: object) -> _FakeProc:
        captured["cmd"] = [str(a) for a in args]
        return _FakeProc()

    async def _session_absent(_session: str) -> bool:
        return False

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", _session_absent)
    monkeypatch.setattr(ttyd_manager_module, "_ensure_tmux_server", lambda: None)
    monkeypatch.setattr(ttyd_manager_module.asyncio, "create_subprocess_exec", _fake_exec)

    await _traex_process(session_kind=SessionKind.CHAT).ensure_tmux_session()

    assert "cmd" in captured
    assert "traex app-server" not in captured["cmd"][-1]
    assert "/bin/zsh" in captured["cmd"][-1]


async def test_traex_terminal_ensure_tmux_launches_tui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    captured: dict[str, list[str]] = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"", b"")

    async def _fake_exec(*args: object, **_kwargs: object) -> _FakeProc:
        captured["cmd"] = [str(a) for a in args]
        return _FakeProc()

    async def _session_absent(_session: str) -> bool:
        return False

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", _session_absent)
    monkeypatch.setattr(ttyd_manager_module, "_ensure_tmux_server", lambda: None)
    monkeypatch.setattr(ttyd_manager_module.asyncio, "create_subprocess_exec", _fake_exec)

    await _traex_process(solo_mode=True).ensure_tmux_session()

    assert "traex --ask-for-approval never --sandbox danger-full-access" in captured["cmd"][-1]


# ── model/env switching: Chat yes, terminal no ──────────────────────────────


async def test_traex_chat_switch_env_updates_without_respawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    chat = _traex_process(session_kind=SessionKind.CHAT)
    await chat.switch_env({"CODEX_MODEL": "Seed-Code"})
    assert chat.env["CODEX_MODEL"] == "Seed-Code"


async def test_traex_terminal_switch_env_rejected() -> None:
    # Terminal-side tmux respawn is not wired for TraeX (fresh-only TUI).
    terminal = _traex_process(session_kind=SessionKind.TERMINAL)
    with pytest.raises(ValueError):
        await terminal.switch_env({"CODEX_MODEL": "Seed-Code"})


# ── working-status classifier shares the codex-family marker set ────────────


def test_traex_working_indicator_classifies_as_working(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists", lambda _session: True)
    manager = TTYDManager.__new__(TTYDManager)
    manager._status_snapshots = {}
    process = _traex_process()

    frame = "\n".join(
        [
            "  → some prior assistant line",
            "",
            " ⠀⠞ Working  4.03k tokens",
            "",
            "",
            "› Add a follow-up",
            "",
            "  Queued follow-up inputs (3):",
            "    1. continue",
            "    2. continue",
            "    3. continue",
            "",
            "  Seed-Evolving · ~/Downloads",
        ]
    )

    status, status_text, detail, _ = manager._classify_agent_status(
        process, frame, "hash-traex-working", "traex"
    )

    assert status == AgentRuntimeStatus.WORKING
    assert status_text == "Working"
    assert detail == "agent is processing"


def _ctx() -> NormalizeContext:
    return NormalizeContext(
        session_id="sess-traex", tab_id="tab-traex", agent_type=AgentType.TRAEX, run_epoch=1
    )


@pytest.mark.parametrize(
    ("solo", "mode", "approval", "sandbox"),
    [
        (True, ChatMode.DEFAULT, "never", "danger-full-access"),
        (False, ChatMode.DEFAULT, "on-request", "workspace-write"),
        (True, ChatMode.PLAN, "on-request", "read-only"),
    ],
)
@pytest.mark.parametrize("resume", [False, True])
async def test_traex_thread_initialization_applies_tab_settings(
    solo: bool,
    mode: ChatMode,
    approval: str,
    sandbox: str,
    resume: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _managed_session()
    session.solo_mode = solo
    session.chat_mode = mode
    session.env = {"CODEX_MODEL": "Seed-Code"}
    session.agent_session_id = "existing-thread" if resume else None
    transport = TraexNativeSession(session)
    proc = _FakeProcess(
        [
            json.dumps({"id": 1, "result": {}}).encode() + b"\n",
            json.dumps(
                {"id": 2, "result": {"thread": {"id": "existing-thread"}, "model": "Seed-Code"}}
            ).encode()
            + b"\n",
        ]
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    try:
        await transport.start()
        request = next(
            r
            for r in _written_requests(proc)
            if r.get("method") == ("thread/resume" if resume else "thread/start")
        )
        assert request["params"]["model"] == "Seed-Code"
        assert request["params"]["cwd"] == session.workspace_path
        assert request["params"]["approvalPolicy"] == approval
        assert request["params"]["sandbox"] == sandbox
    finally:
        await transport.stop()


async def test_traex_interrupt_uses_provider_ids_and_retires_old_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    transport = TraexNativeSession(_managed_session())
    transport._started = True
    transport._process = _FakeProcess([])
    transport._thread_id = "thread-real"
    seen = []

    async def request(method, params):
        seen.append((method, params))
        if method == "turn/start":
            return {"turn": {"id": "turn-real"}}
        assert transport.turn_in_flight
        await transport._handle_notification(
            {
                "method": "item/agentMessage/delta",
                "params": {"turnId": "turn-real", "delta": "late"},
            }
        )
        await transport._handle_notification(
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-real", "status": "interrupted"}},
            }
        )
        return {}

    monkeypatch.setattr(transport, "_send_request", request)
    await transport.send_message("first", [])
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    transport._inflight_images = [image]
    await transport._handle_notification(
        {"method": "item/agentMessage/delta", "params": {"turnId": "turn-real", "delta": "queued"}}
    )
    await transport.cancel_active_turn()
    assert seen[-1] == ("turn/interrupt", {"threadId": "thread-real", "turnId": "turn-real"})
    assert transport._notification_queue.empty()
    assert not image.exists()
    assert not transport.turn_in_flight
    assert transport._started
    await transport._handle_notification(
        {
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-real", "status": "interrupted"}},
        }
    )
    await transport._handle_notification(
        {"method": "item/agentMessage/delta", "params": {"turnId": "next-turn", "delta": "new"}}
    )
    assert (await transport.read_line())["params"]["delta"] == "new"


async def test_traex_interrupt_failure_terminates_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = TraexNativeSession(_managed_session())
    proc = _FakeProcess([])
    transport._process = proc
    transport._started = True
    transport._thread_id, transport._provider_turn_id = "thread", "turn"
    transport._begin_turn()
    monkeypatch.setattr(
        transport, "_send_request", AsyncMock(side_effect=RuntimeError("broken RPC"))
    )
    await transport.cancel_active_turn()
    assert proc._terminated
    assert not transport._started
    assert not transport.turn_in_flight


@pytest.mark.parametrize(
    "method", ["item/commandExecution/requestApproval", "item/fileChange/requestApproval"]
)
@pytest.mark.parametrize(
    ("selected", "decision"),
    [("Allow once", "accept"), ("Reject", "decline"), ("anything else", "decline")],
)
async def test_traex_permission_card_roundtrip(method: str, selected: str, decision: str) -> None:
    transport = TraexNativeSession(_managed_session())
    proc = _FakeProcess([])
    transport._process = proc
    await transport._handle_server_request(
        {
            "id": "approval-id",
            "method": method,
            "params": {
                "itemId": "cmd-1",
                "turnId": "turn-1",
                "command": "touch example.txt",
                "reason": "Write access",
            },
        }
    )
    events = TraexJsonlAdapter().normalize_line(await transport.read_line(), _ctx())
    card = next(e for e in events if e.type == AgentStreamEventType.APPROVAL_REQUIRED)
    assert "touch example.txt" in card.payload["questions"][0]["prompt"]
    assert await transport.answer_pending_question(
        [{"questionId": card.payload["questions"][0]["id"], "selected": [selected]}]
    )
    assert _written_requests(proc) == [
        {"jsonrpc": "2.0", "id": "approval-id", "result": {"decision": decision}}
    ]
    assert not transport._pending_permissions


def test_traex_command_timeline_and_terminal_errors() -> None:
    adapter = TraexJsonlAdapter()
    item = {
        "id": "call-1",
        "type": "commandExecution",
        "command": "false",
        "cwd": "/tmp",
        "status": "inProgress",
    }
    started = adapter.normalize_line({"method": "item/started", "params": {"item": item}}, _ctx())
    assert started[0].type == AgentStreamEventType.TOOL_CALL_STARTED
    assert started[0].payload["args"]["cmd"] == "false"
    item.update(status="completed", exitCode=1, aggregatedOutput="command failed")
    completed = adapter.normalize_line(
        {"method": "item/completed", "params": {"item": item}}, _ctx()
    )
    assert completed[0].call_id == started[0].call_id
    assert completed[0].payload["status"] == "failed"
    assert completed[0].payload["result"] == "command failed"
    failed = adapter.normalize_line(
        {
            "method": "turn/completed",
            "params": {"turn": {"status": "failed", "error": {"message": "model unavailable"}}},
        },
        _ctx(),
    )
    assert [e.type for e in failed] == [
        AgentStreamEventType.ERROR,
        AgentStreamEventType.TURN_COMPLETED,
    ]
    assert failed[0].payload["message"] == "model unavailable"
    interrupted = adapter.normalize_line(
        {"method": "turn/completed", "params": {"turn": {"status": "interrupted"}}}, _ctx()
    )
    assert interrupted[0].payload["status"] == "cancelled"


async def test_traex_parallel_approval_answers_do_not_resolve_other_requests() -> None:
    transport = TraexNativeSession(_managed_session())
    proc = _FakeProcess([])
    transport._process = proc
    for req_id in ["first", "second"]:
        await transport._handle_server_request(
            {
                "id": req_id,
                "method": "item/commandExecution/requestApproval",
                "params": {"itemId": req_id, "command": "true"},
            }
        )
    await transport._handle_server_request(
        {
            "id": "question",
            "method": "item/tool/requestUserInput",
            "params": {
                "itemId": "question-item",
                "questions": [{"id": "q", "question": "Choose", "options": [{"label": "Yes"}]}],
            },
        }
    )
    answers = [{"questionId": "permission:first", "selected": ["Allow once"]}]
    results = await asyncio.gather(
        transport.answer_pending_question(answers), transport.answer_pending_question(answers)
    )
    assert results.count(True) == 1
    assert [r["id"] for r in _written_requests(proc)] == ["first"]
    assert "second" in transport._pending_permissions
    assert "question" in transport._pending_questions


async def test_traex_approval_persistence_only_resolves_answered_card() -> None:
    from claude_hub.services.agent_stream.tailer import SessionTailer

    session = _managed_session()
    tailer = SessionTailer("ws-1", session.id, TraexJsonlAdapter(), lambda: session)
    for req_id in ["first", "second"]:
        tailer._record_approval_card(
            _ctx().event(
                AgentStreamEventType.APPROVAL_REQUIRED,
                {
                    "questions": [{"id": f"permission:{req_id}"}],
                },
                call_id=req_id,
            )
        )
    tailer._publish = AsyncMock()
    await tailer._emit_approval_resolved(
        [{"questionId": "permission:first", "selected": ["Allow once"]}]
    )
    assert set(tailer._pending_approvals) == {"second"}
    assert tailer._publish.await_args.args[0].call_id == "first"


async def test_traex_permission_changes_apply_on_next_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _managed_session()
    session.solo_mode = True
    transport = TraexNativeSession(session)
    transport._started = True
    transport._process = _FakeProcess([])
    transport._thread_id = "thread"
    send = AsyncMock(return_value={"turn": {"id": "turn"}})
    monkeypatch.setattr(transport, "_send_request", send)
    await transport.send_message("run", [])
    assert send.await_args.args[1]["approvalPolicy"] == "never"
    assert send.await_args.args[1]["sandboxPolicy"] == {"type": "dangerFullAccess"}
    transport.acknowledge_turn_complete()
    transport._current_mode = "plan"
    transport._mode_discovery_attempted = True
    transport._thread_model = "Seed-Evolving"
    transport._mode_presets = {"plan": {"mode": "plan"}}
    await transport.send_message("plan only", [])
    assert send.await_args.args[1]["approvalPolicy"] == "on-request"
    assert send.await_args.args[1]["sandboxPolicy"] == {"type": "readOnly"}
