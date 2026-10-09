"""Task edits preserve attachment bytes across validation and commit failures."""

from __future__ import annotations

import asyncio
import base64
import json
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pytest import MonkeyPatch

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    ManagedSession,
    ManagedSessionStatus,
    User,
    Workspace,
    WorkspaceAttachmentCreate,
    WorkspaceCreate,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskCreate,
    WorkspaceTaskStatus,
    WorkspaceTaskUpdate,
)
from claude_hub.models.schemas import TaskExecutionControl, TaskExecutionHandoffRequest
from claude_hub.services.workspace_manager import WorkspaceManager
from claude_hub.services.workspace_manager._task_execution import TaskExecutionConflict
from tests.test_task_execution_control import _wm, manager, no_default_http, workspace

_api = import_module("claude_hub.api.workspaces")
_attachments = import_module("claude_hub.services.workspace_manager._attachments")
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGA"
    "WjR9awAAAABJRU5ErkJggg=="
)
USER = User(open_id="attachment-test", name="test", email="test@example.invalid")


def image(filename: str = "new.png") -> WorkspaceAttachmentCreate:
    return WorkspaceAttachmentCreate(
        filename=filename,
        mime_type="image/png",
        data_url="data:image/png;base64," + base64.b64encode(PNG).decode("ascii"),
    )


@pytest.fixture(autouse=True)
def api_manager(manager: WorkspaceManager, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(_api, "workspace_manager", manager)
    monkeypatch.setattr(_wm.ttyd_manager, "rename_tab", Mock(return_value=True))
    monkeypatch.setattr(_wm.ttyd_manager, "delete_tab", AsyncMock())


async def make_task(
    manager: WorkspaceManager,
    workspace: Workspace,
    control: TaskExecutionControl = TaskExecutionControl.WORKSPACE,
) -> WorkspaceTask:
    task, _ = await manager.register_task(
        workspace.id,
        WorkspaceTaskCreate(
            title="Original task",
            prompt="Original description",
            agent_type=AgentType.CLAUDE,
            attachments=[image("original.png")],
            execution_control=control,
            request_key="attachment-task" if control == TaskExecutionControl.INITIATOR else None,
            reporter_key="K" * 48 if control == TaskExecutionControl.INITIATOR else None,
        ),
        actor_key="attachment-tests",
    )
    path = Path(task.attachments[0].path)
    assert path.read_bytes() == PNG
    (path.parent / "unrelated.keep").write_bytes(b"not owned by this edit")
    return task


def files(task: WorkspaceTask) -> dict[str, bytes]:
    directory = Path(task.attachments[0].path).parent
    return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


async def edit(task: WorkspaceTask, **updates: Any) -> WorkspaceTask:
    payload = WorkspaceTaskUpdate(
        **{
            "prompt": "Edited description",
            "removed_attachment_ids": [a.id for a in task.attachments],
            "add_attachments": [image()],
            **updates,
        }
    )
    result = await _api.update_task(task.id, payload, USER)
    assert isinstance(result, WorkspaceTask)
    return result


def persisted(manager: WorkspaceManager, workspace: Workspace) -> dict[str, Any]:
    value = json.loads(manager._workspace_state_file(workspace.id).read_text())
    assert isinstance(value, dict)
    return value


def assert_original(
    manager: WorkspaceManager,
    workspace: Workspace,
    task: WorkspaceTask,
    before_state: bytes,
    before_files: dict[str, bytes],
) -> None:
    assert manager.tasks[task.id].model_dump() == task.model_dump()
    assert manager.tasks[task.id].reporter_key_hash == task.reporter_key_hash
    assert manager._workspace_state_file(workspace.id).read_bytes() == before_state
    assert Path(task.attachments[0].path).read_bytes() == PNG
    assert files(task) == before_files
    assert not manager._task_execution_operations


def assert_committed(
    manager: WorkspaceManager,
    workspace: Workspace,
    task: WorkspaceTask,
    *,
    old_remains: bool = False,
) -> WorkspaceTask:
    current = manager.tasks[task.id]
    saved = next(item for item in persisted(manager, workspace)["tasks"] if item["id"] == task.id)
    assert current.prompt == "Edited description"
    assert saved["prompt"] == current.prompt
    assert saved["attachments"] == [a.model_dump(mode="json") for a in current.attachments]
    assert len(current.attachments) == 1
    assert current.attachments[0].id != task.attachments[0].id
    assert Path(current.attachments[0].path).read_bytes() == PNG
    assert Path(task.attachments[0].path).exists() is old_remains
    assert (
        Path(task.attachments[0].path).parent / "unrelated.keep"
    ).read_bytes() == b"not owned by this edit"
    assert not manager._task_execution_operations
    return current


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["empty", "related", "parent", "session", "second-image"])
async def test_invalid_edit_preserves_task_and_original_image(
    manager: WorkspaceManager, workspace: Workspace, invalid: str
) -> None:
    task = await make_task(manager, workspace)
    before_state = manager._workspace_state_file(workspace.id).read_bytes()
    before_files = files(task)
    changes: dict[str, Any]
    if invalid == "empty":
        changes = {"prompt": "", "add_attachments": []}
    elif invalid == "related":
        changes = {"related_task_id": task.id}
    elif invalid == "parent":
        changes = {"parent_task_id": task.id}
    elif invalid == "session":
        changes = {"session_id": "missing-session"}
    else:
        changes = {
            "add_attachments": [
                image(),
                image().model_copy(update={"data_url": "data:image/png;base64,AAAA"}),
            ]
        }
    with pytest.raises(HTTPException) as caught:
        await edit(task, **changes)
    assert caught.value.status_code in {400, 404}
    if invalid == "empty":
        assert caught.value.detail == "Task description is required"
    assert_original(manager, workspace, task, before_state, before_files)


@pytest.mark.asyncio
async def test_partial_attachment_batch_failure_removes_only_new_files(
    manager: WorkspaceManager, workspace: Workspace, monkeypatch: MonkeyPatch
) -> None:
    task = await make_task(manager, workspace)
    before_state = manager._workspace_state_file(workspace.id).read_bytes()
    before_files = files(task)
    original_fsync = _attachments.os.fsync
    calls = 0

    def fail_second_file(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second image flush failed")
        original_fsync(fd)

    monkeypatch.setattr(_attachments.os, "fsync", fail_second_file)
    with pytest.raises(OSError, match="second image flush failed"):
        await edit(task, add_attachments=[image("one.png"), image("two.png")])
    assert calls == 2
    assert_original(manager, workspace, task, before_state, before_files)


@pytest.mark.asyncio
async def test_attachment_name_collision_never_overwrites_or_deletes_original(
    manager: WorkspaceManager, workspace: Workspace, monkeypatch: MonkeyPatch
) -> None:
    task = await make_task(manager, workspace)
    before_state = manager._workspace_state_file(workspace.id).read_bytes()
    before_files = files(task)
    monkeypatch.setattr(
        _attachments.uuid, "uuid4", lambda: SimpleNamespace(hex=task.attachments[0].id)
    )
    with pytest.raises(FileExistsError):
        await edit(task, add_attachments=[image("original.png")])
    assert_original(manager, workspace, task, before_state, before_files)


@pytest.mark.asyncio
@pytest.mark.parametrize("control", list(TaskExecutionControl))
@pytest.mark.parametrize("failure", ["save", "replace"])
async def test_precommit_failure_restores_record_and_only_cleans_new_images(
    manager: WorkspaceManager,
    workspace: Workspace,
    monkeypatch: MonkeyPatch,
    control: TaskExecutionControl,
    failure: str,
) -> None:
    task = await make_task(manager, workspace, control)
    state_path = manager._workspace_state_file(workspace.id)
    before_state = state_path.read_bytes()
    before_files = files(task)
    if failure == "save":
        monkeypatch.setattr(manager, "_save_state", Mock(side_effect=OSError("before commit")))
    else:
        original = manager._atomic_write_text

        def fail_replace(path: Path, text: str) -> None:
            if path == state_path:
                raise OSError("before commit")
            original(path, text)

        monkeypatch.setattr(manager, "_atomic_write_text", fail_replace)
    with pytest.raises(OSError, match="before commit"):
        await edit(task)
    assert_original(manager, workspace, task, before_state, before_files)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["after-replace", "after-save", "snapshot"])
async def test_confirmed_commit_survives_late_save_and_snapshot_errors(
    manager: WorkspaceManager, workspace: Workspace, monkeypatch: MonkeyPatch, failure: str
) -> None:
    task = await make_task(manager, workspace)
    state_path = manager._workspace_state_file(workspace.id)
    if failure == "after-replace":
        original_atomic = manager._atomic_write_text

        def replace_then_fail(path: Path, text: str) -> None:
            original_atomic(path, text)
            if path == state_path:
                raise OSError("after replacement")

        monkeypatch.setattr(manager, "_atomic_write_text", replace_then_fail)
    elif failure == "after-save":
        original_save = manager._save_state

        def save_then_fail() -> None:
            original_save()
            raise OSError("after save")

        monkeypatch.setattr(manager, "_save_state", save_then_fail)
    else:
        monkeypatch.setattr(
            manager, "_write_snapshot", Mock(side_effect=OSError("snapshot failed"))
        )
    result = await edit(task)
    assert result.id == task.id
    assert_committed(manager, workspace, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [asyncio.CancelledError, KeyboardInterrupt])
@pytest.mark.parametrize("after_commit", [False, True])
async def test_interrupt_preserves_its_type_and_respects_commit_boundary(
    manager: WorkspaceManager,
    workspace: Workspace,
    monkeypatch: MonkeyPatch,
    error_type: type[BaseException],
    after_commit: bool,
) -> None:
    task = await make_task(manager, workspace)
    before_state = manager._workspace_state_file(workspace.id).read_bytes()
    before_files = files(task)
    original_save = manager._save_state

    def interrupt_save() -> None:
        if after_commit:
            original_save()
        raise error_type("interrupted")

    monkeypatch.setattr(manager, "_save_state", interrupt_save)
    with pytest.raises(error_type):
        await edit(task)
    if after_commit:
        assert_committed(manager, workspace, task)
    else:
        assert_original(manager, workspace, task, before_state, before_files)
    assert manager._report_intake_workspace.get() is None
    assert manager._workspace_mutation_held.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unreadable", "third-version"])
async def test_unknown_commit_keeps_candidate_and_both_sets_of_files(
    manager: WorkspaceManager, workspace: Workspace, monkeypatch: MonkeyPatch, failure: str
) -> None:
    task = await make_task(manager, workspace)
    state_path = manager._workspace_state_file(workspace.id)
    before = state_path.read_bytes()
    expected_disk = before
    if failure == "unreadable":
        original_read = manager._read_task_edit_state
        calls = 0

        def read_once(workspace_id: str) -> bytes | None:
            nonlocal calls
            calls += 1
            if calls > 1:
                raise OSError("readback unavailable")
            return original_read(workspace_id)

        monkeypatch.setattr(manager, "_read_task_edit_state", read_once)
        monkeypatch.setattr(manager, "_save_state", Mock(side_effect=OSError("save interrupted")))
    else:
        expected_disk = before + b"\n"

        def write_other_version() -> None:
            state_path.write_bytes(expected_disk)
            raise OSError("different version")

        monkeypatch.setattr(manager, "_save_state", write_other_version)
    with pytest.raises(OSError):
        await edit(task)
    current = manager.tasks[task.id]
    assert current.prompt == "Edited description"
    assert current.attachments[0].id != task.attachments[0].id
    assert Path(current.attachments[0].path).read_bytes() == PNG
    assert Path(task.attachments[0].path).read_bytes() == PNG
    assert state_path.read_bytes() == expected_disk
    saved = next(item for item in json.loads(expected_disk)["tasks"] if item["id"] == task.id)
    assert saved["attachments"] == [a.model_dump(mode="json") for a in task.attachments]
    assert not manager._task_execution_operations


@pytest.mark.asyncio
async def test_old_file_cleanup_error_does_not_rollback_new_attachment(
    manager: WorkspaceManager, workspace: Workspace, monkeypatch: MonkeyPatch
) -> None:
    task = await make_task(manager, workspace)
    old_path = Path(task.attachments[0].path)
    original_unlink = Path.unlink

    def fail_old_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == old_path:
            raise OSError("old file cleanup failed")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_old_unlink)
    await edit(task)
    assert_committed(manager, workspace, task, old_remains=True)
    assert old_path.read_bytes() == PNG


@pytest.mark.asyncio
async def test_reparent_and_attachments_rollback_together_before_commit(
    manager: WorkspaceManager, workspace: Workspace, monkeypatch: MonkeyPatch
) -> None:
    task = await make_task(manager, workspace)
    child = manager.create_task(
        workspace.id, WorkspaceTaskCreate(title="child", prompt="child", parent_task_id=task.id)
    )
    target = manager.create_task(workspace.id, WorkspaceTaskCreate(title="target", prompt="target"))
    before_tasks = {key: value.model_dump() for key, value in manager.tasks.items()}
    before_state = manager._workspace_state_file(workspace.id).read_bytes()
    before_files = files(task)
    monkeypatch.setattr(manager, "_save_state", Mock(side_effect=OSError("before tree commit")))
    with pytest.raises(OSError, match="before tree commit"):
        await edit(task, parent_task_id=target.id)
    assert {key: value.model_dump() for key, value in manager.tasks.items()} == before_tasks
    assert manager.tasks[child.id].parent_task_id == task.id
    assert_original(manager, workspace, task, before_state, before_files)


def add_session(
    manager: WorkspaceManager,
    workspace: Workspace,
    task: WorkspaceTask,
    session_id: str,
    role: WorkspaceSessionRole,
    *,
    bound: bool = False,
    stopped: bool = False,
    ephemeral: bool = False,
) -> ManagedSession:
    now = _wm._now()
    session = ManagedSession(
        id=session_id,
        workspace_id=workspace.id,
        tab_id=f"tab-{session_id}",
        role=role,
        agent_type=task.agent_type,
        status=ManagedSessionStatus.STOPPED if stopped else ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.OFFLINE if stopped else AgentRuntimeStatus.IDLE,
        task_id=task.id if bound else None,
        current_task_id=task.id if bound else None,
        title=session_id,
        workspace_path=workspace.path,
        tmux_session=f"not-used-{session_id}",
        ephemeral=ephemeral,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session
    return session


async def configured_task(
    manager: WorkspaceManager, workspace: Workspace, status: WorkspaceTaskStatus
) -> tuple[WorkspaceTask, ManagedSession, ManagedSession | None]:
    task = await make_task(manager, workspace)
    worker = add_session(
        manager,
        workspace,
        task,
        "worker",
        WorkspaceSessionRole.ORCHESTRATOR,
        bound=status != WorkspaceTaskStatus.WORKING,
    )
    reviewer = None
    if status in {WorkspaceTaskStatus.DONE, WorkspaceTaskStatus.REVIEW}:
        reviewer = add_session(
            manager,
            workspace,
            task,
            "old-reviewer",
            WorkspaceSessionRole.REVIEWER,
            bound=True,
            stopped=status == WorkspaceTaskStatus.REVIEW,
            ephemeral=status == WorkspaceTaskStatus.DONE,
        )
    task = task.model_copy(
        update={
            "session_id": worker.id,
            "review_session_id": reviewer.id if reviewer else None,
            "review_cycle": 7,
        }
    )
    manager.tasks[task.id] = task
    manager._save_state()
    return task, worker, reviewer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        WorkspaceTaskStatus.WORKING,
        WorkspaceTaskStatus.DONE,
        WorkspaceTaskStatus.REVIEW,
    ],
)
async def test_precommit_failure_restores_authoritative_session_and_report_maps(
    manager: WorkspaceManager,
    workspace: Workspace,
    monkeypatch: MonkeyPatch,
    status: WorkspaceTaskStatus,
) -> None:
    task, worker, reviewer = await configured_task(manager, workspace, status)
    before_state = manager._workspace_state_file(workspace.id).read_bytes()
    before_files = files(task)
    before_sessions = {key: item.model_dump() for key, item in manager.sessions.items()}
    before_reports = {key: item.model_dump() for key, item in manager.reports.items()}
    archive, review = Mock(), AsyncMock()
    monkeypatch.setattr(manager, "_write_task_record", archive)
    monkeypatch.setattr(manager, "_request_task_review", review)

    def inspect_then_fail() -> None:
        assert manager.tasks[task.id].status == status
        if status == WorkspaceTaskStatus.WORKING:
            assert manager.sessions[worker.id].current_task_id == task.id
        elif status == WorkspaceTaskStatus.DONE:
            assert manager.sessions[worker.id].current_task_id is None
            assert reviewer is not None and reviewer.id not in manager.sessions
        else:
            assert reviewer is not None
            assert manager.sessions[reviewer.id].current_task_id is None
            assert manager.tasks[task.id].review_session_id is None
            report = next(item for item in manager.reports.values() if item.task_id == task.id)
            assert report.review_cycle == 7 and report.session_id == worker.id
        raise OSError("before mapping commit")

    monkeypatch.setattr(manager, "_save_state", inspect_then_fail)
    with pytest.raises(OSError, match="before mapping commit"):
        await edit(task, status=status)
    assert_original(manager, workspace, task, before_state, before_files)
    assert {key: item.model_dump() for key, item in manager.sessions.items()} == before_sessions
    assert {key: item.model_dump() for key, item in manager.reports.items()} == before_reports
    archive.assert_not_called()
    review.assert_not_awaited()
    _wm.ttyd_manager.rename_tab.assert_not_called()
    _wm.ttyd_manager.delete_tab.assert_not_awaited()


@pytest.mark.asyncio
async def test_working_mapping_is_committed_before_external_rename(
    manager: WorkspaceManager, workspace: Workspace, monkeypatch: MonkeyPatch
) -> None:
    task, worker, _ = await configured_task(manager, workspace, WorkspaceTaskStatus.WORKING)
    seen: list[str] = []

    def rename(tab_id: str, title: str) -> bool:
        state = persisted(manager, workspace)
        saved_task = next(item for item in state["tasks"] if item["id"] == task.id)
        saved_session = next(item for item in state["sessions"] if item["id"] == worker.id)
        assert saved_task["status"] == "working"
        assert saved_session["task_id"] == saved_session["current_task_id"] == task.id
        assert manager._workspace_mutation_held.get() is None
        seen.append(tab_id)
        return True

    monkeypatch.setattr(_wm.ttyd_manager, "rename_tab", rename)
    dispatch = AsyncMock()
    monkeypatch.setattr(manager, "dispatch_workspace", dispatch)
    await edit(task, status=WorkspaceTaskStatus.WORKING)
    assert seen == [worker.tab_id]
    assert_committed(manager, workspace, task)
    dispatch.assert_awaited_once_with(workspace.id)


@pytest.mark.asyncio
async def test_real_review_request_keeps_current_session_cycle_and_dispatch_sequence(
    manager: WorkspaceManager, workspace: Workspace, monkeypatch: MonkeyPatch
) -> None:
    task, worker, old_reviewer = await configured_task(
        manager, workspace, WorkspaceTaskStatus.REVIEW
    )
    assert old_reviewer is not None
    task = task.model_copy(update={"session_id": None})
    manager.tasks[task.id] = task
    reviewer = add_session(manager, workspace, task, "new-reviewer", WorkspaceSessionRole.REVIEWER)
    manager._save_state()
    saved: list[dict[str, Any]] = []
    original_save = manager._save_state

    def capture_save() -> None:
        original_save()
        saved.append(persisted(manager, workspace))

    async def no_rename(
        session: ManagedSession, _task: WorkspaceTask, **_kwargs: Any
    ) -> ManagedSession:
        return session

    async def send(session_id: str, message: str, **_kwargs: Any) -> None:
        state = persisted(manager, workspace)
        live_task = next(item for item in state["tasks"] if item["id"] == task.id)
        live_session = next(item for item in state["sessions"] if item["id"] == reviewer.id)
        assert live_task["review_session_id"] == reviewer.id
        assert live_session["task_id"] == live_session["current_task_id"] == task.id
        assert manager._workspace_mutation_held.get() is None
        assert session_id == reviewer.id and message == "bounded review prompt"

    monkeypatch.setattr(manager, "_save_state", capture_save)
    monkeypatch.setattr(manager, "_select_or_create_reviewer", AsyncMock(return_value=reviewer))
    monkeypatch.setattr(manager, "_rename_session_for_task", no_rename)
    monkeypatch.setattr(manager, "_lesson_context_payload", Mock(return_value={}))
    monkeypatch.setattr(manager, "_build_review_prompt", Mock(return_value="bounded review prompt"))
    sent, dispatch = AsyncMock(side_effect=send), AsyncMock()
    monkeypatch.setattr(manager, "send_session_message", sent)
    monkeypatch.setattr(manager, "dispatch_workspace", dispatch)
    await edit(task, status=WorkspaceTaskStatus.REVIEW, session_id=worker.id)
    first_task = next(item for item in saved[0]["tasks"] if item["id"] == task.id)
    first_report = next(item for item in saved[0]["reports"] if item["task_id"] == task.id)
    old_mapping = next(item for item in saved[0]["sessions"] if item["id"] == old_reviewer.id)
    assert first_task["status"] == "review" and first_task["review_session_id"] is None
    assert old_mapping["current_task_id"] is None
    assert first_report["review_cycle"] == 7 and first_report["session_id"] == worker.id
    sent.assert_awaited_once()
    dispatch.assert_awaited_once_with(workspace.id)
    assert_committed(manager, workspace, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rename", "archive", "dispatch"])
async def test_postcommit_failure_never_restores_old_record_or_deletes_new_image(
    manager: WorkspaceManager, workspace: Workspace, monkeypatch: MonkeyPatch, failure: str
) -> None:
    status = {
        "rename": WorkspaceTaskStatus.WORKING,
        "archive": WorkspaceTaskStatus.DONE,
        "dispatch": WorkspaceTaskStatus.QUEUED,
    }[failure]
    task, worker, reviewer = await configured_task(manager, workspace, status)
    dispatch = AsyncMock()
    monkeypatch.setattr(manager, "dispatch_workspace", dispatch)
    if failure == "rename":
        monkeypatch.setattr(
            _wm.ttyd_manager, "rename_tab", Mock(side_effect=OSError("postcommit failure"))
        )
    elif failure == "archive":
        monkeypatch.setattr(
            manager, "_write_task_record", Mock(side_effect=OSError("postcommit failure"))
        )
    else:
        dispatch.side_effect = OSError("postcommit failure")
    with pytest.raises(OSError, match="postcommit failure"):
        await edit(task, status=status)
    current = assert_committed(manager, workspace, task)
    assert current.status == status
    state = persisted(manager, workspace)
    mapped = next(item for item in state["sessions"] if item["id"] == worker.id)
    if status == WorkspaceTaskStatus.DONE:
        assert mapped["task_id"] is None and mapped["current_task_id"] is None
        assert reviewer is not None
        assert all(item["id"] != reviewer.id for item in state["sessions"])
        _wm.ttyd_manager.delete_tab.assert_awaited_once_with(reviewer.tab_id)
    elif status == WorkspaceTaskStatus.WORKING:
        assert mapped["task_id"] == mapped["current_task_id"] == task.id


@pytest.mark.asyncio
async def test_postcommit_cleanup_keeps_operation_reservation_but_releases_workspace_lock(
    manager: WorkspaceManager, workspace: Workspace, monkeypatch: MonkeyPatch
) -> None:
    task, _, reviewer = await configured_task(manager, workspace, WorkspaceTaskStatus.DONE)
    assert reviewer is not None
    entered, release = asyncio.Event(), asyncio.Event()

    async def delete_tab(tab_id: str) -> None:
        assert tab_id == reviewer.tab_id
        assert manager._workspace_mutation_held.get() is None
        entered.set()
        await release.wait()

    monkeypatch.setattr(_wm.ttyd_manager, "delete_tab", delete_tab)
    monkeypatch.setattr(manager, "_write_task_record", Mock())
    monkeypatch.setattr(manager, "dispatch_workspace", AsyncMock())
    pending = asyncio.create_task(edit(task, status=WorkspaceTaskStatus.DONE))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        current = manager.tasks[task.id]
        request = TaskExecutionHandoffRequest(
            call_id="during-postcommit-cleanup",
            expected_execution_epoch=current.execution_epoch,
            expected_progress_revision=current.progress_revision,
            execution_control=TaskExecutionControl.INITIATOR,
            new_reporter_key="R" * 48,
        )
        with pytest.raises(TaskExecutionConflict, match="not_released"):
            await asyncio.wait_for(
                manager.handoff_task_execution(workspace.id, task.id, request), 1
            )
    finally:
        release.set()
        await asyncio.wait_for(pending, 5)
    assert_committed(manager, workspace, task)


@pytest.mark.asyncio
async def test_attachment_edit_writes_only_its_workspace(
    manager: WorkspaceManager, workspace: Workspace, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    other_path = tmp_path / "other-repo"
    other_path.mkdir()
    other = manager.create_workspace(WorkspaceCreate(name="other", path=str(other_path)))
    task = await make_task(manager, workspace)
    other_file = manager._workspace_state_file(other.id)
    before_other = other_file.read_bytes()
    writes: list[Path] = []
    original_atomic = manager._atomic_write_text

    def capture_write(path: Path, text: str) -> None:
        writes.append(path)
        original_atomic(path, text)

    monkeypatch.setattr(manager, "_atomic_write_text", capture_write)
    await edit(task)
    assert writes == [
        manager._workspace_state_file(workspace.id),
        manager.snapshot_path(workspace.id),
    ]
    assert other_file.read_bytes() == before_other
    assert_committed(manager, workspace, task)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("save_type", "read_type"),
    [
        (asyncio.CancelledError, OSError),
        (KeyboardInterrupt, OSError),
        (OSError, asyncio.CancelledError),
        (OSError, KeyboardInterrupt),
        (asyncio.CancelledError, asyncio.CancelledError),
        (OSError, OSError),
    ],
)
async def test_failed_readback_cannot_downgrade_an_interrupt(
    manager: WorkspaceManager,
    workspace: Workspace,
    monkeypatch: MonkeyPatch,
    save_type: type[BaseException],
    read_type: type[BaseException],
) -> None:
    task = await make_task(manager, workspace)
    state_path = manager._workspace_state_file(workspace.id)
    before = state_path.read_bytes()
    saved_error, read_error = save_type("save"), read_type("readback")
    original_read = manager._read_task_edit_state
    calls = 0

    def fail_readback(workspace_id: str) -> bytes | None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise read_error
        return original_read(workspace_id)

    monkeypatch.setattr(manager, "_read_task_edit_state", fail_readback)
    monkeypatch.setattr(manager, "_save_state", Mock(side_effect=saved_error))
    expected = saved_error if not isinstance(saved_error, Exception) else read_error
    with pytest.raises(type(expected)) as caught:
        await edit(task)
    assert caught.value is expected
    assert state_path.read_bytes() == before
    current = manager.tasks[task.id]
    assert current.attachments[0].id != task.attachments[0].id
    assert Path(current.attachments[0].path).read_bytes() == PNG
    assert Path(task.attachments[0].path).read_bytes() == PNG
    assert not manager._task_execution_operations
    assert manager._report_intake_workspace.get() is None
