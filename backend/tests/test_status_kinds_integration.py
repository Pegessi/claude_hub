"""Lifecycle -> native snapshots -> tab/board APIs -> CLI, without a live Hub."""

import asyncio
import importlib
import json
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from claude_hub.auth.dependencies import get_current_user
from claude_hub.cli.main import cli
from claude_hub.main import app
from claude_hub.models import (
    AgentStreamEventType,
    AgentType,
    SessionKind,
    User,
    Workspace,
    WorkspaceSessionRole,
)
from claude_hub.services import managed_chat
from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter
from claude_hub.services.agent_stream.codex_jsonl import CodexJsonlAdapter
from claude_hub.services.agent_stream.tailer import SessionTailer, TailerManager
from claude_hub.services.ttyd_manager import TTYDManager, TTYDProcess
from tests.test_agent_stream import (
    _as_provider_session,
    _FakeNativeTransport,
    _isolate_state_root,
    _native_session,
)


def bind_workspace(tmp_path, monkeypatch, session):
    wm = importlib.import_module("claude_hub.services.workspace_manager").workspace_manager
    now = datetime.now(timezone.utc)
    workspace = Workspace(
        id=session.workspace_id,
        name="Status isolation",
        path=str(tmp_path),
        default_branch="main",
        session_prefix="status-test",
        created_at=now,
        updated_at=now,
    )
    for name, value in {
        "workspaces": {workspace.id: workspace},
        "sessions": {session.id: session},
        "tasks": {},
        "reports": {},
    }.items():
        monkeypatch.setattr(wm, name, value)
    for name in [
        "_save_state",
        "_reconcile_task_report_statuses",
        "_reconcile_workspace_session_pointers",
        "_cleanup_stale_orchestrator_assignments",
        "_sync_workspace_tab_metadata",
    ]:
        monkeypatch.setattr(wm, name, lambda *args: None)
    monkeypatch.setattr(wm, "_prune_orphan_workspace_tabs", AsyncMock())
    monkeypatch.setattr(wm, "markdown_documents_for_workspace", lambda *args: [])
    monkeypatch.setitem(
        app.dependency_overrides,
        get_current_user,
        lambda: User(open_id="status-test", name="Status", email="status@localhost"),
    )

    return workspace


def bind_tab_manager(monkeypatch, manager):
    monkeypatch.setattr(importlib.import_module("claude_hub.api.tabs"), "ttyd_manager", manager)
    monkeypatch.setattr(
        importlib.import_module("claude_hub.services.workspace_manager.monitoring.refresh"),
        "ttyd_manager",
        manager,
    )
    # list_tab_agent_statuses' inventory is isolated too; no tmux subprocess.
    monkeypatch.setattr(
        importlib.import_module("claude_hub.services.ttyd_manager"),
        "_tmux_list_sessions",
        AsyncMock(return_value={p.tmux_session for p in manager.processes.values()}),
    )


async def assert_surfaces(client, monkeypatch, session, expected):
    tabs_response = await client.get("/api/tabs/status")
    board_response = await client.get(f"/api/workspaces/{session.workspace_id}/board")
    assert tabs_response.status_code == board_response.status_code == 200
    tabs = tabs_response.json()
    board = board_response.json()
    row = next(s for s in board["sessions"] if s["id"] == session.id)
    tab = next(t for t in tabs if t["tab_id"] == session.tab_id)
    assert tab["status"] == row["runtime_status"] == row["status"] == expected
    # Exercise the real CLI formatters using actual API responses.
    cli_module = importlib.import_module("claude_hub.cli.main")
    api = SimpleNamespace(get_board=lambda _: board, list_tab_statuses=lambda: tabs)
    monkeypatch.setattr(cli_module, "get_client", lambda _: nullcontext(api))
    output = CliRunner().invoke(cli, ["--json", "agent", "status", session.workspace_id])
    assert output.exit_code == 0, output.output
    assert json.loads(output.output)[0]["runtime_status"] == expected
    output = CliRunner().invoke(cli, ["--json", "tab", "status"])
    assert output.exit_code == 0, output.output
    assert json.loads(output.output)[0]["status"] == expected
    output = CliRunner().invoke(
        cli, ["--json", "session", "status", session.id, "--workspace-id", session.workspace_id]
    )
    assert output.exit_code == 0, output.output
    assert json.loads(output.output)["runtime_status"] == expected
    return tab


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", [AgentType.CLAUDE, AgentType.CODEX])
@pytest.mark.parametrize("role", [WorkspaceSessionRole.ORCHESTRATOR, WorkspaceSessionRole.REVIEWER])
async def test_native_chat_lifecycle_through_status_apis_and_cli(
    tmp_path, monkeypatch, role, agent_type
):
    _isolate_state_root(monkeypatch, tmp_path)
    session = _native_session(agent_type).model_copy(
        update={
            # Global weak tailer owners may survive GC until the next case.
            # Distinct provider cases must never share a runtime identity.
            "id": f"native-status-{role.value}-{agent_type.value}",
            "tab_id": f"native-tab-{role.value}-{agent_type.value}",
            "role": role,
            "session_kind": SessionKind.CHAT,
        }
    )
    workspace = bind_workspace(tmp_path, monkeypatch, session)

    transport = _FakeNativeTransport(eof_is_fatal=agent_type == AgentType.CODEX)
    transport.active_thread_id = "parent-thread"
    transport.session = session
    tailer = SessionTailer(
        workspace_id=workspace.id,
        session_id=session.id,
        adapter=ClaudeJsonlAdapter() if agent_type == AgentType.CLAUDE else CodexJsonlAdapter(),
        session_getter=lambda: session,
        native_transport=_as_provider_session(transport),
    )
    owner = TailerManager(lambda _: session)
    owner._tailers[session.id] = tailer
    monkeypatch.setattr(managed_chat, "_manager", lambda: owner)
    queue = await tailer.subscribe()

    manager = TTYDManager.__new__(TTYDManager)
    manager._status_cache = {}
    manager._status_snapshots = {}
    process = TTYDProcess(
        tab_id=session.tab_id,
        port=12345,
        name="Native status",
        agent_type=agent_type,
        session_kind=SessionKind.CHAT,
    )
    manager.processes = {process.tab_id: process}
    manager._tab_order = [process.tab_id]
    # Neither native Chat nor native reviewer may inspect stale terminal markers.
    monkeypatch.setattr(
        process, "capture_history", AsyncMock(side_effect=AssertionError("native pane read"))
    )
    monkeypatch.setattr(
        manager, "_classify_agent_status", lambda *args: pytest.fail("native marker parsing")
    )
    bind_tab_manager(monkeypatch, manager)

    async def event(expected):
        while True:
            result = await asyncio.wait_for(queue.get(), timeout=1)
            if result.type == expected:
                return result

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://status-test"
    ) as client:

        async def assert_status(expected):
            return await assert_surfaces(client, monkeypatch, session, expected)

        try:
            await assert_status("idle")
            await tailer.send_message("status lifecycle", [], client_turn_id="status-turn")
            await event(AgentStreamEventType.TURN_STARTED)
            await assert_status("working")
            marker = "• Working (21s • esc to…)\n? for shortcuts"
            if agent_type == AgentType.CLAUDE:
                records = [
                    {
                        "type": "stream_event",
                        "event": {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": marker},
                        },
                    },
                    {
                        "type": "assistant",
                        "message": {
                            "id": "tool-msg",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "status-tool",
                                    "name": "Bash",
                                    "input": {"command": "echo test"},
                                }
                            ],
                        },
                    },
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "status-tool",
                                    "content": marker,
                                }
                            ]
                        },
                    },
                    {"type": "result", "subtype": "success"},
                ]
            else:

                def rpc(method, **params):
                    return {"method": method, "params": {"threadId": "parent-thread", **params}}

                item = {"id": "status-tool", "type": "commandExecution", "command": "echo test"}
                records = [
                    rpc("item/agentMessage/delta", delta=marker),
                    rpc("item/started", item=item),
                    rpc("item/completed", item={**item, "aggregatedOutput": marker, "exitCode": 0}),
                    rpc("turn/completed", turn={"id": "status-turn", "status": "completed"}),
                ]
            for record, expected_event, expected_status in zip(
                records,
                [
                    AgentStreamEventType.TEXT_DELTA,
                    AgentStreamEventType.TOOL_CALL_STARTED,
                    AgentStreamEventType.TOOL_CALL_COMPLETED,
                    AgentStreamEventType.TURN_COMPLETED,
                ],
                ["working", "working", "working", "idle"],
            ):
                transport._records.put_nowait(record)
                await event(expected_event)
                await assert_status(expected_status)
            assert not transport.turn_in_flight
            await assert_status("idle")  # Stale Working text cannot make native Chat busy.
            if agent_type == AgentType.CLAUDE:
                transport.exit_error = "late EOF after completed turn"
                transport._records.put_nowait(None)
                await asyncio.sleep(0.02)
                await assert_status("idle")
        finally:
            await tailer.stop()


REVIEW_FIXTURES = Path(__file__).parent / "fixtures" / "codex-status-20261020" / "reviewers"
REVIEW_CAPTURES = json.loads((REVIEW_FIXTURES / "manifest.json").read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", REVIEW_CAPTURES, ids=lambda row: row["file"])
async def test_real_terminal_reviewer_screens_through_status_apis_and_cli(
    tmp_path, monkeypatch, capture
):
    _isolate_state_root(monkeypatch, tmp_path)
    provider = AgentType(capture["agent_type"])
    assert capture["role"] == "reviewer" and capture["session_kind"] == "terminal"
    session = _native_session(provider).model_copy(
        update={
            "id": capture["session_id"],
            "role": WorkspaceSessionRole.REVIEWER,
            "session_kind": SessionKind.TERMINAL,
        }
    )
    bind_workspace(tmp_path, monkeypatch, session)
    process = TTYDProcess(
        tab_id=session.tab_id,
        port=12345,
        name="Reviewer replay",
        agent_type=provider,
        session_kind=SessionKind.TERMINAL,
    )
    raw = (REVIEW_FIXTURES / capture["file"]).read_bytes().decode()
    monkeypatch.setattr(process, "capture_history", AsyncMock(return_value=raw))
    monkeypatch.setattr(
        process, "capture_foreground_command", AsyncMock(return_value=provider.value)
    )
    manager = TTYDManager.__new__(TTYDManager)
    manager.processes = {process.tab_id: process}
    manager._tab_order = [process.tab_id]
    manager._status_cache = {}
    manager._status_snapshots = {}
    bind_tab_manager(monkeypatch, manager)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://reviewer-replay"
    ) as client:
        await assert_surfaces(
            client, monkeypatch, session, "working" if capture["observed"] == "busy" else "idle"
        )
