"""Registered HTML files are exact, revocable grants from canonical local Task owners."""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient, Response

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    StreamAttachmentPreview,
    TerminalTab,
    User,
    Workspace,
    WorkspaceCreate,
    WorkspaceSessionRole,
)
from claude_hub.models.schemas import (
    TaskExecutionControl,
    TaskExecutionReference,
    TaskSourceReference,
)
from claude_hub.services.agent_stream.registered_html import RegisteredHtmlGrant
from claude_hub.services.workspace_manager import WorkspaceManager
from tests.test_agent_stream_markdown import _assert_unavailable
from tests.test_task_execution_control import (  # noqa: F401
    KEY,
    handoff,
    manager,
    progress,
    register,
)

TAB_ID = "registered-html-source"
HTML = "<!doctype html><html><style>body { color: navy; }</style><h1>Report</h1></html>"


@dataclass
class HtmlScope:
    manager: WorkspaceManager
    workspace: Workspace
    task_id: str
    path: Path
    tabs: dict[str, TerminalTab]


@pytest.fixture
async def html_scope(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> HtmlScope:
    from claude_hub.api import agent_stream as api
    from claude_hub.auth import dependencies

    cwd = tmp_path / "workspace"
    cwd.mkdir()
    artifacts = tmp_path / "registered-artifacts"
    artifacts.mkdir()
    path = artifacts / "report.html"
    path.write_text(HTML, encoding="utf-8")
    workspace = manager.create_workspace(WorkspaceCreate(name="HTML owner", path=str(cwd)))
    tabs = {
        TAB_ID: TerminalTab(
            id=TAB_ID,
            name="HTML source",
            cwd=str(cwd),
            port=0,
            created_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            is_active=True,
            agent_type=AgentType.MASO,
            session_kind=SessionKind.CHAT,
            target=ExecutionTarget.LOCAL,
        )
    }
    monkeypatch.setattr(api, "workspace_manager", manager)
    monkeypatch.setattr(api.ttyd_manager, "get_tab", tabs.get)
    monkeypatch.setattr(dependencies, "is_local_network_request", lambda _request: True)
    task = await register(
        manager,
        workspace,
        source=TaskSourceReference(kind="chat", tab_id=TAB_ID),
        execution_ref=TaskExecutionReference(session_id=f"terminal-tab-{TAB_ID}", run_epoch=1),
    )
    await manager.record_task_progress(
        workspace.id, task.id, progress(task, artifact_refs=[str(path)]), KEY
    )
    return HtmlScope(manager, workspace, task.id, path, tabs)


async def _get_html(
    client: AsyncClient, path: str, *, scoped: bool = False, tab_id: str = TAB_ID
) -> Response:
    endpoint = (
        f"/api/workspaces/tabs/{tab_id}/stream/attachment"
        if scoped
        else "/api/workspaces/artifacts/html-preview"
    )
    return await client.get(endpoint, params={"path": path})


async def _publish(scope: HtmlScope, paths: list[str]) -> None:
    task = scope.manager.tasks[scope.task_id]
    await scope.manager.record_task_progress(
        scope.workspace.id,
        task.id,
        progress(task, call_id=f"publish-{task.progress_revision}", artifact_refs=paths),
        KEY,
    )


@pytest.mark.parametrize("scoped", [False, True])
async def test_registered_html_returns_bounded_json_not_active_html(
    client: AsyncClient, html_scope: HtmlScope, scoped: bool
) -> None:
    response = await _get_html(client, str(html_scope.path), scoped=scoped)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json() == {
        "path": str(html_scope.path),
        "filename": "report.html",
        "content": HTML,
        "size_bytes": len(HTML.encode()),
        "truncated": False,
        "kind": "html",
        "language": "xml",
    }


async def test_registered_report_sized_like_user_report_is_not_truncated(
    client: AsyncClient, html_scope: HtmlScope
) -> None:
    data = HTML.encode() + b" " * (513642 - len(HTML.encode()))
    html_scope.path.write_bytes(data)
    response = await _get_html(client, str(html_scope.path))
    assert response.status_code == 200
    assert response.json()["content"] == data.decode()
    assert response.json()["size_bytes"] == 513642
    assert not response.json()["truncated"]
    html_scope.path.write_bytes(b"x" * (512 * 1024 - 1) + "中".encode())
    response = await _get_html(client, str(html_scope.path))
    assert response.json()["truncated"]
    assert response.json()["content"] == "x" * (512 * 1024 - 1)


@pytest.mark.parametrize("data", [b"\xef\xbb\xbfhello", b"", b"\xff", b"text\x00binary"])
async def test_registered_html_keeps_utf8_and_empty_file_policy(
    client: AsyncClient, html_scope: HtmlScope, data: bytes
) -> None:
    html_scope.path.write_bytes(data)
    response = await _get_html(client, str(html_scope.path))
    if data in {b"\xff", b"text\x00binary"}:
        assert response.status_code == 415
        assert response.headers["cache-control"] == "no-store"
    else:
        assert response.status_code == 200
        assert response.json()["content"] == data.decode("utf-8-sig")


async def test_global_grant_does_not_expand_unrelated_chat_scope(
    client: AsyncClient, html_scope: HtmlScope
) -> None:
    other = html_scope.tabs[TAB_ID].model_copy(update={"id": "other-chat"})
    html_scope.tabs[other.id] = other
    _assert_unavailable(await _get_html(client, str(html_scope.path), scoped=True, tab_id=other.id))
    assert (await _get_html(client, str(html_scope.path))).status_code == 200


@pytest.mark.parametrize(
    "variant",
    [
        "sibling",
        "directory",
        "traversal",
        "relative",
        "url",
        "double-slash",
        "code",
        "encoded",
        "control",
    ],
)
async def test_registered_grant_is_an_exact_html_path(
    client: AsyncClient, html_scope: HtmlScope, variant: str
) -> None:
    sibling = html_scope.path.with_name("private.html")
    sibling.write_text("private", encoding="utf-8")
    variants = {
        "sibling": str(sibling),
        "directory": str(html_scope.path.parent),
        "traversal": str(
            html_scope.path.parent / ".." / html_scope.path.parent.name / html_scope.path.name
        ),
        "relative": html_scope.path.name,
        "url": html_scope.path.as_uri(),
        "double-slash": "/" + str(html_scope.path),
        "code": str(html_scope.path.with_suffix(".py")),
        "encoded": str(html_scope.path).replace("/", "%2F"),
        "control": str(html_scope.path) + "\x00.html",
    }
    _assert_unavailable(await _get_html(client, variants[variant]))
    await _publish(html_scope, [str(html_scope.path.parent)])
    _assert_unavailable(await _get_html(client, str(html_scope.path)))


@pytest.mark.parametrize("scoped", [False, True])
async def test_removed_refs_and_new_epochs_revoke_without_history_recovery(
    client: AsyncClient, html_scope: HtmlScope, scoped: bool
) -> None:
    await _publish(html_scope, [])
    _assert_unavailable(await _get_html(client, str(html_scope.path), scoped=scoped))
    await _publish(html_scope, [str(html_scope.path)])
    task = html_scope.manager.tasks[html_scope.task_id]
    released = await html_scope.manager.record_task_progress(
        html_scope.workspace.id, task.id, progress(task, "released", "release-html"), KEY
    )
    assert (await _get_html(client, str(html_scope.path), scoped=scoped)).status_code == 200
    await html_scope.manager.handoff_task_execution(
        html_scope.workspace.id, task.id, handoff(released.task)
    )
    _assert_unavailable(await _get_html(client, str(html_scope.path), scoped=scoped))


async def test_released_html_grant_survives_manager_reload(
    client: AsyncClient, html_scope: HtmlScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.api import agent_stream as api

    task = html_scope.manager.tasks[html_scope.task_id]
    await html_scope.manager.record_task_progress(
        html_scope.workspace.id, task.id, progress(task, "released", "release-html"), KEY
    )
    fresh = WorkspaceManager()
    monkeypatch.setattr(api, "workspace_manager", fresh)
    response = await _get_html(client, str(html_scope.path))
    assert response.status_code == 200
    assert response.json()["content"] == HTML


def _change_owner(scope: HtmlScope, change: str) -> None:
    task = scope.manager.tasks[scope.task_id]
    now = datetime.now(timezone.utc) + timedelta(seconds=2)
    if change == "task-deleted":
        scope.manager.tasks.pop(task.id)
    elif change == "task-replaced":
        scope.manager.tasks[task.id] = task.model_copy(update={"created_at": now})
    elif change == "workspace-deleted":
        scope.manager.workspaces.pop(scope.workspace.id)
    elif change == "workspace-replaced":
        scope.manager.workspaces[scope.workspace.id] = scope.workspace.model_copy(
            update={"created_at": now}
        )
    elif change == "workspace-remote":
        scope.manager.workspaces[scope.workspace.id] = scope.workspace.model_copy(
            update={"target": ExecutionTarget.REMOTE}
        )
    elif change == "source-deleted":
        scope.tabs.pop(TAB_ID)
    elif change == "source-replaced":
        scope.tabs[TAB_ID] = scope.tabs[TAB_ID].model_copy(update={"created_at": now})
    elif change == "source-remote":
        scope.tabs[TAB_ID] = scope.tabs[TAB_ID].model_copy(
            update={"target": ExecutionTarget.REMOTE}
        )
    elif change == "source-terminal":
        scope.tabs[TAB_ID] = scope.tabs[TAB_ID].model_copy(
            update={"session_kind": SessionKind.TERMINAL}
        )
    elif change == "source-path":
        scope.tabs[TAB_ID] = scope.tabs[TAB_ID].model_copy(update={"cwd": "/different-workspace"})
    elif change == "refs-removed":
        assert task.latest_progress is not None
        task.latest_progress.artifact_refs = []
    elif change == "epoch":
        scope.manager.tasks[task.id] = task.model_copy(
            update={"execution_epoch": task.execution_epoch + 1}
        )
    elif change == "revision":
        scope.manager.tasks[task.id] = task.model_copy(
            update={"progress_revision": task.progress_revision + 1}
        )
    elif change == "execution-ref":
        scope.manager.tasks[task.id] = task.model_copy(
            update={"execution_ref": TaskExecutionReference(session_id="other-owner")}
        )
    elif change == "control":
        scope.manager.tasks[task.id] = task.model_copy(
            update={"execution_control": TaskExecutionControl.WORKSPACE}
        )
    else:
        raise AssertionError(change)


@pytest.mark.parametrize(
    "change",
    [
        "workspace-deleted",
        "workspace-replaced",
        "workspace-remote",
        "source-deleted",
        "source-replaced",
        "source-remote",
        "source-terminal",
        "refs-removed",
        "epoch",
        "execution-ref",
        "control",
    ],
)
async def test_invalid_registered_owner_fails_before_file_read(
    client: AsyncClient, html_scope: HtmlScope, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from claude_hub.api.routes.agent_stream import media

    _change_owner(html_scope, change)

    def unexpected_read(_grant: RegisteredHtmlGrant) -> StreamAttachmentPreview:
        pytest.fail("unverified owner reached file I/O")

    monkeypatch.setattr(media, "read_registered_html_preview", unexpected_read)
    _assert_unavailable(await _get_html(client, str(html_scope.path)))


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize(
    "change",
    [
        "task-deleted",
        "task-replaced",
        "workspace-deleted",
        "workspace-replaced",
        "workspace-remote",
        "source-deleted",
        "source-replaced",
        "source-remote",
        "source-path",
        "refs-removed",
        "epoch",
        "revision",
        "execution-ref",
        "control",
    ],
)
async def test_registered_html_revalidates_after_threaded_read(
    client: AsyncClient,
    html_scope: HtmlScope,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    scoped: bool,
) -> None:
    from claude_hub.api.routes.agent_stream import media

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_read = media.read_registered_html_preview

    def delayed_read(grant: RegisteredHtmlGrant) -> StreamAttachmentPreview:
        try:
            result = original_read(grant)
            entered.set()
            assert release.wait(5)
            return result
        finally:
            finished.set()

    monkeypatch.setattr(media, "read_registered_html_preview", delayed_read)
    pending = asyncio.create_task(_get_html(client, str(html_scope.path), scoped=scoped))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        _change_owner(html_scope, change)
        release.set()
        response = await pending
        _assert_unavailable(response)
        assert HTML not in response.text
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("kind", ["symlink", "directory-symlink", "directory", "fifo"])
async def test_registered_file_open_rejects_symlinks_and_non_regular_files(
    client: AsyncClient, html_scope: HtmlScope, kind: str
) -> None:
    path = html_scope.path
    if kind == "directory-symlink":
        original = path.parent.with_name("original-artifacts")
        path.parent.rename(original)
        path.parent.symlink_to(original, target_is_directory=True)
    else:
        path.unlink()
        if kind == "symlink":
            target = path.with_name("private.html")
            target.write_text("private", encoding="utf-8")
            path.symlink_to(target)
        elif kind == "directory":
            path.mkdir()
        else:
            os.mkfifo(path)
    descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK) if kind == "fifo" else None
    try:
        _assert_unavailable(await asyncio.wait_for(_get_html(client, str(path)), timeout=2))
    finally:
        if descriptor is not None:
            os.close(descriptor)


async def test_registered_html_authentication_precedes_grant_and_file_read(
    client: AsyncClient, html_scope: HtmlScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.api.routes.agent_stream import media
    from claude_hub.auth import dependencies

    cookie_name = dependencies.settings.session_cookie_name
    monkeypatch.setattr(dependencies, "is_local_network_request", lambda _request: False)
    monkeypatch.setattr(
        dependencies,
        "settings",
        SimpleNamespace(auth_enabled=True, allowed_open_ids_list=[], allowed_emails_list=[]),
    )
    user = User(open_id="html-reader", name="HTML Reader", email="reader@example.test")
    monkeypatch.setattr(
        dependencies,
        "get_session",
        lambda token: SimpleNamespace(user=user) if token == "valid" else None,
    )
    reads: list[str] = []
    original_read = media.read_registered_html_preview

    def observed_read(grant: RegisteredHtmlGrant) -> StreamAttachmentPreview:
        reads.append(grant.path)
        return original_read(grant)

    monkeypatch.setattr(media, "read_registered_html_preview", observed_read)
    assert (await _get_html(client, str(html_scope.path))).status_code == 401
    assert not reads
    client.cookies.set(cookie_name, "invalid")
    assert (await _get_html(client, str(html_scope.path))).status_code == 401
    assert not reads
    client.cookies.set(cookie_name, "valid")
    assert (await _get_html(client, str(html_scope.path))).status_code == 200
    assert reads == [str(html_scope.path)]


def _bind_managed_source(html_scope: HtmlScope) -> ManagedSession:
    tab = html_scope.tabs[TAB_ID]
    assert tab.cwd is not None
    session = ManagedSession(
        id="managed-html-source",
        workspace_id=html_scope.workspace.id,
        tab_id=TAB_ID,
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        title="HTML source",
        workspace_path=tab.cwd,
        tmux_session="",
        target=ExecutionTarget.LOCAL,
        created_at=tab.created_at,
        updated_at=tab.created_at,
    )
    html_scope.manager.sessions[session.id] = session
    html_scope.tabs[TAB_ID] = tab.model_copy(
        update={"managed_session_id": session.id, "workspace_role": WorkspaceSessionRole.WORKER}
    )
    task = html_scope.manager.tasks[html_scope.task_id]
    html_scope.manager.tasks[task.id] = task.model_copy(
        update={"execution_ref": TaskExecutionReference(session_id=session.id)}
    )
    return session


@pytest.mark.parametrize(
    "change", ["missing", "remote", "replaced", "wrong-tab", "wrong-workspace"]
)
async def test_managed_source_requires_its_canonical_session(
    client: AsyncClient, html_scope: HtmlScope, change: str
) -> None:
    session = _bind_managed_source(html_scope)
    endpoint = f"/api/workspaces/sessions/{session.id}/stream/attachment"
    assert (await client.get(endpoint, params={"path": str(html_scope.path)})).status_code == 200
    assert (await _get_html(client, str(html_scope.path))).status_code == 200
    if change == "missing":
        html_scope.manager.sessions.pop(session.id)
    else:
        updates: dict[str, dict[str, Any]] = {
            "remote": {"target": ExecutionTarget.REMOTE},
            "replaced": {"created_at": datetime.now(timezone.utc) + timedelta(seconds=1)},
            "wrong-tab": {"tab_id": "another-tab"},
            "wrong-workspace": {"workspace_id": "another-workspace"},
        }
        html_scope.manager.sessions[session.id] = session.model_copy(update=updates[change])
    _assert_unavailable(await _get_html(client, str(html_scope.path)))


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("during_read", [False, True])
async def test_archived_or_stopped_sources_keep_published_artifacts(
    client: AsyncClient,
    html_scope: HtmlScope,
    monkeypatch: pytest.MonkeyPatch,
    managed: bool,
    scoped: bool,
    during_read: bool,
) -> None:
    from claude_hub.api.routes.agent_stream import media

    session = _bind_managed_source(html_scope) if managed else None
    if scoped:
        owner = f"sessions/{session.id}" if session is not None else f"tabs/{TAB_ID}"
        endpoint = f"/api/workspaces/{owner}/stream/attachment"
    else:
        endpoint = "/api/workspaces/artifacts/html-preview"

    def stop_source() -> None:
        if session is not None:
            html_scope.manager.sessions[session.id] = session.model_copy(
                update={
                    "status": ManagedSessionStatus.STOPPED,
                    "runtime_status": AgentRuntimeStatus.OFFLINE,
                }
            )
        else:
            html_scope.tabs[TAB_ID] = html_scope.tabs[TAB_ID].model_copy(
                update={"archived": True, "is_active": False}
            )

    if not during_read:
        stop_source()
        response = await client.get(endpoint, params={"path": str(html_scope.path)})
        assert response.status_code == 200
        assert response.json()["content"] == HTML
        return

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_read = media.read_registered_html_preview

    def delayed_read(grant: RegisteredHtmlGrant) -> StreamAttachmentPreview:
        try:
            result = original_read(grant)
            entered.set()
            assert release.wait(5)
            return result
        finally:
            finished.set()

    monkeypatch.setattr(media, "read_registered_html_preview", delayed_read)
    pending = asyncio.create_task(client.get(endpoint, params={"path": str(html_scope.path)}))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        stop_source()
        release.set()
        response = await pending
        assert response.status_code == 200
        assert response.json()["content"] == HTML
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_reference_revoked_during_invalid_utf8_read_returns_opaque_denial(
    client: AsyncClient, html_scope: HtmlScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.api.routes.agent_stream import media

    html_scope.path.write_bytes(b"\xffprivate")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_read = media.read_registered_html_preview

    def delayed_read(grant: RegisteredHtmlGrant) -> StreamAttachmentPreview:
        try:
            entered.set()
            assert release.wait(5)
            return original_read(grant)
        finally:
            finished.set()

    monkeypatch.setattr(media, "read_registered_html_preview", delayed_read)
    pending = asyncio.create_task(_get_html(client, str(html_scope.path)))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await _publish(html_scope, [])
        release.set()
        _assert_unavailable(await pending)
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_registered_open_rejects_ancestor_symlink_replacement(
    client: AsyncClient, html_scope: HtmlScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.services.agent_stream import local_markdown

    original_read = local_markdown._read_beneath
    original_dir = html_scope.path.parent
    preserved = original_dir.with_name("preserved-artifacts")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def delayed_open(root: Path, relative: Path) -> tuple[bytes, int]:
        try:
            entered.set()
            assert release.wait(5)
            return original_read(root, relative)
        finally:
            finished.set()

    monkeypatch.setattr(local_markdown, "_read_beneath", delayed_open)
    pending = asyncio.create_task(_get_html(client, str(html_scope.path)))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        original_dir.rename(preserved)
        original_dir.symlink_to(preserved, target_is_directory=True)
        release.set()
        _assert_unavailable(await pending)
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("path", ["", "x" * 4097], ids=["empty", "over-limit"])
async def test_registered_html_query_length_is_validated(client: AsyncClient, path: str) -> None:
    assert (await _get_html(client, path)).status_code == 422
