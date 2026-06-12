from datetime import datetime, timedelta

import pytest

from claude_hub.models import (
    AgentReportState,
    AgentRuntimeStatus,
    AutonomousRunPhase,
    EvaluationDecision,
    ManagedSessionStatus,
    ReviewDecision,
    ReviewProfile,
    WorkspaceSessionRole,
    WorkspaceTaskMode,
    WorkspaceTaskStatus,
)
from claude_hub.services import workspace_state_policy as policy


@pytest.mark.parametrize(
    ("runtime_status", "expected"),
    [
        (AgentRuntimeStatus.IDLE, ManagedSessionStatus.IDLE),
        (AgentRuntimeStatus.WORKING, ManagedSessionStatus.WORKING),
        (AgentRuntimeStatus.ATTENTION, ManagedSessionStatus.NEEDS_INPUT),
        (AgentRuntimeStatus.OFFLINE, ManagedSessionStatus.STOPPED),
    ],
)
def test_managed_status_from_runtime(
    runtime_status: AgentRuntimeStatus,
    expected: ManagedSessionStatus,
) -> None:
    assert policy.managed_status_from_runtime(runtime_status) == expected


@pytest.mark.parametrize(
    ("role", "state", "expected"),
    [
        (
            WorkspaceSessionRole.ORCHESTRATOR,
            AgentReportState.READY_FOR_REVIEW,
            ManagedSessionStatus.IDLE,
        ),
        (
            WorkspaceSessionRole.ORCHESTRATOR,
            AgentReportState.COMPLETED,
            ManagedSessionStatus.IDLE,
        ),
        (
            WorkspaceSessionRole.ORCHESTRATOR,
            AgentReportState.BLOCKED,
            ManagedSessionStatus.NEEDS_INPUT,
        ),
        (
            WorkspaceSessionRole.REVIEWER,
            AgentReportState.REVIEW_STARTED,
            ManagedSessionStatus.WORKING,
        ),
        (
            WorkspaceSessionRole.REVIEWER,
            AgentReportState.REVIEW_PASSED,
            ManagedSessionStatus.IDLE,
        ),
        (
            WorkspaceSessionRole.REVIEWER,
            AgentReportState.REVIEW_NEEDS_INPUT,
            ManagedSessionStatus.NEEDS_INPUT,
        ),
        (
            WorkspaceSessionRole.WORKER,
            AgentReportState.COMPLETED,
            ManagedSessionStatus.DONE,
        ),
    ],
)
def test_managed_status_from_report(
    role: WorkspaceSessionRole,
    state: AgentReportState,
    expected: ManagedSessionStatus,
) -> None:
    assert (
        policy.managed_status_from_report(
            state,
            role,
            fallback=ManagedSessionStatus.STOPPED,
        )
        == expected
    )


def test_managed_status_from_report_uses_fallback_for_unmapped_reviewer_state() -> None:
    assert (
        policy.managed_status_from_report(
            AgentReportState.REVIEW_FAILED,
            WorkspaceSessionRole.ORCHESTRATOR,
            fallback=ManagedSessionStatus.IDLE,
        )
        == ManagedSessionStatus.IDLE
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (AgentReportState.STARTED, AgentRuntimeStatus.WORKING),
        (AgentReportState.WORKING, AgentRuntimeStatus.WORKING),
        (AgentReportState.READY_FOR_REVIEW, AgentRuntimeStatus.IDLE),
        (AgentReportState.COMPLETED, AgentRuntimeStatus.IDLE),
        (AgentReportState.BLOCKED, AgentRuntimeStatus.ATTENTION),
        (AgentReportState.NEEDS_INPUT, AgentRuntimeStatus.ATTENTION),
        (AgentReportState.REVIEW_STARTED, AgentRuntimeStatus.WORKING),
        (AgentReportState.REVIEW_PASSED, AgentRuntimeStatus.IDLE),
        (AgentReportState.REVIEW_FAILED, AgentRuntimeStatus.IDLE),
        (AgentReportState.REVIEW_NEEDS_INPUT, AgentRuntimeStatus.ATTENTION),
    ],
)
def test_runtime_status_from_report(
    state: AgentReportState,
    expected: AgentRuntimeStatus,
) -> None:
    assert policy.runtime_status_from_report(state, AgentRuntimeStatus.OFFLINE) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (AgentReportState.STARTED, WorkspaceTaskStatus.WORKING),
        (AgentReportState.WORKING, WorkspaceTaskStatus.WORKING),
        (AgentReportState.BLOCKED, WorkspaceTaskStatus.WORKING),
        (AgentReportState.NEEDS_INPUT, WorkspaceTaskStatus.WORKING),
        (AgentReportState.REVIEW_STARTED, WorkspaceTaskStatus.WORKING),
        (AgentReportState.READY_FOR_REVIEW, WorkspaceTaskStatus.REVIEW),
        (AgentReportState.COMPLETED, WorkspaceTaskStatus.REVIEW),
        (AgentReportState.REVIEW_PASSED, WorkspaceTaskStatus.REVIEW),
        (AgentReportState.REVIEW_FAILED, WorkspaceTaskStatus.REVIEW),
        (AgentReportState.REVIEW_NEEDS_INPUT, WorkspaceTaskStatus.REVIEW),
    ],
)
def test_task_status_from_report(
    state: AgentReportState,
    expected: WorkspaceTaskStatus,
) -> None:
    assert policy.task_status_from_report(state) == expected


def test_completion_evidence_gaps_only_apply_to_completion_style_reports() -> None:
    assert (
        policy.completion_evidence_gaps(
            AgentReportState.WORKING,
            has_goal_packet=False,
            has_acceptance_check=False,
        )
        == []
    )
    assert policy.completion_evidence_gaps(
        AgentReportState.COMPLETED,
        has_goal_packet=False,
        has_acceptance_check=False,
    ) == ["stored Goal Packet", "acceptance_check evidence"]
    assert (
        policy.completion_evidence_gaps(
            AgentReportState.READY_FOR_REVIEW,
            has_goal_packet=True,
            has_acceptance_check=True,
        )
        == []
    )


def test_review_gate_states_are_explicit() -> None:
    assert policy.is_review_gate_state(AgentReportState.READY_FOR_REVIEW)
    assert policy.is_review_gate_state(AgentReportState.COMPLETED)
    assert policy.is_review_gate_state(AgentReportState.BLOCKED)
    assert policy.is_review_gate_state(AgentReportState.NEEDS_INPUT)
    assert not policy.is_review_gate_state(AgentReportState.WORKING)
    assert not policy.is_review_gate_state(AgentReportState.REVIEW_STARTED)


@pytest.mark.parametrize(
    ("changed_files", "risk_level"),
    [
        ([], "low"),
        (["backend/claude_hub/config.py"], "trivial"),
    ],
)
def test_can_skip_task_review_allows_low_risk_completion(
    changed_files: list[str],
    risk_level: str,
) -> None:
    context = policy.ReviewSkipContext(
        report_state=AgentReportState.COMPLETED,
        evidence_gaps=[],
        changed_files=changed_files,
        risk_level=risk_level,
        latest_review_state=None,
        workspace_has_tracked_changes=False,
    )

    assert policy.can_skip_task_review(context)


@pytest.mark.parametrize(
    "context",
    [
        policy.ReviewSkipContext(
            report_state=AgentReportState.READY_FOR_REVIEW,
            evidence_gaps=[],
            changed_files=[],
            risk_level="low",
            latest_review_state=None,
            workspace_has_tracked_changes=False,
        ),
        policy.ReviewSkipContext(
            report_state=AgentReportState.COMPLETED,
            evidence_gaps=["stored Goal Packet"],
            changed_files=[],
            risk_level="low",
            latest_review_state=None,
            workspace_has_tracked_changes=False,
        ),
        policy.ReviewSkipContext(
            report_state=AgentReportState.COMPLETED,
            evidence_gaps=[],
            changed_files=["backend/file.py"],
            risk_level="low",
            latest_review_state=None,
            workspace_has_tracked_changes=False,
        ),
        policy.ReviewSkipContext(
            report_state=AgentReportState.COMPLETED,
            evidence_gaps=[],
            changed_files=[],
            risk_level="medium",
            latest_review_state=None,
            workspace_has_tracked_changes=False,
        ),
        policy.ReviewSkipContext(
            report_state=AgentReportState.COMPLETED,
            evidence_gaps=[],
            changed_files=[],
            risk_level="low",
            latest_review_state=AgentReportState.REVIEW_FAILED,
            workspace_has_tracked_changes=False,
        ),
        policy.ReviewSkipContext(
            report_state=AgentReportState.COMPLETED,
            evidence_gaps=[],
            changed_files=[],
            risk_level="low",
            latest_review_state=None,
            workspace_has_tracked_changes=True,
        ),
    ],
)
def test_can_skip_task_review_rejects_forced_review_conditions(
    context: policy.ReviewSkipContext,
) -> None:
    assert not policy.can_skip_task_review(context)


@pytest.mark.parametrize(
    ("state", "decision", "can_skip", "expected"),
    [
        (AgentReportState.COMPLETED, ReviewDecision.REQUEST, False, True),
        (AgentReportState.READY_FOR_REVIEW, ReviewDecision.SKIP, True, True),
        (AgentReportState.BLOCKED, ReviewDecision.SKIP, True, True),
        (AgentReportState.NEEDS_INPUT, ReviewDecision.SKIP, True, True),
        (AgentReportState.COMPLETED, ReviewDecision.AUTO, False, True),
        (AgentReportState.COMPLETED, ReviewDecision.SKIP, True, False),
        (AgentReportState.COMPLETED, ReviewDecision.SKIP, False, True),
    ],
)
def test_should_request_task_review(
    state: AgentReportState,
    decision: ReviewDecision,
    can_skip: bool,
    expected: bool,
) -> None:
    assert (
        policy.should_request_task_review(
            trigger_kind="agent_report",
            report_state=state,
            review_decision=decision,
            can_skip_review=can_skip,
        )
        is expected
    )


def test_non_agent_report_triggers_request_review() -> None:
    assert policy.should_request_task_review(
        trigger_kind="manual",
        report_state=AgentReportState.COMPLETED,
        review_decision=ReviewDecision.SKIP,
        can_skip_review=True,
    )


def test_infer_review_profiles_combines_task_and_evidence_lenses() -> None:
    profiles = policy.infer_review_profiles(
        policy.ReviewProfileContext(
            task_mode=WorkspaceTaskMode.AUTONOMOUS,
            report_state=AgentReportState.COMPLETED,
            title="Ship wallpaper",
            prompt="Generate image and send to Feishu.",
            changed_files=["frontend/src/App.vue"],
            validation="Playwright screenshot passed.",
            require_artifact_review=True,
        )
    )

    assert profiles == [
        ReviewProfile.GENERAL,
        ReviewProfile.CODE,
        ReviewProfile.ARTIFACT,
        ReviewProfile.UI,
        ReviewProfile.DELIVERY,
    ]


def test_infer_review_profiles_honors_explicit_boundary_profile() -> None:
    profiles = policy.infer_review_profiles(
        policy.ReviewProfileContext(
            task_mode=WorkspaceTaskMode.DIRECT,
            report_state=AgentReportState.COMPLETED,
            explicit_profiles=[ReviewProfile.BOUNDARY],
            message="Used production credential for external delivery.",
        )
    )

    assert profiles == [
        ReviewProfile.BOUNDARY,
        ReviewProfile.GENERAL,
        ReviewProfile.DELIVERY,
    ]


def test_autonomous_worker_report_moves_to_evaluation() -> None:
    assert (
        policy.autonomous_phase_after_worker_report(AgentReportState.COMPLETED)
        == AutonomousRunPhase.EVALUATING
    )
    assert (
        policy.autonomous_task_status_from_phase(AutonomousRunPhase.EVALUATING)
        == WorkspaceTaskStatus.WORKING
    )


def test_autonomous_evaluation_decision_respects_iteration_budget() -> None:
    assert (
        policy.autonomous_phase_from_evaluation_decision(
            decision=EvaluationDecision.REVISE,
            current_iteration=1,
            max_iterations=3,
        )
        == AutonomousRunPhase.REVISING
    )
    assert (
        policy.autonomous_phase_from_evaluation_decision(
            decision=EvaluationDecision.REVISE,
            current_iteration=3,
            max_iterations=3,
        )
        == AutonomousRunPhase.EXHAUSTED
    )
    assert (
        policy.autonomous_phase_from_evaluation_decision(
            decision=EvaluationDecision.PASS,
            current_iteration=3,
            max_iterations=3,
        )
        == AutonomousRunPhase.PASSED
    )


def test_auto_continue_ignores_stale_interruption_before_latest_prompt() -> None:
    output = "\n".join(
        [
            "API Error: 400 unknown error",
            "",
            "\u203a please continue",
            "",
            "no new instruction",
            "",
            "\u276f ",
        ]
    )

    assert policy.auto_continue_interruption_reason(output) is None


def test_auto_continue_detects_current_interruption_and_completion_segments() -> None:
    interrupted = "\n".join(
        [
            "\u203a New workspace task assigned",
            "",
            "Command failed",
            "API Error: 400 unknown error",
            "",
            "\u276f ",
        ]
    )
    completed = "\n".join(
        [
            "\u203a New workspace task assigned",
            "",
            "Implemented the fix.",
            "Validation: tests passed.",
            "Risks: no known risk.",
            "",
            "\u276f ",
        ]
    )

    assert policy.auto_continue_interruption_reason(interrupted) == "api error"
    assert policy.auto_continue_completion_reason(completed) == "validation:"


def test_auto_continue_output_looks_busy() -> None:
    assert policy.auto_continue_output_looks_busy("running...")
    assert policy.auto_continue_output_looks_busy("press esc to interrupt")
    assert not policy.auto_continue_output_looks_busy("Validation: passed\n\u276f ")


# --------------------------------------------------------------------------
# Reviewer verdict predicates.
# --------------------------------------------------------------------------

_T0 = datetime(2026, 6, 12, 12, 0, 0)
_BEFORE = _T0 - timedelta(minutes=5)
_AFTER = _T0 + timedelta(minutes=5)


@pytest.mark.parametrize(
    ("requested", "completed", "expected"),
    [
        (_T0, None, True),
        (None, None, False),
        (_T0, _T0, False),
        (None, _T0, False),
    ],
)
def test_review_in_flight(
    requested: datetime | None,
    completed: datetime | None,
    expected: bool,
) -> None:
    assert policy.review_in_flight(requested, completed) is expected


@pytest.mark.parametrize(
    ("completed", "report_created", "expected"),
    [
        (_AFTER, _T0, True),
        (_T0, _T0, True),
        (_BEFORE, _T0, False),
        (None, _T0, False),
    ],
)
def test_reviewer_verdict_already_applied(
    completed: datetime | None,
    report_created: datetime,
    expected: bool,
) -> None:
    assert policy.reviewer_verdict_already_applied(completed, report_created) is expected


@pytest.mark.parametrize(
    ("requested", "completed", "report_created", "expected"),
    [
        # Review in flight (requested, no verdict) -> actionable.
        (_T0, None, _T0, True),
        # Verdict already recorded at/after report -> actionable (idempotent).
        (None, _AFTER, _T0, True),
        # Stale verdict from a prior cycle, task reopened -> not actionable.
        (None, _BEFORE, _T0, False),
        (None, None, _T0, False),
    ],
)
def test_reviewer_verdict_actionable(
    requested: datetime | None,
    completed: datetime | None,
    report_created: datetime,
    expected: bool,
) -> None:
    assert policy.reviewer_verdict_actionable(requested, completed, report_created) is expected


@pytest.mark.parametrize(
    ("status", "completed", "now", "expected"),
    [
        (WorkspaceTaskStatus.REVIEW, _T0, _AFTER, True),
        (WorkspaceTaskStatus.REVIEW, _T0, _T0, True),
        (WorkspaceTaskStatus.REVIEW, _T0, _BEFORE, False),
        (WorkspaceTaskStatus.REVIEW, None, _T0, False),
        (WorkspaceTaskStatus.WORKING, _T0, _AFTER, False),
    ],
)
def test_reviewer_verdict_still_authoritative(
    status: WorkspaceTaskStatus,
    completed: datetime | None,
    now: datetime,
    expected: bool,
) -> None:
    assert policy.reviewer_verdict_still_authoritative(status, completed, now) is expected


@pytest.mark.parametrize(
    ("status", "latest_state", "latest_created", "completed", "report_created", "expected"),
    [
        (
            WorkspaceTaskStatus.REVIEW,
            AgentReportState.REVIEW_PASSED,
            _BEFORE,
            _T0,
            _T0,
            True,
        ),
        (
            WorkspaceTaskStatus.REVIEW,
            AgentReportState.REVIEW_FAILED,
            _T0,
            _T0,
            _T0,
            True,
        ),
        # latest review newer than this report -> not terminal for it.
        (
            WorkspaceTaskStatus.REVIEW,
            AgentReportState.REVIEW_PASSED,
            _AFTER,
            _T0,
            _T0,
            False,
        ),
        # Non-terminal latest state.
        (
            WorkspaceTaskStatus.REVIEW,
            AgentReportState.REVIEW_STARTED,
            _BEFORE,
            _T0,
            _T0,
            False,
        ),
        # No latest review state.
        (WorkspaceTaskStatus.REVIEW, None, None, _T0, _T0, False),
        # Not in REVIEW status.
        (
            WorkspaceTaskStatus.WORKING,
            AgentReportState.REVIEW_PASSED,
            _BEFORE,
            _T0,
            _T0,
            False,
        ),
        # No recorded verdict.
        (
            WorkspaceTaskStatus.REVIEW,
            AgentReportState.REVIEW_PASSED,
            _BEFORE,
            None,
            _T0,
            False,
        ),
    ],
)
def test_review_verdict_terminal(
    status: WorkspaceTaskStatus,
    latest_state: AgentReportState | None,
    latest_created: datetime | None,
    completed: datetime | None,
    report_created: datetime,
    expected: bool,
) -> None:
    assert (
        policy.review_verdict_terminal(
            status,
            latest_state,
            latest_created,
            completed,
            report_created,
        )
        is expected
    )


def test_compute_reviewer_verdict_task_update_excludes_mixin_fields() -> None:
    update = policy.compute_reviewer_verdict_task_update(
        report_state=AgentReportState.REVIEW_PASSED,
        reviewer_session_id="rev-1",
        now=_T0,
        existing_review_completed_at=None,
        existing_reviewed_at=None,
        existing_human_acceptance_requested_at=None,
        preserve_existing_review_completed_at=False,
        preserve_existing_reviewed_at=False,
        preserve_existing_human_acceptance_requested_at=False,
    )
    assert "goal_packet" not in update
    assert "autonomous_run" not in update
    assert update["status"] == WorkspaceTaskStatus.REVIEW
    assert update["review_session_id"] == "rev-1"
    assert update["review_skipped_at"] is None
    assert update["review_skip_reason"] is None
    assert update["completed_at"] is None
    assert update["human_accepted_at"] is None
    assert update["updated_at"] == _T0


def test_compute_reviewer_verdict_task_update_fast_path_flags() -> None:
    """Fast-path: completed=existing-or-now, reviewed=now, human=existing-or-now."""

    update = policy.compute_reviewer_verdict_task_update(
        report_state=AgentReportState.REVIEW_PASSED,
        reviewer_session_id="rev-1",
        now=_T0,
        existing_review_completed_at=_BEFORE,
        existing_reviewed_at=_BEFORE,
        existing_human_acceptance_requested_at=_BEFORE,
        preserve_existing_review_completed_at=True,
        preserve_existing_reviewed_at=False,
        preserve_existing_human_acceptance_requested_at=True,
    )
    assert update["review_completed_at"] == _BEFORE  # existing kept
    assert update["reviewed_at"] == _T0  # always now
    assert update["human_acceptance_requested_at"] == _BEFORE  # existing kept


def test_compute_reviewer_verdict_task_update_fast_path_no_existing() -> None:
    update = policy.compute_reviewer_verdict_task_update(
        report_state=AgentReportState.REVIEW_PASSED,
        reviewer_session_id="rev-1",
        now=_T0,
        existing_review_completed_at=None,
        existing_reviewed_at=None,
        existing_human_acceptance_requested_at=None,
        preserve_existing_review_completed_at=True,
        preserve_existing_reviewed_at=False,
        preserve_existing_human_acceptance_requested_at=True,
    )
    assert update["review_completed_at"] == _T0
    assert update["reviewed_at"] == _T0
    assert update["human_acceptance_requested_at"] == _T0


def test_compute_reviewer_verdict_task_update_impl_handler_flags() -> None:
    """Impl handler: completed=now, reviewed=existing-or-now, human=now (passed)."""

    update = policy.compute_reviewer_verdict_task_update(
        report_state=AgentReportState.REVIEW_PASSED,
        reviewer_session_id="rev-1",
        now=_T0,
        existing_review_completed_at=_BEFORE,
        existing_reviewed_at=_BEFORE,
        existing_human_acceptance_requested_at=_BEFORE,
        preserve_existing_review_completed_at=False,
        preserve_existing_reviewed_at=True,
        preserve_existing_human_acceptance_requested_at=False,
    )
    assert update["review_completed_at"] == _T0  # always now
    assert update["reviewed_at"] == _BEFORE  # existing kept
    assert update["human_acceptance_requested_at"] == _T0  # always now (passed)


@pytest.mark.parametrize(
    "report_state",
    [AgentReportState.REVIEW_FAILED, AgentReportState.REVIEW_NEEDS_INPUT],
)
def test_compute_reviewer_verdict_task_update_non_passed_clears_human(
    report_state: AgentReportState,
) -> None:
    update = policy.compute_reviewer_verdict_task_update(
        report_state=report_state,
        reviewer_session_id="rev-1",
        now=_T0,
        existing_review_completed_at=None,
        existing_reviewed_at=None,
        existing_human_acceptance_requested_at=_BEFORE,
        preserve_existing_review_completed_at=False,
        preserve_existing_reviewed_at=True,
        preserve_existing_human_acceptance_requested_at=False,
    )
    assert update["human_acceptance_requested_at"] is None


def test_compute_reviewer_verdict_task_update_goal_packet_never_requests_human() -> None:
    """Goal-packet handler: human_acceptance always None, even on pass."""

    update = policy.compute_reviewer_verdict_task_update(
        report_state=AgentReportState.REVIEW_PASSED,
        reviewer_session_id="rev-1",
        now=_T0,
        existing_review_completed_at=None,
        existing_reviewed_at=_BEFORE,
        existing_human_acceptance_requested_at=None,
        preserve_existing_review_completed_at=False,
        preserve_existing_reviewed_at=True,
        preserve_existing_human_acceptance_requested_at=False,
        human_acceptance_for_passed=False,
    )
    assert update["human_acceptance_requested_at"] is None
    assert update["review_completed_at"] == _T0
    assert update["reviewed_at"] == _BEFORE
