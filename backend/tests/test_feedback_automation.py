"""Automatic feedback must be quiet, fresh-only, bounded and source-backed."""

import asyncio
import importlib
import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from claude_hub.models import (
    AgentRuntimeStatus,
    FeedbackLessonCreate,
    FeedbackSummaryMode,
    ManagedSessionStatus,
    Workspace,
    WorkspaceTaskStatus,
)
from claude_hub.services.feedback_automation import (
    ChatCorrectionCreate,
    FeedbackAutomationSettings,
    FeedbackAutomationStore,
)
from claude_hub.services.feedback_lessons import FeedbackLessonStore, FeedbackLessonValidationError
from claude_hub.services.workspace_manager import WorkspaceManager

wm = importlib.import_module("claude_hub.services.workspace_manager")


def _record(
    root: Path, task_id: str, now: datetime, *, internal: bool = False, failed: bool = True
) -> Path:
    directory = root / "ws" / "task_records"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{now.isoformat()}-{task_id}.json"
    path.write_text(
        json.dumps(
            {
                "workspace_id": "ws",
                "archived_at": now.isoformat(),
                "task": {
                    "id": task_id,
                    "system_internal": internal,
                    "title": "checkout validation",
                    "status": "done",
                },
                "reports": [
                    {
                        "id": "report-1",
                        "state": "review_failed" if failed else "completed",
                        "message": "wrong checkout",
                    }
                ],
            }
        )
    )
    return path


def _manager(root: Path, monkeypatch: pytest.MonkeyPatch, now: datetime) -> WorkspaceManager:
    manager = object.__new__(WorkspaceManager)
    manager.workspaces = {
        "ws": Workspace(
            id="ws",
            name="Test",
            path=str(root),
            default_branch="main",
            session_prefix="test",
            created_at=now,
            updated_at=now,
        )
    }
    manager.tasks = {}
    manager.sessions = {}
    manager._feedback_summary_locks = {}
    monkeypatch.setattr(manager, "_feedback_store", lambda: FeedbackLessonStore(root))
    monkeypatch.setattr(
        manager, "_workspace_task_records_dir", lambda wid: root / wid / "task_records"
    )
    monkeypatch.setattr(wm, "_now", lambda: now)
    return manager


def _event(
    root: Path,
    *,
    turn: str = "turn-1",
    text: str = "Always verify the feature checkout before reviewing.",
    metadata: dict | None = None,
) -> None:
    directory = root / "terminal-tabs/agent_streams"
    directory.mkdir(parents=True, exist_ok=True)
    event = {
        "type": "turn_started",
        "tab_id": "tab-1",
        "turn_id": turn,
        "message_id": f"{turn}:user",
        "stream_sequence": 1,
        "payload": {"summary": text, **({"metadata": metadata} if metadata else {})},
    }
    with (directory / "terminal-tab-tab-1.jsonl").open("a") as out:
        out.write(json.dumps(event) + "\n")


@pytest.mark.asyncio
async def test_bootstrap_skips_old_internal_clean_and_empty_without_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 10, 5, 10)
    manager = _manager(tmp_path, monkeypatch, now)
    dispatch = AsyncMock()
    monkeypatch.setattr(manager, "_summarize_workspace_feedback_locked", dispatch)
    _record(tmp_path, "old-failure", now - timedelta(days=1))
    _record(tmp_path, "internal-loop", now + timedelta(seconds=1), internal=True)
    _record(tmp_path, "clean", now + timedelta(seconds=1), failed=False)
    await manager._tick_feedback_automation()
    await manager._tick_feedback_automation()
    dispatch.assert_not_awaited()
    assert manager.feedback_automation_status("ws")["last_outcome"] == "no_fresh_eligible_evidence"
    assert not (tmp_path / "ws/feedback/summary-runs").exists()


@pytest.mark.asyncio
async def test_fresh_batch_concurrency_cooldown_failure_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 10, 5, 10)
    manager = _manager(tmp_path, monkeypatch, now)
    manager.feedback_automation_status("ws")
    for i in range(12):
        _record(tmp_path, f"task-{i}", now + timedelta(seconds=i + 1))
    dispatch = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    monkeypatch.setattr(manager, "_summarize_workspace_feedback_locked", dispatch)
    await asyncio.gather(manager._tick_feedback_automation(), manager._tick_feedback_automation())
    assert dispatch.await_count == 1
    selected = dispatch.call_args.kwargs["summary_input"]
    assert len(selected["input_records"]) == 5
    assert selected["_prompt_char_limit"] == 24000
    assert not (tmp_path / "ws/feedback/index.json").exists()
    restarted = _manager(tmp_path, monkeypatch, now + timedelta(minutes=20))
    monkeypatch.setattr(restarted, "_summarize_workspace_feedback_locked", dispatch)
    await restarted._tick_feedback_automation()
    assert dispatch.await_count == 1
    monkeypatch.setattr(wm, "_now", lambda: now + timedelta(hours=1, seconds=1))
    await restarted._tick_feedback_automation()
    assert dispatch.await_count == 2


@pytest.mark.asyncio
async def test_disabled_busy_native_and_manual_reaper_suppress_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 10, 5, 10)
    manager = _manager(tmp_path, monkeypatch, now)
    manager.configure_feedback_automation("ws", FeedbackAutomationSettings(enabled=False))
    _record(tmp_path, "fresh", now + timedelta(seconds=1))
    dispatch = AsyncMock(return_value=SimpleNamespace(id="run-1", task_id="reaper"))
    monkeypatch.setattr(manager, "_summarize_workspace_feedback_locked", dispatch)
    await manager._tick_feedback_automation()
    manager.configure_feedback_automation("ws", FeedbackAutomationSettings())
    manager._feedback_chat_busy = lambda: True
    await manager._tick_feedback_automation()
    dispatch.assert_not_awaited()
    monkeypatch.setattr(wm, "_now", lambda: now + timedelta(minutes=2))
    manager._feedback_chat_busy = lambda: False
    manager.sessions = {
        "worker": SimpleNamespace(workspace_id="ws", status=ManagedSessionStatus.WORKING)
    }
    await manager._tick_feedback_automation()
    dispatch.assert_not_awaited()
    manager.sessions = {}
    manager.tasks = {
        "manual": SimpleNamespace(
            workspace_id="ws",
            system_internal=True,
            internal_kind="feedback_reaper",
            status=WorkspaceTaskStatus.WORKING,
            manual_aborted_at=None,
            created_at=now,
            id="manual",
        )
    }
    await manager._tick_feedback_automation()
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_waiting_human_review_does_not_starve_feedback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 10, 5, 10)
    manager = _manager(tmp_path, monkeypatch, now)
    manager.feedback_automation_status("ws")
    _record(tmp_path, "fresh", now + timedelta(seconds=1))
    manager.tasks = {
        "waiting": SimpleNamespace(
            workspace_id="ws", system_internal=False, status=WorkspaceTaskStatus.REVIEW
        )
    }
    manager.sessions = {
        "idle": SimpleNamespace(
            workspace_id="ws",
            status=ManagedSessionStatus.IDLE,
            runtime_status=AgentRuntimeStatus.IDLE,
            pending_call_ids=[],
            processing_call_ids=[],
        )
    }
    dispatch = AsyncMock(return_value=SimpleNamespace(id="run-1", task_id="reaper"))
    monkeypatch.setattr(manager, "_summarize_workspace_feedback_locked", dispatch)
    await manager._tick_feedback_automation()
    dispatch.assert_awaited_once()


def test_correction_exact_source_dedup_and_staged_commit(tmp_path: Path) -> None:
    now = datetime(2026, 10, 5, 10)
    _event(tmp_path)
    controls = FeedbackAutomationStore(tmp_path)
    request = ChatCorrectionCreate(
        tab_id="tab-1",
        turn_id="turn-1",
        message_id="turn-1:user",
        quote="verify the feature checkout before reviewing",
    )
    correction = controls.capture("ws", request, now)
    again = controls.capture(
        "ws", request.model_copy(update={"quote": "Always verify the feature checkout"}), now
    )
    assert correction == again
    with pytest.raises(ValueError, match="exact correction"):
        controls.capture("ws", request.model_copy(update={"quote": "invented correction"}), now)
    store = FeedbackLessonStore(tmp_path)
    selected = store.prepare_summary_input(
        "ws",
        tmp_path / "ws/task_records",
        mode=FeedbackSummaryMode.INCREMENTAL,
        limit=5,
        force=False,
        automatic_since=now,
    )
    assert selected["input_record_ids"] == [correction.id]
    assert selected["input_records"][0]["digest"]["source_message_id"] == "turn-1:user"
    assert "task_id" not in selected["input_records"][0]
    store.stage_summary_input(selected, [selected["input_records"][0]["_path"]])
    # In-flight evidence remains eligible until successful completion.
    assert not store.prepare_summary_input(
        "ws",
        tmp_path / "ws/task_records",
        mode=FeedbackSummaryMode.INCREMENTAL,
        limit=5,
        force=False,
    )["cache_hit"]
    store.commit_staged_summary_input("ws", selected["run_id"])
    assert store.prepare_summary_input(
        "ws",
        tmp_path / "ws/task_records",
        mode=FeedbackSummaryMode.INCREMENTAL,
        limit=5,
        force=False,
    )["cache_hit"]
    payload = FeedbackLessonCreate(
        summary="Verify review checkout",
        applies_when=["feature review"],
        do="Verify cwd",
        avoid="Using main checkout",
        source_record_ids=[correction.id],
        confidence=0.9,
    )
    lesson = store.create_lesson("ws", payload)
    assert lesson.confidence == 0.6
    assert store.lesson_context_payload("ws", "unrelated banana") == []
    assert store.lesson_context_payload("ws", "") == []
    assert store.lesson_context_payload("ws", "feature review")[0]["id"] == lesson.id
    with pytest.raises(FeedbackLessonValidationError, match="verified Chat"):
        store.create_lesson("other", payload)


def test_sources_exclude_synthetic_turns_and_path_injection(tmp_path: Path) -> None:
    _event(tmp_path, turn="scheduled-1")
    _event(tmp_path, turn="goal-1")
    _event(tmp_path, turn="machine", metadata={"protocol": "goal-continuation-v1"})
    _event(tmp_path)
    controls = FeedbackAutomationStore(tmp_path)
    assert [item["turn_id"] for item in controls.sources("tab-1")] == ["turn-1"]
    with pytest.raises(ValueError, match="invalid tab"):
        controls.sources("../../escape")


def test_internal_reaper_archive_never_returns_even_in_full_mode(tmp_path: Path) -> None:
    _record(tmp_path, "internal", datetime.now(), internal=True)
    store = FeedbackLessonStore(tmp_path)
    selected = store.prepare_summary_input(
        "ws", tmp_path / "ws/task_records", mode=FeedbackSummaryMode.FULL, limit=30, force=True
    )
    assert selected["cache_hit"]


@pytest.mark.asyncio
async def test_corrupt_automation_state_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now()
    manager = _manager(tmp_path, monkeypatch, now)
    manager.feedback_automation_status("ws")
    (tmp_path / "ws/feedback/automation.json").write_text("{}")
    _record(tmp_path, "fresh", now)
    dispatch = AsyncMock()
    monkeypatch.setattr(manager, "_summarize_workspace_feedback_locked", dispatch)
    await manager._tick_feedback_automation()
    dispatch.assert_not_awaited()


def test_automatic_scan_cursor_visits_more_than_1000_uuid_names(tmp_path: Path) -> None:
    now = datetime.now() - timedelta(seconds=10)
    controls = FeedbackAutomationStore(tmp_path)
    directory = tmp_path / "ws/task_records"
    for i in range(1005):
        path = _record(tmp_path, f"task-{i:04}", now, failed=i == 1004)
        path.rename(directory / f"{i:04}.json")
    store = FeedbackLessonStore(tmp_path)
    first = store.prepare_summary_input(
        "ws",
        directory,
        mode=FeedbackSummaryMode.INCREMENTAL,
        limit=5,
        force=False,
        automatic_since=now,
    )
    assert first["cache_hit"]
    second = store.prepare_summary_input(
        "ws",
        directory,
        mode=FeedbackSummaryMode.INCREMENTAL,
        limit=5,
        force=False,
        automatic_since=now,
        automatic_cursor=first["_scan_cursor"],
    )
    assert second["input_record_ids"] == ["task-1004"]
    # New IDs sorting before the cursor remain reachable on the next wrap.
    new = _record(tmp_path, "new-low", now)
    new.rename(directory / "0000-low.json")
    third = store.prepare_summary_input(
        "ws",
        directory,
        mode=FeedbackSummaryMode.INCREMENTAL,
        limit=5,
        force=False,
        automatic_since=now,
        automatic_cursor=second["_scan_cursor"],
    )
    assert "new-low" in third["input_record_ids"]


def test_automatic_scan_read_budget_skips_oversized_files_without_starvation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now() - timedelta(seconds=10)
    directory = tmp_path / "ws/task_records"
    directory.mkdir(parents=True)
    for index in range(9):
        (directory / f"{index:04}.json").write_bytes(b"x" * (1024 * 1024 + 100))
    _record(tmp_path, "reachable", now).rename(directory / "9999.json")
    real_open = Path.open
    read_bytes = 0

    @contextmanager
    def counted_open(path: Path, mode: str = "r", *args: object, **kwargs: object):
        nonlocal read_bytes
        with real_open(path, mode, *args, **kwargs) as stream:
            if path.parent != directory or mode != "rb":
                yield stream
                return

            class CountedRead:
                def read(self, size: int = -1) -> bytes:
                    nonlocal read_bytes
                    assert size > 0, "automatic scans must never read an unbounded file"
                    data = stream.read(size)
                    read_bytes += len(data)
                    return data

            yield CountedRead()

    monkeypatch.setattr(Path, "open", counted_open)
    store = FeedbackLessonStore(tmp_path)
    cursor = ""
    found = False
    for _ in range(2):
        read_bytes = 0
        selected = store.prepare_summary_input(
            "ws",
            directory,
            mode=FeedbackSummaryMode.INCREMENTAL,
            limit=5,
            force=False,
            automatic_since=now,
            automatic_cursor=cursor,
        )
        assert read_bytes <= 8 * 1024 * 1024
        cursor = selected["_scan_cursor"]
        found |= "reachable" in selected["input_record_ids"]
    assert found


@pytest.mark.asyncio
async def test_process_start_watermark_keeps_first_tick_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = datetime.now() - timedelta(minutes=1)
    manager = _manager(tmp_path, monkeypatch, datetime.now())
    manager._feedback_started_at = start
    _record(tmp_path, "between-start-and-first-tick", start + timedelta(seconds=5))
    dispatch = AsyncMock(return_value=SimpleNamespace(id="run-1", task_id="reaper"))
    monkeypatch.setattr(manager, "_summarize_workspace_feedback_locked", dispatch)
    await manager._tick_feedback_automation()
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabling_during_dispatch_survives_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now()
    manager = _manager(tmp_path, monkeypatch, now)
    # Allow for filesystem timestamp granularity so this test reaches dispatch.
    manager._feedback_started_at = now - timedelta(seconds=1)
    manager.feedback_automation_status("ws")
    _record(tmp_path, "fresh", now + timedelta(seconds=1))

    async def dispatch(*args: object, **kwargs: object) -> SimpleNamespace:
        manager.configure_feedback_automation("ws", FeedbackAutomationSettings(enabled=False))
        return SimpleNamespace(id="run-1", task_id="reaper")

    monkeypatch.setattr(manager, "_summarize_workspace_feedback_locked", dispatch)
    await manager._tick_feedback_automation()
    status = manager.feedback_automation_status("ws")
    assert status["last_outcome"] == "started"
    assert status["settings"]["enabled"] is False


@pytest.mark.asyncio
async def test_background_feedback_is_single_owned_task_and_shutdown_joins_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path, monkeypatch, datetime.now())
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def slow_check() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(manager, "_tick_feedback_automation", slow_check)
    manager._kick_feedback_automation()
    owned = manager._feedback_automation_task
    manager._kick_feedback_automation()
    assert manager._feedback_automation_task is owned
    await started.wait()
    manager._monitor_task = None
    await manager.stop_background_monitor()
    assert stopped.is_set()
    assert manager._feedback_automation_task is None


@pytest.mark.asyncio
async def test_correction_http_validates_source_and_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from claude_hub.api import feedback_automation as api
    from claude_hub.auth.dependencies import get_current_user
    from claude_hub.models import ExecutionTarget, SessionKind

    now = datetime.now()
    manager = _manager(tmp_path, monkeypatch, now)
    monkeypatch.setattr(api, "workspace_manager", manager)
    tab = SimpleNamespace(
        session_kind=SessionKind.CHAT, target=ExecutionTarget.LOCAL, cwd=str(tmp_path)
    )
    monkeypatch.setattr(api.ttyd_manager, "get_tab", lambda _tid: tab)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="user")
    _event(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        sources = await client.get("/api/workspaces/tabs/tab-1/feedback/sources")
        assert sources.status_code == 200
        assert sources.json()["sources"][0]["message_id"] == "turn-1:user"
        body = {
            "tab_id": "tab-1",
            "turn_id": "turn-1",
            "message_id": "turn-1:user",
            "quote": "verify the feature checkout before reviewing",
        }
        captured = await client.post("/api/workspaces/ws/feedback/corrections", json=body)
        assert captured.status_code == 200
        body["quote"] = "invented user instruction"
        assert (
            await client.post("/api/workspaces/ws/feedback/corrections", json=body)
        ).status_code == 400
        body["quote"] = "verify the feature checkout before reviewing"
        tab.cwd = str(tmp_path.parent / "other-repo")
        assert (
            await client.post("/api/workspaces/ws/feedback/corrections", json=body)
        ).status_code == 400
        context = await client.get(
            "/api/workspaces/ws/feedback/context", params={"query": "banana"}
        )
        assert context.json() == []


def test_feedback_cli_capture_uses_tab_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    from click.testing import CliRunner

    from claude_hub.cli import main as cli_main
    from claude_hub.cli.client import HubClient
    from claude_hub.cli.commands.feedback import feedback

    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "record"})

    monkeypatch.setenv("CLAUDE_HUB_TAB_ID", "tab-from-env")
    monkeypatch.setattr(
        cli_main,
        "get_client",
        lambda ctx: HubClient("http://test", transport=httpx.MockTransport(handle)),
    )
    result = CliRunner().invoke(
        feedback,
        [
            "capture",
            "ws",
            "--turn-id",
            "turn-1",
            "--message-id",
            "turn-1:user",
            "--quote",
            "Use the feature checkout",
        ],
        obj=SimpleNamespace(json_output=True),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(requests[0].content)["tab_id"] == "tab-from-env"
    assert requests[0].url.path == "/api/workspaces/ws/feedback/corrections"
