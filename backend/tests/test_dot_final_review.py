"""Dot notifications for final independent review and human acceptance."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from claude_hub.models import (
    AgentReport,
    AgentReportState,
    AgentType,
    WorkspaceTask,
    WorkspaceTaskStatus,
)
from claude_hub.services import workspace_manager, workspace_state_policy
from claude_hub.services.dot import DotService, DotStore
from claude_hub.services.dot.context import TURN_PACK_CHARS, build_turn_pack
from claude_hub.services.dot.prompts import WAKE_MESSAGE

NOW = datetime(2026, 10, 18, 19, 23, 43)


def _task(**changes: Any) -> WorkspaceTask:
    task = WorkspaceTask(
        id="task-1",
        workspace_id="ws-1",
        title="Assess text embedding bypass",
        prompt="Report the coverage and residual risks.",
        agent_type=AgentType.CODEX,
        status=WorkspaceTaskStatus.REVIEW,
        session_id="worker-1",
        review_session_id="reviewer-1",
        review_cycle=2,
        reviewed_cycle=1,
        review_requested_at=NOW - timedelta(minutes=2),
        created_at=NOW - timedelta(hours=1),
        updated_at=NOW,
    )
    update = workspace_state_policy.compute_reviewer_verdict_task_update(
        report_state=AgentReportState.REVIEW_PASSED,
        reviewer_session_id="reviewer-1",
        now=NOW,
        report_review_cycle=2,
        existing_review_completed_at=None,
        existing_reviewed_at=None,
        existing_human_acceptance_requested_at=None,
        preserve_existing_review_completed_at=True,
        preserve_existing_reviewed_at=True,
        preserve_existing_human_acceptance_requested_at=True,
    )
    return task.model_copy(update={**update, **changes})


def _report(
    report_id: str = "review-pass",
    *,
    state: AgentReportState = AgentReportState.REVIEW_PASSED,
    **changes: Any,
) -> AgentReport:
    report = AgentReport(
        id=report_id,
        workspace_id="ws-1",
        task_id="task-1",
        session_id="reviewer-1",
        state=state,
        message="PARTIAL: text-only bypass works; multimodal admission still needs a fix.",
        validation="All six acceptance criteria passed independent review.",
        risks="Static evidence only; live deployment was not verified.",
        review_cycle=2,
        created_at=NOW,
    )
    return report.model_copy(update=changes)


@pytest.fixture()
def dot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DotService:
    store = DotStore(tmp_path / "dot")
    store.ensure()
    store.update_state(tab_id="dot-tab")
    monkeypatch.setattr(workspace_manager, "tasks", {})
    return DotService(store)


def _track(
    dot: DotService,
    monkeypatch: pytest.MonkeyPatch,
    task: WorkspaceTask,
    reports: list[AgentReport],
    *,
    watched: bool = True,
    **fields: Any,
) -> None:
    workspace_manager.tasks[task.id] = task
    monkeypatch.setattr(workspace_manager, "reports_for_task", lambda _ws, _id: reports)
    dot.store.upsert_work(
        task.id,
        workspace_id=task.workspace_id,
        title=task.title,
        watched=watched,
        status="review",
        **fields,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("watched", [True, False], ids=["watched", "delegated"])
async def test_final_pass_wakes_with_verdict_after_worker_event_was_consumed(
    dot: DotService,
    monkeypatch: pytest.MonkeyPatch,
    watched: bool,
) -> None:
    worker = _report(
        "worker-result",
        state=AgentReportState.READY_FOR_REVIEW,
        session_id="worker-1",
        message="Worker result: bypass covers pure text only.",
        artifact_refs=["docs/result.md"],
        created_at=NOW - timedelta(minutes=2),
    )
    progress = _report(
        "worker-progress",
        state=AgentReportState.WORKING,
        session_id="worker-1",
        message="Artifact is still in final independent review.",
        created_at=NOW - timedelta(minutes=1),
    )
    reports = [worker, progress, _report()]
    _track(
        dot,
        monkeypatch,
        _task(),
        reports,
        watched=watched,
        last_report_id=progress.id,
        announced_report_id=worker.id,
        announced_status="review",
        review_due=False,
    )
    before = workspace_manager.tasks["task-1"].model_dump()
    wake = AsyncMock(return_value=True)
    monkeypatch.setattr(dot, "wake", wake)
    monkeypatch.setattr(dot, "_wake_in_flight", lambda: False)
    await dot._wake_if_due()
    wake.assert_awaited_once()
    events = dot.store.list_inbox()
    assert len(events) == 1
    assert events[0]["kind"] == "task_review_passed"
    assert "PARTIAL" in events[0]["text"]
    assert "awaiting human acceptance" in events[0]["text"]
    card = dot.store.get_card(events[0]["card_id"])
    assert card is not None
    assert "Worker result:" in card.body and "docs/result.md" in card.body
    assert "Final independent review" in card.body and reports[-1].message in card.body
    assert "All six acceptance criteria" in card.body
    assert "still in final independent review" not in card.body
    assert dot.store.pending_reviews() == [], "notification does not take review ownership"
    assert workspace_manager.tasks["task-1"].model_dump() == before

    plan = dot._prepare_turn(WAKE_MESSAGE, None, feishu_bound=True)
    assert events[0]["id"] in plan.delivered_event_ids
    assert "PARTIAL" in plan.turn_pack
    assert "Consider a brief update if useful" in plan.turn_pack
    assert "[quiet] is fine if there is nothing new worth reporting" in plan.turn_pack
    assert "Task acceptance still requires user authorization" in plan.turn_pack
    assert "do not reply [quiet]" not in plan.turn_pack
    assert (
        "Consider `claude-hub dot notify` if an update should reach the user now" in plan.turn_pack
    )
    assert len(plan.turn_pack) <= TURN_PACK_CHARS
    dot.mark_delivered(plan.delivered_event_ids)
    assert dot.store.list_inbox() == []
    assert await dot.check_work() == 0

    # Restart and late audit/progress reports must not re-announce this review round.
    reports.extend(
        [
            _report("late-echo", created_at=NOW + timedelta(minutes=1), message="Duplicate echo"),
            _report(
                "late-progress",
                state=AgentReportState.WORKING,
                session_id="worker-1",
                created_at=NOW + timedelta(minutes=2),
            ),
        ]
    )
    restarted = DotService(DotStore(dot.store.home))
    assert await restarted.check_work() == 0
    assert len(restarted.store.list_inbox(unread_only=False)) == 1
    assert restarted.store.get_card(card.id) == card


@pytest.mark.asyncio
async def test_same_poll_result_and_pass_produces_one_complete_event(
    dot: DotService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _report(
        "result",
        state=AgentReportState.READY_FOR_REVIEW,
        session_id="worker-1",
        created_at=NOW - timedelta(minutes=2),
    )
    _track(dot, monkeypatch, _task(), [worker, _report()], watched=False)
    dot.store.upsert_work("task-1", status="working")
    assert await dot.check_work() == 1
    events = dot.store.list_inbox()
    assert [e["kind"] for e in events] == ["task_review_passed"]
    assert len(dot.store.list_cards("work")) == 1
    assert len(dot.store.pending_reviews()) == 1
    assert (
        "Final independent review"
        in build_turn_pack(
            dot.store,
            None,
            WAKE_MESSAGE,
            first_turn=False,
            session_turn=1,
        ).text
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_changes,report_changes",
    [
        ({"human_acceptance_requested_at": None}, {}),
        ({"review_completed_at": None}, {}),
        ({"review_requested_at": None}, {}),
        ({"human_accepted_at": NOW}, {}),
        ({"status": WorkspaceTaskStatus.DONE}, {}),
        ({"status": WorkspaceTaskStatus.WORKING, "review_cycle": 3}, {}),
        ({"reviewed_cycle": 1}, {}),
        ({}, {"review_cycle": 1}),
        ({}, {"state": AgentReportState.REVIEW_FAILED}),
        ({}, {"state": AgentReportState.REVIEW_NEEDS_INPUT}),
        ({}, {"session_id": "old-reviewer"}),
        ({}, {"created_at": NOW + timedelta(seconds=1)}),
        ({}, {"created_at": NOW - timedelta(minutes=3)}),
    ],
    ids=[
        "plan-pass",
        "unfinished",
        "unrequested",
        "accepted",
        "done",
        "reopened",
        "unsealed",
        "stale-cycle",
        "failed",
        "needs-input",
        "old-reviewer",
        "late-echo",
        "before-request",
    ],
)
async def test_nonfinal_or_stale_reports_do_not_announce_acceptance(
    dot: DotService,
    monkeypatch: pytest.MonkeyPatch,
    task_changes: dict[str, Any],
    report_changes: dict[str, Any],
) -> None:
    _track(dot, monkeypatch, _task(**task_changes), [_report(**report_changes)])
    await dot.check_work()
    assert not any(e["kind"] == "task_review_passed" for e in dot.store.list_inbox())


@pytest.mark.asyncio
async def test_late_same_cycle_reviewer_echo_does_not_replace_applied_verdict(
    dot: DotService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = [
        _report(),
        _report("echo", message="Outdated plan approved", created_at=NOW + timedelta(seconds=1)),
    ]
    _track(dot, monkeypatch, _task(), reports)
    assert await dot.check_work() == 1
    event = dot.store.list_inbox()[0]
    assert "PARTIAL" in event["text"] and "Outdated plan" not in event["text"]


@pytest.mark.asyncio
async def test_new_review_cycle_rearms_but_watch_does_not_replay_history(
    dot: DotService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = [_report()]
    task = _task()
    workspace_manager.tasks[task.id] = task
    monkeypatch.setattr(workspace_manager, "reports_for_task", lambda _ws, _id: reports)
    await dot.watch(task.id)
    assert await dot.check_work() == 0
    assert dot.store.list_inbox() == []
    task = _task(
        review_cycle=3,
        reviewed_cycle=3,
        review_requested_at=NOW + timedelta(minutes=1),
        review_completed_at=NOW + timedelta(minutes=2),
        human_acceptance_requested_at=NOW + timedelta(minutes=2),
    )
    workspace_manager.tasks[task.id] = task
    reports.append(_report("next-pass", review_cycle=3, created_at=NOW + timedelta(minutes=2)))
    assert await dot.check_work() == 1
    assert dot.store.list_inbox()[0]["key"] == "task_review_passed:task-1:3"
    assert await dot.check_work() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [False, True], ids=["unread", "delivered"])
async def test_inbox_write_before_work_marker_is_idempotent_on_retry(
    dot: DotService,
    monkeypatch: pytest.MonkeyPatch,
    delivered: bool,
) -> None:
    _track(dot, monkeypatch, _task(), [_report()])
    save = dot.store.upsert_work

    def fail_save(*args: Any, **kwargs: Any) -> Any:
        raise OSError("disk failure after inbox write")

    monkeypatch.setattr(dot.store, "upsert_work", fail_save)
    await dot.check_work()
    assert len(dot.store.list_inbox()) == 1
    if delivered:
        dot.store.mark_inbox_read()
    monkeypatch.setattr(dot.store, "upsert_work", save)
    await dot.check_work()
    assert len(dot.store.list_inbox(unread_only=False)) == 1
    assert len(dot.store.list_cards("work")) == 1
    assert len(dot.store.list_inbox()) == (0 if delivered else 1)
    assert await dot.check_work() == 0


@pytest.mark.asyncio
async def test_unwatch_suppresses_final_review_notification(
    dot: DotService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _track(dot, monkeypatch, _task(), [_report()])
    await dot.unwatch("task-1")
    assert await dot.check_work() == 0
    assert dot.store.list_inbox() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        AgentReportState.READY_FOR_REVIEW,
        AgentReportState.COMPLETED,
        AgentReportState.REVIEW_NEEDS_INPUT,
        AgentReportState.BLOCKED,
        AgentReportState.NEEDS_INPUT,
    ],
)
@pytest.mark.parametrize("before_poll", [True, False], ids=["before-notice", "after-notice"])
async def test_late_returns_and_questions_cannot_replace_reviewed_evidence(
    dot: DotService,
    monkeypatch: pytest.MonkeyPatch,
    state: AgentReportState,
    before_poll: bool,
) -> None:
    worker = _report(
        "reviewed-result",
        state=AgentReportState.READY_FOR_REVIEW,
        session_id="worker-1",
        created_at=NOW - timedelta(minutes=2),
        message="Reviewed worker result.",
        artifact_refs=["reviewed.md"],
    )
    late = _report(
        "late",
        state=state,
        session_id="reviewer-1" if state == AgentReportState.REVIEW_NEEDS_INPUT else "worker-1",
        created_at=NOW + timedelta(seconds=1),
        message="UNREVIEWED late return or question",
        artifact_refs=["unreviewed.md"],
    )
    reports = [worker, _report()]
    _track(dot, monkeypatch, _task(), reports)
    if before_poll:
        reports.append(late)
    assert await dot.check_work() == 1
    if not before_poll:
        reports.append(late)
        assert await dot.check_work() == 0
    events = dot.store.list_inbox()
    assert [e["kind"] for e in events] == ["task_review_passed"]
    card = dot.store.get_card(events[0]["card_id"])
    assert card is not None
    assert "reviewed.md" in card.body and "Reviewed worker result." in card.body
    assert "UNREVIEWED" not in card.body and "unreviewed.md" not in card.body
