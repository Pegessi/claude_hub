"""Real local lifecycle owners with isolated storage and a mocked remote MASO."""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from claude_hub.auth.dependencies import get_current_user
from claude_hub.models import (
    AgentType,
    SessionKind,
    User,
    Workspace,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskStatus,
)
from claude_hub.services import maso_session_archive as archive
from claude_hub.services.agent_stream import tailer
from claude_hub.services.agent_stream.maso_http import MasoHttpSession
from claude_hub.services.agent_stream.tailer import TailerManager
from claude_hub.services.dot import DotService, DotStore, reset_dot_service
from claude_hub.services.ttyd_manager import TTYDManager, TTYDProcess
from claude_hub.services.workspace_manager import WorkspaceManager
from tests.test_maso_http import MasoServer, session, transport

ttyd_module = importlib.import_module("claude_hub.services.ttyd_manager")
wm_module = importlib.import_module("claude_hub.services.workspace_manager")
BASE = "http://maso.test"


@pytest.fixture
async def remote(monkeypatch: pytest.MonkeyPatch):
    coordinator = archive.MasoSessionArchives()
    monkeypatch.setattr(archive, "maso_session_archives", coordinator)
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(
        archive,
        "AsyncClient",
        lambda **kwargs: httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(handle)),
    )
    yield SimpleNamespace(coordinator=coordinator, calls=calls)
    await coordinator.drain()


@pytest.fixture
def owners(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote):
    monkeypatch.setattr(ttyd_module, "STATE_FILE", tmp_path / "tabs.json")
    monkeypatch.setattr(ttyd_module, "ORDER_FILE", tmp_path / "order.json")
    monkeypatch.setattr(TTYDProcess, "stop", AsyncMock())
    monkeypatch.setattr(TTYDManager, "ensure_tab_running", AsyncMock())
    monkeypatch.setattr(tailer, "_DELETION_FENCED_SESSION_IDS", set())
    monkeypatch.setattr(tailer, "_PROVIDER_FENCED_SESSION_IDS", set())
    tabs = TTYDManager()
    for mod in (wm_module, wm_module._state, wm_module._persistence):
        monkeypatch.setattr(mod, "INDEX_FILE", tmp_path / "workspaces" / "index.json")
    monkeypatch.setattr(wm_module, "STATE_ROOT", tmp_path / "workspaces")
    monkeypatch.setattr(wm_module, "LEGACY_STATE_FILE", tmp_path / "absent.json")
    monkeypatch.setattr(wm_module._state, "LEGACY_STATE_FILE", tmp_path / "absent.json")
    wm = WorkspaceManager()
    for module_name in (
        "sessions.lifecycle",
        "monitoring.reports",
        "scheduling.registry",
        "_constants",
    ):
        mod = importlib.import_module("claude_hub.services.workspace_manager." + module_name)
        monkeypatch.setattr(mod, "ttyd_manager", tabs)
    now = datetime.now()
    wm.workspaces["w"] = Workspace(
        id="w",
        name="archive tests",
        path=str(tmp_path),
        session_prefix="archive",
        created_at=now,
        updated_at=now,
        default_branch="main",
    )
    return tabs, wm


async def add_tab(owners, remote, *, tab_id="tab", sid="remote", **kwargs):
    tabs, _wm = owners
    process = TTYDProcess(
        tab_id=tab_id,
        port=0,
        name="Archive test",
        agent_type=AgentType.MASO,
        session_kind=SessionKind.CHAT,
        env={"MASO_BASE_URL": BASE},
        agent_session_id=sid,
        agent_session_id_verified=True,
        **kwargs,
    )
    tabs.processes[tab_id] = process
    tabs._save_state()
    await remote.coordinator.drain()
    remote.calls.clear()
    return process


async def add_managed(owners, remote, *, sid="managed-remote", **kwargs):
    tabs, wm = owners
    record = session(
        id="managed",
        tab_id="managed-tab",
        workspace_id="w",
        env={"MASO_BASE_URL": BASE},
        agent_session_id=sid,
        agent_session_id_verified=True,
        **kwargs,
    )
    wm.sessions[record.id] = record
    await add_tab(
        owners,
        remote,
        tab_id=record.tab_id,
        sid="stale-backing-id",
        managed_session_id=record.id,
    )
    wm._save_state()
    await remote.coordinator.drain()
    remote.calls.clear()
    return record


def payloads(remote):
    return [(str(request.url), json.loads(request.content)) for request in remote.calls]


@pytest.mark.asyncio
async def test_tab_api_archive_restore_delete(owners, remote, monkeypatch):
    from claude_hub.api import tabs as api_tabs

    tabs, _wm = owners
    process = await add_tab(owners, remote)
    monkeypatch.setattr(api_tabs, "ttyd_manager", tabs)
    app = FastAPI()
    app.include_router(api_tabs.router)
    app.dependency_overrides[get_current_user] = lambda: User(
        open_id="local", name="Local", email="local@localhost", avatar_url=None
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://hub.test"
    ) as client:
        for method, path, expected in (
            ("POST", "/api/tabs/tab/archive", True),
            ("POST", "/api/tabs/tab/unarchive", False),
            ("DELETE", "/api/tabs/tab", True),
        ):
            response = await client.request(method, path)
            assert response.status_code in {200, 204}, response.text
            await remote.coordinator.drain()
            assert payloads(remote)[-1] == (
                BASE + "/api/sessions/remote/archive",
                {"archived": expected},
            )
    assert process.tab_id not in tabs.processes
    assert all(request.method == "POST" for request in remote.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["timeout", "deadline", "404", "503", "connection"])
async def test_remote_failure_never_blocks_local_retirement(
    owners, remote, monkeypatch, caplog, fault
):
    tabs, _wm = owners
    await add_tab(owners, remote)
    entered, release = asyncio.Event(), asyncio.Event()

    async def handle(request):
        entered.set()
        await release.wait()
        if fault == "deadline":
            await asyncio.Future()
        if fault == "timeout":
            raise httpx.ReadTimeout("private response")
        if fault == "connection":
            raise httpx.ConnectError("private response")
        return httpx.Response(int(fault))

    monkeypatch.setattr(archive, "_REQUEST_TIMEOUT", 0.1)
    monkeypatch.setattr(
        archive,
        "AsyncClient",
        lambda **kwargs: httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(handle)),
    )
    assert (await tabs.archive_tab("tab")).archived
    await entered.wait()
    assert await tabs.delete_tab("tab")  # Remote response still blocked.
    assert tabs.get_tab("tab") is None
    release.set()
    await remote.coordinator.drain()
    assert "MASO archive sync failed" in caplog.text
    assert "private response" not in caplog.text


@pytest.mark.asyncio
async def test_shared_direct_and_managed_identity_and_endpoint_guard(owners, remote):
    tabs, wm = owners
    await add_tab(owners, remote, sid="shared")
    managed = await add_managed(owners, remote, sid="shared")
    await tabs.archive_tab("tab")
    await remote.coordinator.drain()
    assert not any(body["archived"] for _, body in payloads(remote))
    await wm.archive_session("w", managed.id)
    await remote.coordinator.drain()
    assert payloads(remote)[-1] == (BASE + "/api/sessions/shared/archive", {"archived": True})
    assert all("stale-backing-id" not in url for url, _ in payloads(remote))


@pytest.mark.asyncio
async def test_inflight_archive_then_restore_is_serialized(owners, remote, monkeypatch):
    tabs, _wm = owners
    await add_tab(owners, remote)
    entered, release = asyncio.Event(), asyncio.Event()
    values = []

    async def handle(request):
        value = json.loads(request.content)["archived"]
        values.append(value)
        if len(values) == 1:
            entered.set()
            await release.wait()
        return httpx.Response(200)

    monkeypatch.setattr(
        archive,
        "AsyncClient",
        lambda **kwargs: httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(handle)),
    )
    await tabs.archive_tab("tab")
    await entered.wait()
    await tabs.unarchive_tab("tab")
    assert values == [True]
    release.set()
    await remote.coordinator.drain()
    assert values == [True, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["unverified", "missing-endpoint", "non-maso", "terminal"])
async def test_unconfirmed_or_non_http_identity_never_contacts_remote(owners, remote, invalid):
    tabs, _wm = owners
    process = await add_tab(owners, remote)
    if invalid == "unverified":
        process.agent_session_id_verified = False
    elif invalid == "missing-endpoint":
        process.env = {}
    elif invalid == "non-maso":
        process.agent_type = AgentType.CODEX
    else:
        process.session_kind = SessionKind.TERMINAL
    # Start from the actual persisted input, not a formerly confirmed owner.
    tabs._maso_archive_source = archive.MasoArchiveSource(tabs._maso_archive_references)
    await tabs.delete_tab("tab")
    await remote.coordinator.drain()
    assert remote.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["archive", "delete", "workspace"])
async def test_managed_lifecycle_uses_managed_identity(owners, remote, monkeypatch, action):
    from claude_hub.services import managed_chat

    tabs, wm = owners
    record = await add_managed(owners, remote, ephemeral=True, caller_owned_ephemeral=True)
    monkeypatch.setattr(managed_chat, "start", AsyncMock())
    if action == "archive":
        await wm.archive_session("w", record.id)
        await remote.coordinator.drain()
        assert payloads(remote)[-1][1] == {"archived": True}
        await wm.unarchive_session("w", record.id)
        await remote.coordinator.drain()
        assert payloads(remote)[-1][1] == {"archived": False}
    elif action == "delete":
        await wm.delete_session(record.id)
    else:
        await wm.delete_workspace("w")
    await remote.coordinator.drain()
    if action != "archive":
        assert payloads(remote)[-1][1] == {"archived": True}
        assert record.id not in wm.sessions
    assert all(url == BASE + "/api/sessions/managed-remote/archive" for url, _ in payloads(remote))


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [False, True])
async def test_cross_reviewer_cleanup_captures_identity_before_owner_is_removed(
    owners, remote, deferred
):
    _tabs, wm = owners
    record = await add_managed(owners, remote, ephemeral=True, role=WorkspaceSessionRole.REVIEWER)
    now = datetime.now()
    task = WorkspaceTask(
        id="task",
        workspace_id="w",
        title="Review",
        prompt="Review",
        created_at=now,
        updated_at=now,
        status=WorkspaceTaskStatus.DONE,
        review_session_id=record.id,
        agent_type=AgentType.MASO,
    )
    wm.tasks[task.id] = task
    record.task_id = task.id
    wm._save_state()
    await remote.coordinator.drain()
    remote.calls.clear()
    tab_ids = await wm._cleanup_reviewer_for_terminal_task(
        task, updated_at=now, delete_tabs=not deferred
    )
    assert record.id not in wm.sessions
    wm._save_state()
    for tab_id in tab_ids:
        await _tabs.delete_tab(tab_id)
    await remote.coordinator.drain()
    assert payloads(remote) == [(BASE + "/api/sessions/managed-remote/archive", {"archived": True})]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_scouting_finally_archives_remote(owners, remote, monkeypatch, fail):
    from claude_hub.services.dot import scouting

    _tabs, wm = owners
    record = await add_managed(owners, remote, ephemeral=True, caller_owned_ephemeral=True)
    monkeypatch.setattr(wm_module, "workspace_manager", wm)
    monkeypatch.setattr(scouting, "resolve_maso_base_url", AsyncMock(return_value=BASE))
    monkeypatch.setattr(scouting, "_resolve_scout_workspace", lambda: "w")
    monkeypatch.setattr(wm, "ensure_workspace_agent", AsyncMock(return_value=record))
    run = (
        AsyncMock(side_effect=RuntimeError("turn failed")) if fail else AsyncMock(return_value="ok")
    )
    monkeypatch.setattr(scouting, "_run_ephemeral_turn", run)
    if fail:
        with pytest.raises(RuntimeError, match="turn failed"):
            await scouting._ephemeral_model_turn("query", "model")
    else:
        assert await scouting._ephemeral_model_turn("query", "model") == "ok"
    await remote.coordinator.drain()
    assert payloads(remote) == [(BASE + "/api/sessions/managed-remote/archive", {"archived": True})]


@pytest.mark.asyncio
async def test_rotation_archives_only_confirmed_retired_session(remote):
    server = MasoServer()
    provider = transport(server, session(agent_session_id="old", agent_session_id_verified=True))
    try:
        candidate = await provider.rotate_conversation()
        assert remote.calls == []
        provider.adopt_rotated_conversation(candidate)
        assert not provider._delivery_path.exists()
        await remote.coordinator.drain()
        assert payloads(remote) == [
            ("http://127.0.0.1:9796/api/sessions/old/archive", {"archived": True})
        ]
        assert provider.active_thread_id == candidate
    finally:
        await provider.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["identity", "seed", "journal"])
async def test_failed_adoption_does_not_archive_until_cold_recovery(remote, monkeypatch, boundary):
    from claude_hub.services.agent_stream import maso_http

    provider = transport(
        MasoServer(), session(agent_session_id="old", agent_session_id_verified=True)
    )
    original_unlink = Path.unlink

    def fail(*args, **kwargs):
        raise OSError("injected commit failure")

    def unlink(path, *args, **kwargs):
        if path == provider._delivery_path:
            fail()
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as faults:
        if boundary == "identity":
            faults.setattr(provider, "_conversation_id_persist", fail)
        elif boundary == "seed":
            faults.setattr(provider, "_rotated_conversation_seed", lambda: "prior history")
            faults.setattr(maso_http, "write_seed_sidecar", fail)
        else:
            faults.setattr(Path, "unlink", unlink)
        with pytest.raises(OSError, match="injected"):
            provider._commit_rotated_conversation("new")
    await remote.coordinator.drain()
    assert remote.calls == []
    assert provider._delivery_path.exists()
    cold = MasoHttpSession(provider.session)
    await remote.coordinator.drain()
    assert payloads(remote) == [
        ("http://127.0.0.1:9796/api/sessions/old/archive", {"archived": True})
    ]
    assert cold.active_thread_id == "new"
    await provider.stop()
    await cold.stop()


@pytest.mark.asyncio
async def test_real_managed_persistence_callback_is_not_skipped_or_swallowed(
    owners, remote, monkeypatch
):
    _tabs, wm = owners
    record = await add_managed(owners, remote)
    manager = TailerManager(wm.sessions.get, persist_session_id=wm.set_session_agent_session_id)
    # The getter returns the exact same mutable object as the persistence owner.
    manager._persist_session_id(record.id, "new")
    assert wm.sessions[record.id].agent_session_id == "new"
    from claude_hub.services.local_storage import read_document

    saved = read_document(wm._workspace_state_file("w"))
    assert saved["sessions"][0]["agent_session_id"] == "new"
    monkeypatch.setattr(wm, "_save_state", lambda: (_ for _ in ()).throw(OSError("disk failed")))
    with pytest.raises(OSError, match="disk failed"):
        manager._persist_session_id(record.id, "uncommitted")
    assert wm.sessions[record.id].agent_session_id == "new"


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["rotate", "tidy"])
async def test_dot_rotation_retires_old_not_new_session(remote, tmp_path, trigger):
    store = DotStore(tmp_path / "dot")
    store.ensure()
    record = session(agent_session_id="old-dot", agent_session_id_verified=True)
    store.update_state(tab_id=record.tab_id, session_conversation_id="old-dot")
    dot = DotService(store)
    reset_dot_service(dot)
    provider = transport(MasoServer(), record)
    try:
        if trigger == "rotate":
            dot.request_rotation()
        else:
            dot.prepare_turn("[Dot tidy] go", "old-dot")
        await provider._prepare_turn_context("continue")
        await remote.coordinator.drain()
        assert provider.active_thread_id != "old-dot"
        assert payloads(remote) == [
            ("http://127.0.0.1:9796/api/sessions/old-dot/archive", {"archived": True})
        ]
    finally:
        await provider.stop()
        reset_dot_service(None)


@pytest.mark.asyncio
async def test_strict_review_subagent_close_archives_remote(owners, remote, tmp_path):
    from claude_hub.models.subagent import Subagent
    from claude_hub.services.subagents import SubagentService

    tabs, _wm = owners
    process = await add_tab(owners, remote, subagent_id="review-child")
    runtime: Any = SimpleNamespace(get_tab=tabs.get_tab, archive_tab=tabs.archive_tab)
    service = SubagentService(tmp_path / "subagents.json", runtime)
    record = Subagent(
        id="review-child",
        tab_id=process.tab_id,
        parent_tab_id="parent",
        agent_type=AgentType.MASO,
        cwd=str(tmp_path),
        name="strict review",
        created_at=datetime.now(),
    )
    assert await service._close(record, "strict review finished")
    await remote.coordinator.drain()
    assert record.closed
    assert payloads(remote) == [(BASE + "/api/sessions/remote/archive", {"archived": True})]


@pytest.mark.asyncio
async def test_equal_remote_id_on_different_endpoint_is_not_shared(owners, remote):
    tabs, wm = owners
    await add_tab(owners, remote, sid="same")
    record = await add_managed(owners, remote, sid="same")
    record.env = {"MASO_BASE_URL": "http://other-maso.test"}
    wm._save_state()
    await remote.coordinator.drain()
    remote.calls.clear()
    await tabs.archive_tab("tab")
    await remote.coordinator.drain()
    assert payloads(remote) == [(BASE + "/api/sessions/same/archive", {"archived": True})]


@pytest.mark.asyncio
@pytest.mark.parametrize("verified,new", [(False, "new"), (True, "old")])
async def test_rotation_never_archives_constructive_or_unchanged_identity(remote, verified, new):
    provider = transport(
        MasoServer(), session(agent_session_id="old", agent_session_id_verified=verified)
    )
    try:
        provider._commit_rotated_conversation(new, seed_prior_history=False)
        await remote.coordinator.drain()
        assert remote.calls == []
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_retired_rotation_with_other_live_reference_is_not_archived(owners, remote):
    await add_tab(owners, remote, sid="old")
    provider = transport(
        MasoServer(),
        session(
            agent_session_id="old", agent_session_id_verified=True, env={"MASO_BASE_URL": BASE}
        ),
    )
    try:
        provider._commit_rotated_conversation("new", seed_prior_history=False)
        await remote.coordinator.drain()
        assert not any(body["archived"] for _, body in payloads(remote))
    finally:
        await provider.stop()


@pytest.mark.asyncio
async def test_save_failure_does_not_publish_archive(owners, remote, monkeypatch):
    tabs, wm = owners
    record = await add_managed(owners, remote)
    monkeypatch.setattr(
        wm, "_atomic_save_state_text", lambda *args: (_ for _ in ()).throw(OSError("disk failed"))
    )
    with pytest.raises(OSError, match="disk failed"):
        await wm.archive_session("w", record.id)
    await remote.coordinator.drain()
    assert not wm.sessions[record.id].archived
    assert remote.calls == []


@pytest.mark.asyncio
async def test_direct_persistence_callback_does_not_swallow_failure(owners, remote, monkeypatch):
    tabs, _wm = owners
    process = await add_tab(owners, remote)
    monkeypatch.setattr(
        ttyd_module,
        "_write_tabs_snapshot",
        lambda *_: (_ for _ in ()).throw(OSError("disk failed")),
    )
    with pytest.raises(OSError, match="disk failed"):
        tabs.set_tab_agent_session_id(process.tab_id, "not-committed")
    await remote.coordinator.drain()
    assert process.agent_session_id == "remote"
    assert remote.calls == []


@pytest.mark.asyncio
async def test_dot_provider_switch_archives_previous_tab(owners, remote, monkeypatch, tmp_path):
    from claude_hub.api import agent_stream as stream_api
    from claude_hub.services.dot import execution

    tabs, wm = owners
    old = await add_tab(owners, remote, sid="old-dot")
    dot = DotService(DotStore(tmp_path / "dot-switch"))
    dot.store.ensure()
    dot.store.update_state(tab_id=old.tab_id)
    monkeypatch.setattr(execution, "ttyd_manager", tabs)
    monkeypatch.setattr(execution, "workspace_manager", wm)
    monkeypatch.setattr(execution, "_check_switchable", lambda *_: None)
    monkeypatch.setattr(
        stream_api,
        "_get_tab_tailer_manager",
        lambda: SimpleNamespace(get_tailer=lambda *_: None, forget_session=AsyncMock()),
    )
    monkeypatch.setattr(dot, "sync_journal", AsyncMock())

    async def create(**kwargs):
        new = await add_tab(owners, remote, tab_id="new-dot-tab", sid="new-dot")
        return new.to_schema()

    monkeypatch.setattr(tabs, "create_tab", create)
    before = execution.execution_view(dot)
    result = await execution.switch_execution(
        dot,
        expected_revision=before["revision"],
        agent_type=AgentType.MASO,
        env={"MASO_BASE_URL": BASE, "MASO_MODEL": "different"},
    )
    await remote.coordinator.drain()
    assert result["tab_id"] == "new-dot-tab"
    assert tabs.get_tab(old.tab_id).archived
    assert not tabs.get_tab("new-dot-tab").archived
    assert payloads(remote) == [(BASE + "/api/sessions/old-dot/archive", {"archived": True})]


@pytest.mark.asyncio
async def test_inflight_restore_cannot_infer_uncommitted_retirement(owners, remote, monkeypatch):
    tabs, _wm = owners
    await add_tab(owners, remote, archived=True)
    entered, release = asyncio.Event(), asyncio.Event()
    stopping, stopped = asyncio.Event(), asyncio.Event()
    values = []

    async def handle(request):
        values.append(json.loads(request.content)["archived"])
        if len(values) == 1:
            entered.set()
            await release.wait()
        return httpx.Response(200)

    async def stop(*args, **kwargs):
        stopping.set()
        await stopped.wait()

    monkeypatch.setattr(
        archive,
        "AsyncClient",
        lambda **kwargs: httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(handle)),
    )
    await tabs.unarchive_tab("tab")
    await entered.wait()
    monkeypatch.setattr(TTYDProcess, "stop", stop)
    retiring = asyncio.create_task(tabs.archive_tab("tab"))
    try:
        await stopping.wait()
        release.set()
        await remote.coordinator.drain()
        assert values == [False], "an in-memory archive flag is not a retirement commit"
    finally:
        stopped.set()
        await retiring
    await remote.coordinator.drain()
    assert values == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["inflight-restore", "shared-retirement"])
async def test_pending_adoption_protects_old_session_from_other_archive_jobs(
    owners, remote, monkeypatch, trigger
):
    tabs, _wm = owners
    process = await add_tab(owners, remote, sid="old", archived=trigger == "inflight-restore")
    if trigger == "shared-retirement":
        await add_tab(owners, remote, tab_id="shared", sid="old")
    entered, release = asyncio.Event(), asyncio.Event()
    values = []

    async def handle(request):
        body = json.loads(request.content)
        values.append((request.url.path, body["archived"]))
        if trigger == "inflight-restore" and request.url.path.endswith("/old/archive"):
            entered.set()
            await release.wait()
        return httpx.Response(200)

    monkeypatch.setattr(
        archive,
        "AsyncClient",
        lambda **kwargs: httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(handle)),
    )
    if trigger == "inflight-restore":
        await tabs.unarchive_tab("tab")
        await entered.wait()
    record = session(
        id="terminal-tab-tab",
        tab_id="tab",
        workspace_id="terminal-tabs",
        agent_session_id="old",
        agent_session_id_verified=True,
        env={"MASO_BASE_URL": BASE},
    )
    provider = MasoHttpSession(
        record,
        conversation_id_persist=lambda cid: tabs.set_tab_agent_session_id(process.tab_id, cid),
    )
    from claude_hub.services.agent_stream import maso_http

    with monkeypatch.context() as faults:
        faults.setattr(provider, "_rotated_conversation_seed", lambda: "prior")
        faults.setattr(
            maso_http,
            "write_seed_sidecar",
            lambda *_: (_ for _ in ()).throw(OSError("seed failed")),
        )
        with pytest.raises(OSError, match="seed failed"):
            provider._commit_rotated_conversation("new")
    if trigger == "shared-retirement":
        await tabs.archive_tab("shared")
    release.set()
    await remote.coordinator.drain()
    assert ("/api/sessions/old/archive", True) not in values
    provider._finish_conversation_adoption()
    await remote.coordinator.drain()
    assert values[-1] == ("/api/sessions/old/archive", True)
    await provider.stop()


@pytest.mark.asyncio
async def test_first_allocation_is_recoverable_and_cannot_send_without_persistence(
    owners, remote, monkeypatch
):
    tabs, _wm = owners
    process = await add_tab(owners, remote)
    process.agent_session_id = "local-placeholder"
    process.agent_session_id_verified = False
    tabs._save_state()
    await remote.coordinator.drain()
    remote.calls.clear()
    record = session(
        id="terminal-tab-tab",
        tab_id="tab",
        workspace_id="terminal-tabs",
        agent_session_id="local-placeholder",
        agent_session_id_verified=False,
        env={"MASO_BASE_URL": BASE},
    )
    server = MasoServer()
    provider = MasoHttpSession(
        record, conversation_id_persist=lambda cid: tabs.set_tab_agent_session_id("tab", cid)
    )
    provider._client = httpx.AsyncClient(
        base_url=BASE, transport=httpx.MockTransport(server.handle)
    )
    with monkeypatch.context() as faults:
        faults.setattr(
            ttyd_module,
            "_write_tabs_snapshot",
            lambda *_: (_ for _ in ()).throw(OSError("disk failed")),
        )
        with pytest.raises(OSError, match="disk failed"):
            await provider.send_message("must not be accepted", [])
    assert not any(path.endswith("/messages") for _, path, _ in server.calls)
    assert provider._delivery_path.exists()
    await remote.coordinator.drain()
    assert remote.calls == []
    # Retry the journal, not POST /sessions: the allocated B remains owned.
    provider._finish_conversation_adoption()
    assert process.agent_session_id == "opaque-sid"
    assert server.session_creations == 1
    await tabs.archive_tab("tab")
    await remote.coordinator.drain()
    assert payloads(remote)[-1] == (BASE + "/api/sessions/opaque-sid/archive", {"archived": True})
    assert all("local-placeholder" not in url for url, _ in payloads(remote))
    await provider.stop()


@pytest.mark.asyncio
async def test_confirmed_allocation_is_archived_if_journal_cannot_be_written(remote, monkeypatch):
    provider = transport(MasoServer(), session())
    mkdir = Path.mkdir

    def fail_journal_directory(path, *args, **kwargs):
        if path == provider._delivery_path.parent:
            raise OSError("journal unavailable")
        return mkdir(path, *args, **kwargs)

    with monkeypatch.context() as faults:
        faults.setattr(Path, "mkdir", fail_journal_directory)
        with pytest.raises(OSError, match="journal unavailable"):
            await provider._create_session()
    await remote.coordinator.drain()
    assert payloads(remote) == [
        ("http://127.0.0.1:9796/api/sessions/opaque-sid/archive", {"archived": True})
    ]
    await provider.stop()


@pytest.mark.asyncio
async def test_cold_owner_snapshot_honors_unfinished_rotation_journal(owners, remote):
    from claude_hub.services.agent_stream.store import AgentStreamStore

    tabs, _wm = owners
    process = await add_tab(owners, remote, sid="adopted")
    await add_tab(owners, remote, tab_id="shared", sid="retired")
    path = AgentStreamStore("terminal-tabs", "terminal-tab-tab").path.with_suffix(
        ".maso-delivery.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "base_url": BASE,
                "session_id": "retired",
                "session_id_verified": True,
                "adopted_session_id": "adopted",
            }
        )
    )
    # A cold source has never seen the old ID; the durable journal still guards it.
    tabs._maso_archive_source = archive.MasoArchiveSource(tabs._maso_archive_references)
    await tabs.archive_tab("shared")
    await remote.coordinator.drain()
    assert not any(body["archived"] for _, body in payloads(remote))
    assert process.agent_session_id == "adopted"


@pytest.mark.asyncio
async def test_remote_restore_failure_keeps_local_restoration(owners, remote, monkeypatch):
    tabs, _wm = owners
    await add_tab(owners, remote, archived=True)
    monkeypatch.setattr(
        archive,
        "AsyncClient",
        lambda **kwargs: httpx.AsyncClient(
            **kwargs, transport=httpx.MockTransport(lambda _request: httpx.Response(503))
        ),
    )
    assert not (await tabs.unarchive_tab("tab")).archived
    await remote.coordinator.drain()
    assert not tabs.get_tab("tab").archived


@pytest.mark.asyncio
async def test_backend_lifetime_cancels_and_joins_owned_archive_jobs(owners, remote, monkeypatch):
    tabs, _wm = owners
    await add_tab(owners, remote)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def handle(_request):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(
        archive,
        "AsyncClient",
        lambda **kwargs: httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(handle)),
    )
    async with remote.coordinator.lifetime():
        await tabs.archive_tab("tab")
        await entered.wait()
    assert cancelled.is_set()
    assert remote.coordinator._jobs == {}
    assert remote.coordinator._pending == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["tab", "managed"])
@pytest.mark.parametrize("action", ["archive", "delete"])
@pytest.mark.parametrize("placeholder", ["local-placeholder", None], ids=["constructive", "empty"])
async def test_failed_first_adoption_can_be_retired_without_retrying_send(
    owners, remote, monkeypatch, owner, action, placeholder
):
    tabs, wm = owners
    if owner == "tab":
        local = await add_tab(owners, remote, sid=placeholder)
        local.agent_session_id_verified = False
        tabs._save_state()
        record = session(
            id="terminal-tab-tab",
            workspace_id="terminal-tabs",
            tab_id="tab",
            env={"MASO_BASE_URL": BASE},
            agent_session_id=placeholder,
            agent_session_id_verified=False,
        )
        persist = lambda cid: tabs.set_tab_agent_session_id("tab", cid)
        fault_owner, fault_name = ttyd_module, "_write_tabs_snapshot"
    else:
        local = await add_managed(owners, remote, sid=placeholder)
        local.agent_session_id_verified = False
        wm._save_state()
        record = local
        persist = lambda cid: wm.set_session_agent_session_id(local.id, cid)
        fault_owner, fault_name = wm, "_atomic_save_state_text"
    await remote.coordinator.drain()
    remote.calls.clear()
    server = MasoServer()
    provider = MasoHttpSession(record, conversation_id_persist=persist)
    provider._client = httpx.AsyncClient(
        base_url=BASE, transport=httpx.MockTransport(server.handle)
    )
    with monkeypatch.context() as faults:
        faults.setattr(
            fault_owner, fault_name, lambda *_: (_ for _ in ()).throw(OSError("disk failed"))
        )
        with pytest.raises(OSError, match="disk failed"):
            await provider._create_session()
    assert provider._delivery_path.exists()
    assert not local.agent_session_id_verified
    manager = TailerManager(lambda _id: record)
    monkeypatch.setattr(tailer, "create_native_session", lambda *args, **kwargs: provider)
    await manager._get_or_create(record)
    if owner == "tab":
        if action == "archive":
            await tabs.archive_tab("tab")
        else:
            await tabs.delete_tab("tab")
    elif action == "archive":
        await wm.archive_session("w", local.id)
    else:
        await wm.delete_session(local.id)
    await remote.coordinator.drain()
    assert payloads(remote) == [(BASE + "/api/sessions/opaque-sid/archive", {"archived": True})]
