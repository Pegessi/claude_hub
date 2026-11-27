"""Regression checks for workspace review convergence."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

from claude_hub.models import AgentReport, AgentReportState, WorkspaceTaskStatus
from claude_hub.services.workspace_manager.prompts.review import BLIND_REVIEW_RULES
from tests.test_review_delivery import delivery_env  # noqa: F401


def test_review_failure_cap_and_liveness(delivery_env, monkeypatch):
    manager, clock, _ = delivery_env
    task = manager.tasks["t"]
    reviewer = manager.sessions["reviewer"]
    for cycle in (1, 2, 3, 3):
        report = AgentReport(
            id=f"failure-{cycle}-{len(manager.reports)}",
            workspace_id="w",
            task_id="t",
            session_id=reviewer.id,
            state=AgentReportState.REVIEW_FAILED,
            message="P1: concrete defect",
            review_cycle=cycle,
            created_at=clock[0] + timedelta(seconds=cycle),
        )
        manager.reports[report.id] = report
    task = task.model_copy(
        update={
            "status": WorkspaceTaskStatus.REVIEW,
            "review_cycle": 3,
            "reviewed_cycle": 3,
            "review_completed_at": clock[0],
            "human_acceptance_requested_at": None,
        }
    )
    manager.tasks[task.id] = task
    continuation = AsyncMock()
    monkeypatch.setattr(manager, "continue_task", continuation)
    assert manager._implementation_review_failure_count(task.id) == 3
    asyncio.run(manager._handle_review_report(task, reviewer, report))
    continuation.assert_not_awaited()
    assert not asyncio.run(manager._liveness_facts(task)).resume_verdict


def test_blind_review_rules_cover_scope_and_convergence():
    for text in ("Input boundary", "Scope", "Severity", "Evidence", "Convergence", "P2"):
        assert text in BLIND_REVIEW_RULES


def test_subagent_review_verdicts_and_launch(delivery_env, monkeypatch):
    import json
    from types import SimpleNamespace

    from claude_hub.models.subagent import SubagentTurnStatus
    from claude_hub.services.cross_reviewer import CrossReviewCandidate
    from claude_hub.services.workspace_manager import subagent_review

    manager, clock, _ = delivery_env
    manager.tasks["t"] = manager.tasks["t"].model_copy(
        update={"review_session_id": None, "review_cycle": 1}
    )
    monkeypatch.setattr(
        subagent_review,
        "select_cross_reviewer_candidates",
        lambda *args, **kwargs: [CrossReviewCandidate(provider="codex")],
    )
    monkeypatch.setattr(manager, "_after_review_passed_effects", AsyncMock())
    trigger = AgentReport(
        id="worker-report",
        workspace_id="w",
        task_id="t",
        session_id="worker",
        state=AgentReportState.COMPLETED,
        message="done",
        created_at=clock[0],
    )

    async def run_case(verdict, findings, fail=False):
        task = manager.tasks["t"].model_copy(
            update={
                "status": WorkspaceTaskStatus.REVIEW,
                "review_completed_at": None,
                "reviewed_cycle": 0,
                "review_session_id": None,
                "human_acceptance_requested_at": None,
            }
        )
        manager.tasks["t"] = task
        result = {"verdict": verdict, "summary": "reviewed", "findings": findings}
        turn = SimpleNamespace(
            status=SubagentTurnStatus.COMPLETED,
            final_text="<task-review>" + json.dumps(result) + "</task-review>",
            turn_id="turn",
        )
        service = SimpleNamespace(
            create=AsyncMock(
                side_effect=RuntimeError("offline") if fail else None,
                return_value=(SimpleNamespace(id="review-id"), turn),
            )
        )
        monkeypatch.setattr(manager, "_get_live_subagent_service", lambda: service)
        await manager._execute_subagent_task_review(manager.workspaces["w"], task, trigger, {})
        result_task = manager.tasks["t"]
        report = list(
            r for r in manager.reports.values() if r.session_id == result_task.review_session_id
        )[-1]
        if not fail:
            request = service.create.await_args.args[0]
            assert (
                request.parent_tab_id is None
                and request.caller_tab_id is None
                and request.caller_subagent_id is None
            )
            assert request.keep is False and request.solo_mode is True
        return result_task, report

    task, report = asyncio.run(run_case("review_passed", []))
    assert task.human_acceptance_requested_at is not None
    assert report.session_id == task.review_session_id
    task, report = asyncio.run(
        run_case("review_failed", [{"severity": "P2", "summary": "suggestion"}])
    )
    assert report.state == AgentReportState.REVIEW_PASSED
    assert "suggestion" in report.message
    task, report = asyncio.run(run_case("review_failed", [], fail=True))
    assert report.state == AgentReportState.REVIEW_NEEDS_INPUT
    assert task.review_completed_at is not None


def test_subagent_request_rework_cap(delivery_env, monkeypatch):
    import json
    from types import SimpleNamespace

    from claude_hub.models.subagent import SubagentTurnStatus
    from claude_hub.services.cross_reviewer import CrossReviewCandidate
    from claude_hub.services.workspace_manager import subagent_review

    manager, clock, _ = delivery_env
    manager.tasks["t"] = manager.tasks["t"].model_copy(update={"review_session_id": None})
    monkeypatch.setattr(
        subagent_review,
        "select_cross_reviewer_candidates",
        lambda *args, **kwargs: [CrossReviewCandidate(provider="codex")],
    )
    turn = SimpleNamespace(
        status=SubagentTurnStatus.COMPLETED,
        turn_id="turn",
        final_text="<task-review>"
        + json.dumps(
            {
                "verdict": "review_failed",
                "summary": "defect",
                "findings": [{"severity": "P1", "file": "a.py:1", "summary": "broken"}],
            }
        )
        + "</task-review>",
    )
    service = SimpleNamespace(create=AsyncMock(return_value=(SimpleNamespace(id="review"), turn)))
    monkeypatch.setattr(manager, "_get_live_subagent_service", lambda: service)
    continuation = AsyncMock()
    monkeypatch.setattr(manager, "continue_task", continuation)
    trigger = AgentReport(
        id="worker-report",
        workspace_id="w",
        task_id="t",
        session_id="worker",
        state=AgentReportState.COMPLETED,
        message="done",
        created_at=clock[0],
    )

    async def run():
        for cycle in (1, 2, 3):
            manager.tasks["t"] = manager.tasks["t"].model_copy(
                update={
                    "status": WorkspaceTaskStatus.WORKING,
                    "review_cycle": cycle,
                    "review_completed_at": None,
                    "review_session_id": None,
                }
            )
            await manager._request_task_review_workspace_report(manager.tasks["t"], trigger)
            assert manager.tasks["t"].review_session_id is None
            await manager._subagent_review_tasks["t"]
            assert continuation.await_count == min(cycle, 2)
        assert not (await manager._liveness_facts(manager.tasks["t"])).resume_verdict

    asyncio.run(run())


def test_running_subagent_review_keeps_liveness_and_discards_stale_result(
    delivery_env, monkeypatch
):
    from types import SimpleNamespace

    from claude_hub.models.subagent import SubagentTurnStatus
    from claude_hub.services.cross_reviewer import CrossReviewCandidate
    from claude_hub.services.workspace_manager import subagent_review

    manager, clock, _ = delivery_env
    manager.tasks["t"] = manager.tasks["t"].model_copy(
        update={"review_session_id": None, "review_cycle": 1}
    )
    monkeypatch.setattr(
        subagent_review,
        "select_cross_reviewer_candidates",
        lambda *args, **kwargs: [CrossReviewCandidate(provider="codex")],
    )
    trigger = AgentReport(
        id="worker-report",
        workspace_id="w",
        task_id="t",
        session_id="worker",
        state=AgentReportState.COMPLETED,
        message="done",
        created_at=clock[0],
    )

    async def run():
        started, finish = asyncio.Event(), asyncio.Event()

        async def create(_request):
            started.set()
            await finish.wait()
            return SimpleNamespace(id="review"), SimpleNamespace(
                status=SubagentTurnStatus.COMPLETED,
                final_text='<task-review>{"verdict":"review_passed","summary":"ok","findings":[]}</task-review>',
            )

        monkeypatch.setattr(
            manager, "_get_live_subagent_service", lambda: SimpleNamespace(create=create)
        )
        await manager._request_task_review_workspace_report(manager.tasks["t"], trigger)
        review = manager._subagent_review_tasks["t"]
        await started.wait()
        facts = await manager._liveness_facts(manager.tasks["t"])
        assert facts.active and not facts.dead
        manager.tasks["t"] = manager.tasks["t"].model_copy(
            update={"status": WorkspaceTaskStatus.WORKING, "review_cycle": 2}
        )
        finish.set()
        await review
        assert not manager.reports
        assert manager.tasks["t"].status == WorkspaceTaskStatus.WORKING

    asyncio.run(run())
