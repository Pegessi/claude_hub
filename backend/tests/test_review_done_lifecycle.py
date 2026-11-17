"""End-to-end tests for unified Review vs Done lifecycle across Chat archive/restore, SQLite stream summaries, and MR status cache."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from claude_hub.models import (
    AgentReport,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    ManagedSession,
    ManagedSessionStatus,
    ReviewDecision,
    SessionKind,
    WorkspaceCreate,
    WorkspaceSessionRole,
    WorkspaceTaskCreate,
    WorkspaceTaskStatus,
)
from claude_hub.models.agent_stream import AgentStreamEvent, AgentStreamEventType
from claude_hub.models.schemas import (
    TaskExecutionControl,
    TaskExecutionReference,
    TaskProgressRequest,
    TaskProgressState,
    TaskSourceReference,
)
from claude_hub.services.agent_stream.store import AgentStreamStore
from claude_hub.services.demand_patrol.models import MRStateSnapshot
from claude_hub.services.mr_status_cache import (
    MRStatusCache,
    extract_codebase_mr_urls,
    format_mr_badge_label,
    mr_status_cache,
)
from claude_hub.services.ttyd_manager import TTYDManager, ttyd_manager
from claude_hub.services.workspace_manager import WorkspaceManager

_wm = import_module("claude_hub.services.workspace_manager")
ttyd_manager_mod = import_module("claude_hub.services.ttyd_manager")
KEY = "r" * 48
OTHER_KEY = "z" * 48


def _setup_isolated_manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> WorkspaceManager:
    root = tmp_path / "state"
    root.mkdir(parents=True, exist_ok=True)
    index = root / "index.json"
    monkeypatch.setattr(_wm, "STATE_ROOT", root)
    monkeypatch.setattr(_wm, "INDEX_FILE", index)
    monkeypatch.setattr(_wm._persistence, "INDEX_FILE", index)
    monkeypatch.setattr(_wm._state, "INDEX_FILE", index)
    return WorkspaceManager()


@pytest.mark.asyncio
async def test_archive_closes_only_active_initiator_task_and_restore_lazily_reopens_without_touching_historical_done(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = _setup_isolated_manager(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="Archive Repo", path=str(repo)))

    tab_id = "chat-archive-restore-1"
    tab_state = {"archived": False}
    monkeypatch.setattr(
        ttyd_manager,
        "get_tab",
        lambda tid: (
            SimpleNamespace(id=tid, archived=tab_state["archived"]) if tid == tab_id else None
        ),
    )

    # Create 7 historical explicitly-completed DONE tasks + 1 active WORKING task on the same Chat
    historical_ids: list[str] = []
    for idx in range(7):
        hist_task, _ = await manager.register_task(
            ws.id,
            WorkspaceTaskCreate(
                title=f"Historical task {idx}",
                prompt=f"Old work {idx}",
                execution_control=TaskExecutionControl.INITIATOR,
                request_key=f"hist-req-{idx}",
                reporter_key=KEY,
                source=TaskSourceReference(kind="chat", tab_id=tab_id),
            ),
            actor_key="actor-1",
        )
        completed = await manager.record_task_progress(
            ws.id,
            hist_task.id,
            TaskProgressRequest(
                call_id=f"hist-done-{idx}",
                expected_execution_epoch=hist_task.execution_epoch,
                expected_progress_revision=hist_task.progress_revision,
                state=TaskProgressState.COMPLETED,
                summary=f"Completed {idx}",
            ),
            KEY,
        )
        assert completed.task.status == WorkspaceTaskStatus.DONE
        assert completed.task.closed_reason == "completed"
        assert completed.task.archive_closure is None
        historical_ids.append(hist_task.id)

    active_task, _ = await manager.register_task(
        ws.id,
        WorkspaceTaskCreate(
            title="Active USM validation",
            prompt="Validate MR !1379",
            execution_control=TaskExecutionControl.INITIATOR,
            request_key="active-req-1",
            reporter_key=KEY,
            source=TaskSourceReference(kind="chat", tab_id=tab_id),
            execution_ref=TaskExecutionReference(
                provider="maso",
                session_id=f"terminal-tab-{tab_id}",
                turn_id="turn-before-archive",
                run_epoch=1,
            ),
        ),
        actor_key="actor-1",
    )
    started = await manager.record_task_progress(
        ws.id,
        active_task.id,
        TaskProgressRequest(
            call_id="active-start-1",
            expected_execution_epoch=active_task.execution_epoch,
            expected_progress_revision=active_task.progress_revision,
            state=TaskProgressState.WORKING,
            summary="Running benchmark",
        ),
        KEY,
    )
    assert started.task.status == WorkspaceTaskStatus.WORKING

    # 1. User archives the Chat -> active_task transitions to DONE (closed_reason="chat_archived"),
    # never ORPHANED / Session lost; all 7 historical DONE tasks keep archive_closure=None.
    tab_state["archived"] = True
    closed = await manager.close_initiator_tasks_for_archived_chat(tab_id, last_stream_sequence=42)
    assert closed == [active_task.id]
    after_archive = manager.tasks[active_task.id]
    assert after_archive.status == WorkspaceTaskStatus.DONE
    assert after_archive.closed_reason == "chat_archived"
    assert after_archive.archive_closure is not None
    assert after_archive.archive_closure.last_stream_sequence == 42

    lifecycles = manager.task_lifecycles([after_archive])
    assert lifecycles[active_task.id].phase.value == "done"
    assert lifecycles[active_task.id].reason == "chat_archived"
    assert lifecycles[active_task.id].attention is False

    for hid in historical_ids:
        assert manager.tasks[hid].archive_closure is None
        assert manager.tasks[hid].closed_reason == "completed"

    # 2. User restores (`unarchive`) the Chat JUST to read history -> stays DONE!
    tab_state["archived"] = False
    assert manager.tasks[active_task.id].status == WorkspaceTaskStatus.DONE

    # Replaying a pre-archive TURN_STARTED (sequence <= 42) or child subagent turn must NOT reopen!
    await manager.record_task_stream_event(
        AgentStreamEvent(
            stream_sequence=42,
            session_id=f"terminal-tab-{tab_id}",
            tab_id=tab_id,
            agent_type=AgentType.MASO,
            type=AgentStreamEventType.TURN_STARTED,
            run_epoch=1,
            turn_id="replayed-old-turn",
            payload={"summary": "old"},
            created_at=datetime.now(timezone.utc),
        )
    )
    assert manager.tasks[active_task.id].status == WorkspaceTaskStatus.DONE

    await manager.record_task_stream_event(
        AgentStreamEvent(
            stream_sequence=43,
            session_id=f"terminal-tab-{tab_id}",
            tab_id=tab_id,
            agent_type=AgentType.MASO,
            type=AgentStreamEventType.TURN_STARTED,
            run_epoch=2,
            turn_id="child-subagent-turn",
            payload={"subagent_thread": "child-1"},
            created_at=datetime.now(timezone.utc),
        )
    )
    assert manager.tasks[active_task.id].status == WorkspaceTaskStatus.DONE

    # 3. User sends a genuinely new message in the restored Chat (sequence 44 > 42) ->
    # lazily reopens ONLY active_task to WORKING and binds the new turn!
    await manager.record_task_stream_event(
        AgentStreamEvent(
            stream_sequence=44,
            session_id=f"terminal-tab-{tab_id}",
            tab_id=tab_id,
            agent_type=AgentType.MASO,
            type=AgentStreamEventType.TURN_STARTED,
            run_epoch=2,
            turn_id="fresh-restored-turn",
            payload={"summary": "Continue fixing CI"},
            created_at=datetime.now(timezone.utc),
        )
    )
    reopened = manager.tasks[active_task.id]
    assert reopened.status == WorkspaceTaskStatus.WORKING
    assert reopened.closed_reason is None
    assert reopened.archive_closure is None
    assert reopened.execution_ref is not None
    assert reopened.execution_ref.turn_id == "fresh-restored-turn"
    assert reopened.execution_ref.run_epoch == 2
    assert reopened.runtime_observation is not None
    assert reopened.runtime_observation.status == "active"

    # All 7 historical DONE tasks remain DONE and untouched!
    for hid in historical_ids:
        assert manager.tasks[hid].status == WorkspaceTaskStatus.DONE
        assert manager.tasks[hid].closed_reason == "completed"

    # 4. Also verify explicit authenticated `task progress --state working` can reopen
    # an archive-closed task while historical DONE tasks still reject non-release progress.
    tab_state["archived"] = True
    await manager.close_initiator_tasks_for_archived_chat(tab_id, last_stream_sequence=50)
    tab_state["archived"] = False
    archived_again = manager.tasks[active_task.id]
    assert archived_again.status == WorkspaceTaskStatus.DONE

    reopened_via_cli = await manager.record_task_progress(
        ws.id,
        active_task.id,
        TaskProgressRequest(
            call_id="explicit-reopen-1",
            expected_execution_epoch=archived_again.execution_epoch,
            expected_progress_revision=archived_again.progress_revision,
            state=TaskProgressState.WORKING,
            summary="Resumed explicitly via CLI",
        ),
        KEY,
    )
    assert reopened_via_cli.task.status == WorkspaceTaskStatus.WORKING
    assert reopened_via_cli.task.archive_closure is None


@pytest.mark.asyncio
async def test_sqlite_stream_summary_and_mr_cache_drive_tab_review_and_auto_done_on_merge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = _setup_isolated_manager(monkeypatch, tmp_path)
    monkeypatch.setattr("claude_hub.services.workspace_manager.tasks", manager.tasks)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="MR Repo", path=str(repo)))

    mr_url = "https://[redacted]/seed/xperf_evo/merge_requests/1483"
    doc_example = "https://[redacted]/byteapi/bytedcli/merge_requests/821"
    assert extract_codebase_mr_urls([f"See {doc_example} and {mr_url}"]) == [mr_url]
    assert format_mr_badge_label([mr_url]) == "MR !1483"

    # Write events purely to SQLite AgentStreamStore (no .jsonl file on disk!)
    # Use >4h ago so once the MR merges below, the 4h recent-active window has already elapsed.
    now_utc = datetime.now(timezone.utc)
    tab_id = "sqlite-mr-tab"
    store = AgentStreamStore("terminal-tabs", f"terminal-tab-{tab_id}")
    t0 = (now_utc - timedelta(hours=10, minutes=7)).replace(microsecond=0)
    t1 = (now_utc - timedelta(hours=10)).replace(microsecond=0)
    await store.append(
        AgentStreamEvent(
            stream_sequence=0,
            session_id=f"terminal-tab-{tab_id}",
            tab_id=tab_id,
            agent_type=AgentType.CURSOR,
            type=AgentStreamEventType.TURN_STARTED,
            run_epoch=1,
            turn_id="t-1",
            payload={"summary": "Split operator_trace.py"},
            created_at=t0,
        )
    )
    await store.append(
        AgentStreamEvent(
            stream_sequence=1,
            session_id=f"terminal-tab-{tab_id}",
            tab_id=tab_id,
            agent_type=AgentType.CURSOR,
            type=AgentStreamEventType.TOOL_CALL_STARTED,
            run_epoch=1,
            turn_id="t-1",
            payload={"name": "Edit", "args": {"file": "operator_trace.py"}},
            created_at=t0,
        )
    )
    await store.append(
        AgentStreamEvent(
            stream_sequence=2,
            session_id=f"terminal-tab-{tab_id}",
            tab_id=tab_id,
            agent_type=AgentType.CURSOR,
            type=AgentStreamEventType.TEXT_DELTA,
            run_epoch=1,
            turn_id="t-1",
            payload={"text": f"Updated and pushed {mr_url}."},
            created_at=t1,
        )
    )
    await store.append(
        AgentStreamEvent(
            stream_sequence=3,
            session_id=f"terminal-tab-{tab_id}",
            tab_id=tab_id,
            agent_type=AgentType.CURSOR,
            type=AgentStreamEventType.TURN_COMPLETED,
            run_epoch=1,
            turn_id="t-1",
            payload={"status": "completed", "assistant_text": f"Updated and pushed {mr_url}."},
            created_at=t1,
        )
    )
    assert not store.path.exists(), "SQLite-backed stream has no .jsonl file on disk"

    summary = await store.stream_summary()
    assert summary is not None
    assert summary.excerpt is not None
    assert summary.excerpt.text == f"Updated and pushed {mr_url}."
    assert summary.last_activity_at == t1
    assert summary.assistant_mr_urls == (mr_url,)
    assert summary.has_delivery_mutation is True

    # Register an initiator task referencing this MR
    mr_task, _ = await manager.register_task(
        ws.id,
        WorkspaceTaskCreate(
            title="Split operator_trace helper",
            prompt=f"Deliver {mr_url}",
            execution_control=TaskExecutionControl.INITIATOR,
            request_key="mr-task-1",
            reporter_key=KEY,
            source=TaskSourceReference(kind="chat", tab_id=tab_id),
        ),
        actor_key="actor-mr",
    )
    await manager.record_task_progress(
        ws.id,
        mr_task.id,
        TaskProgressRequest(
            call_id="mr-prog-1",
            expected_execution_epoch=mr_task.execution_epoch,
            expected_progress_revision=mr_task.progress_revision,
            state=TaskProgressState.WORKING,
            summary=f"Pushed {mr_url}",
            artifact_refs=[mr_url],
        ),
        KEY,
    )

    # Seed MR cache with state="open" -> board projects delivery_mr_state="open",
    # and tab status projects review_state="mr_open" / review_label="MR !1483"
    mr_status_cache.clear()
    mr_status_cache.seed(MRStateSnapshot(url=mr_url, state="open", title="Split trace"))
    try:
        lc_open = manager.task_lifecycles([manager.tasks[mr_task.id]])[mr_task.id]
        assert lc_open.delivery_mr_state == "open"
        assert lc_open.delivery_mr_label == "MR !1483"

        # Fake TTYDManager tab status check
        proc = SimpleNamespace(
            tab_id=tab_id,
            name="gb200 perf",
            agent_type=AgentType.CURSOR,
            session_kind=SessionKind.CHAT,
            cursor_transport="acp",
            tmux_session="tmux-1",
            workspace_id=None,
            managed_session_id=None,
            subagent_id=None,
            parent_tab_id=None,
            last_viewed_at=None,
        )
        fake_mgr = TTYDManager.__new__(TTYDManager)
        fake_mgr.processes = {tab_id: proc}  # type: ignore[dict-item]
        fake_mgr._status_cache = {}
        fake_mgr._status_snapshots = {}
        monkeypatch.setattr(
            ttyd_manager_mod,
            "get_tab_native_runtime_snapshot",
            lambda _tid: SimpleNamespace(status=AgentRuntimeStatus.IDLE, detail="Idle"),
        )
        tab_st = await fake_mgr.get_tab_agent_status(tab_id, use_cache=False)
        assert tab_st is not None
        assert tab_st.last_reply_excerpt == f"Updated and pushed {mr_url}."
        assert tab_st.last_activity_at == t1
        assert tab_st.review_state == "mr_open"
        assert tab_st.review_label == "MR !1483"

        # Dispatcher tabs (e.g. Dot / Ted) that spawned a workspace-controlled task
        # with source.tab_id must NOT inherit task_review from their dispatched tasks!
        ted_tab_id = "ted-dispatcher-tab"
        ted_proc = SimpleNamespace(
            tab_id=ted_tab_id,
            name="Ted",
            agent_type=AgentType.CURSOR,
            session_kind=SessionKind.CHAT,
            cursor_transport="acp",
            tmux_session="tmux-ted",
            workspace_id=None,
            managed_session_id=None,
            subagent_id=None,
            parent_tab_id=None,
            last_viewed_at=None,
        )
        fake_mgr.processes[ted_tab_id] = ted_proc  # type: ignore[assignment]
        dispatched = manager.create_task(
            ws.id,
            WorkspaceTaskCreate(
                title="Background task dispatched from Ted",
                prompt="Run review",
                execution_control=TaskExecutionControl.WORKSPACE,
                source=TaskSourceReference(kind="chat", tab_id=ted_tab_id),
            ),
        )
        manager.tasks[dispatched.id] = dispatched.model_copy(
            update={"status": WorkspaceTaskStatus.REVIEW}
        )
        ted_st = await fake_mgr.get_tab_agent_status(ted_tab_id, use_cache=False)
        assert ted_st is not None
        assert ted_st.review_state is None
        assert ted_st.review_label is None

        # Once the MR transitions to state="merged", board overview naturally settles
        # the task to DONE with closed_reason="mr_merged" and clears the tab's review pill!
        mr_status_cache.seed(MRStateSnapshot(url=mr_url, state="merged", title="Split trace"))
        monkeypatch.setattr(
            ttyd_manager,
            "get_tab",
            lambda tid: SimpleNamespace(id=tid, archived=False),
        )
        overview = manager.get_board_overview()
        settled = manager.tasks[mr_task.id]
        assert settled.status == WorkspaceTaskStatus.DONE
        assert settled.closed_reason == "mr_merged"
        assert overview["task_lifecycles"][mr_task.id].reason == "mr_merged"

        tab_st_after = await fake_mgr.get_tab_agent_status(tab_id, use_cache=False)
        assert tab_st_after is not None
        assert tab_st_after.review_state is None
        assert tab_st_after.review_label is None

        # Idle top-level Chat sessions active within the last 4 hours project
        # review_state="recent_active" (with review_label=None), then settle to None after 4h.
        recent_tab_id = "recent-2h-tab"
        recent_store = AgentStreamStore("terminal-tabs", f"terminal-tab-{recent_tab_id}")
        await recent_store.append(
            AgentStreamEvent(
                stream_sequence=0,
                session_id=f"terminal-tab-{recent_tab_id}",
                tab_id=recent_tab_id,
                agent_type=AgentType.CURSOR,
                type=AgentStreamEventType.TURN_COMPLETED,
                run_epoch=1,
                turn_id="t-recent",
                payload={"status": "completed", "assistant_text": "Investigated slot transfer."},
                created_at=now_utc - timedelta(hours=2),
            )
        )
        fake_mgr.processes[recent_tab_id] = SimpleNamespace(  # type: ignore[assignment]
            tab_id=recent_tab_id,
            name="slot传输调研",
            agent_type=AgentType.CURSOR,
            session_kind=SessionKind.CHAT,
            cursor_transport="acp",
            tmux_session="tmux-recent",
            workspace_id=None,
            managed_session_id=None,
            subagent_id=None,
            parent_tab_id=None,
            last_viewed_at=now_utc,
        )
        recent_st = await fake_mgr.get_tab_agent_status(recent_tab_id, use_cache=False)
        assert recent_st is not None
        assert recent_st.review_state == "recent_active"
        assert recent_st.review_label is None

        # A freshly created Chat with zero turns yet falls back to created_at (<= 4h -> recent_active),
        # whereas subagent child tabs never project top-level review state.
        fresh_tab_id = "fresh-empty-tab"
        fake_mgr.processes[fresh_tab_id] = SimpleNamespace(  # type: ignore[assignment]
            tab_id=fresh_tab_id,
            name="new chat",
            agent_type=AgentType.CURSOR,
            session_kind=SessionKind.CHAT,
            cursor_transport="acp",
            tmux_session="tmux-fresh",
            workspace_id=None,
            managed_session_id=None,
            subagent_id=None,
            parent_tab_id=None,
            created_at=datetime.now() - timedelta(minutes=20),
            last_viewed_at=now_utc,
        )
        fresh_st = await fake_mgr.get_tab_agent_status(fresh_tab_id, use_cache=False)
        assert fresh_st is not None
        assert fresh_st.review_state == "recent_active"

        sub_tab_id = "recent-subagent-tab"
        fake_mgr.processes[sub_tab_id] = SimpleNamespace(  # type: ignore[assignment]
            tab_id=sub_tab_id,
            name="subagent worker",
            agent_type=AgentType.CURSOR,
            session_kind=SessionKind.CHAT,
            cursor_transport="acp",
            tmux_session="tmux-sub",
            workspace_id=None,
            managed_session_id=None,
            subagent_id="sub-1",
            parent_tab_id=recent_tab_id,
            created_at=datetime.now() - timedelta(minutes=5),
            last_viewed_at=now_utc,
        )
        sub_st = await fake_mgr.get_tab_agent_status(sub_tab_id, use_cache=False)
        assert sub_st is not None
        assert sub_st.review_state is None

        stale_tab_id = "stale-5h-tab"
        stale_store = AgentStreamStore("terminal-tabs", f"terminal-tab-{stale_tab_id}")
        await stale_store.append(
            AgentStreamEvent(
                stream_sequence=0,
                session_id=f"terminal-tab-{stale_tab_id}",
                tab_id=stale_tab_id,
                agent_type=AgentType.CURSOR,
                type=AgentStreamEventType.TURN_COMPLETED,
                run_epoch=1,
                turn_id="t-stale",
                payload={"status": "completed", "assistant_text": "Old finished Q&A."},
                created_at=now_utc - timedelta(hours=5),
            )
        )
        fake_mgr.processes[stale_tab_id] = SimpleNamespace(  # type: ignore[assignment]
            tab_id=stale_tab_id,
            name="h800预估",
            agent_type=AgentType.CURSOR,
            session_kind=SessionKind.CHAT,
            cursor_transport="acp",
            tmux_session="tmux-stale",
            workspace_id=None,
            managed_session_id=None,
            subagent_id=None,
            parent_tab_id=None,
            created_at=datetime.now() - timedelta(hours=5),
            last_viewed_at=now_utc,
        )
        stale_st = await fake_mgr.get_tab_agent_status(stale_tab_id, use_cache=False)
        assert stale_st is not None
        assert stale_st.review_state is None
        assert stale_st.review_label is None

        # Verify reference-only MR URLs in title/prompt do NOT prematurely complete unfinished work!
        ref_only_url = "https://[redacted]/[redacted]/merge_requests/3001"
        mr_status_cache.seed(MRStateSnapshot(url=ref_only_url, state="merged", title="Old MR"))
        diag_task, _ = await manager.register_task(
            ws.id,
            WorkspaceTaskCreate(
                title=f"Diagnose regression introduced by {ref_only_url}",
                prompt=f"Investigate why {ref_only_url} slowed prefill",
                execution_control=TaskExecutionControl.INITIATOR,
                request_key="ref-only-diag-1",
                reporter_key=KEY,
                source=TaskSourceReference(kind="chat", tab_id=tab_id),
            ),
            actor_key="actor-mr",
        )
        await manager.record_task_progress(
            ws.id,
            diag_task.id,
            TaskProgressRequest(
                call_id="diag-start-1",
                expected_execution_epoch=diag_task.execution_epoch,
                expected_progress_revision=diag_task.progress_revision,
                state=TaskProgressState.STARTED,
                summary=f"Reading logs around {ref_only_url}",
            ),
            KEY,
        )
        manager.get_board_overview()
        assert manager.tasks[diag_task.id].status == WorkspaceTaskStatus.WORKING

        # Verify Workspace-controlled tasks whose worker is actively WORKING are NOT closed mid-turn!
        now_local = datetime.now()
        worker = ManagedSession(
            id="ws-worker-1",
            workspace_id=ws.id,
            workspace_path=str(repo),
            tab_id="ws-worker-tab",
            tmux_session="tmux-ws-worker",
            title="Worker 1",
            role=WorkspaceSessionRole.ORCHESTRATOR,
            agent_type=AgentType.CLAUDE,
            session_kind=SessionKind.CHAT,
            status=ManagedSessionStatus.WORKING,
            runtime_status=AgentRuntimeStatus.WORKING,
            created_at=now_local,
            updated_at=now_local,
        )
        manager.sessions["ws-worker-1"] = worker
        ws_task = manager.create_task(
            ws.id,
            WorkspaceTaskCreate(
                title="Managed delivery task",
                prompt="Run post-merge checks",
                agent_type=AgentType.CLAUDE,
                session_kind=SessionKind.CHAT,
            ),
        )
        manager.tasks[ws_task.id] = ws_task.model_copy(
            update={"status": WorkspaceTaskStatus.WORKING, "session_id": "ws-worker-1"}
        )
        manager.reports["r-ws-1"] = AgentReport(
            id="r-ws-1",
            workspace_id=ws.id,
            task_id=ws_task.id,
            session_id="ws-worker-1",
            state=AgentReportState.COMPLETED,
            message="Delivered",
            artifact_refs=[mr_url],
            review_decision=ReviewDecision.SKIP,
            review_cycle=ws_task.review_cycle,
            created_at=now_local,
        )
        # While worker.runtime_status == WORKING -> stays WORKING
        manager.get_board_overview()
        assert manager.tasks[ws_task.id].status == WorkspaceTaskStatus.WORKING

        # Once worker returns to IDLE -> naturally settles to DONE (mr_merged)
        manager.sessions["ws-worker-1"] = worker.model_copy(
            update={
                "status": ManagedSessionStatus.IDLE,
                "runtime_status": AgentRuntimeStatus.IDLE,
            }
        )
        manager.get_board_overview()
        assert manager.tasks[ws_task.id].status == WorkspaceTaskStatus.DONE
        assert manager.tasks[ws_task.id].closed_reason == "mr_merged"
    finally:
        mr_status_cache.clear()


@pytest.mark.asyncio
async def test_mr_status_cache_nonblocking_refresh_and_error_backoff() -> None:
    calls: list[str] = []

    async def fake_runner(argv: list[str], **_kwargs: Any) -> Any:
        url = argv[-1]
        calls.append(url)
        if "fail" in url:
            raise ValueError("transient error")
        return {"data": {"merge_request": {"Status": "open", "Title": "Demo MR"}}}

    cache = MRStatusCache(runner=fake_runner, ttl_seconds=60.0, allow_in_tests=True)
    ok_url = "https://[redacted]/[redacted]/merge_requests/4164"
    fail_url = "https://[redacted]/[redacted]/merge_requests/9999_fail"
    try:
        # First observe returns immediately (0 blocking wait) and schedules background refresh
        assert cache.observe([ok_url, fail_url]) == {}
        await cache.wait_pending()
        assert len(calls) == 2
        snap = cache.observe([ok_url, fail_url])
        assert snap[ok_url].state == "open"
        assert fail_url not in snap
        # Subsequent observe within TTL/backoff does not re-spawn subprocesses
        await cache.wait_pending()
        assert len(calls) == 2
    finally:
        await cache.stop()
