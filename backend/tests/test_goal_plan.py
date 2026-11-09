"""``plan_first`` Goals plan, park on needs_input, and implement after approval."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_hub.models.goal_run import GoalRun, GoalRunCreate, GoalRunStatus
from claude_hub.services.agent_stream.runtime.goal_protocol import (
    _GoalProtocolSanitizer,
)
from claude_hub.services.goal_run import (
    GoalPolicyError,
    GoalRunController,
    GoalRunStore,
)
from claude_hub.services.goal_run.controller import (
    build_continuation_prompt,
    build_dispatch_prompt,
    build_plan_prompt,
)

PLAN_JSON = {
    "summary": "Add a bounded cache with tests",
    "steps": [
        {"title": "Extract the cache", "detail": "Move the lookup into a helper"},
        {"title": "Verify", "acceptance": "targeted tests pass"},
    ],
    "budget": {
        "helpers": [{"role": "implementation", "provider": "claude", "model": "opus"}],
        "max_parallelism": 2,
        "duration_estimate": "about 12 minutes",
    },
}


def plan_response(state: str = "needs_input", plan: object = PLAN_JSON) -> str:
    return f"""Scope: add the cache and verify it.
<goal-plan>
{json.dumps(plan)}
</goal-plan>
<goal-checkpoint>
{{"verified_progress":[],"decisions":["Plan before work"],
 "remaining":["Implement the approved plan"],"blocker":null,"next_step":"Await approval"}}
</goal-checkpoint>
<goal-status state="{state}">Awaiting approval of the plan above.</goal-status>"""


def create_request(request_id: str = "plan-1", *, plan_first: bool = True) -> GoalRunCreate:
    return GoalRunCreate(
        objective="Add a bounded cache",
        client_request_id=request_id,
        plan_first=plan_first,
    )


def controller(tmp_path: Path) -> tuple[GoalRunController, list[tuple[GoalRun, str]]]:
    sent: list[tuple[GoalRun, str]] = []

    async def dispatch(goal: GoalRun, prompt: str) -> str | None:
        sent.append((goal, prompt))
        return goal.pending_step_id

    return GoalRunController(GoalRunStore(tmp_path / "goal_runs.json"), dispatch), sent


def test_plan_prompt_asks_for_scope_acceptance_and_budget_without_actions() -> None:
    goal = GoalRun(tab_id="tab", objective="Add a bounded cache", plan_first=True)
    prompt = build_plan_prompt(goal)
    assert "Do not start it." in prompt
    assert "Take no implementing actions" in prompt
    assert "do not edit or create files, commit, install, delete, or change any state" in prompt
    assert '<goal-status state="needs_input">' in prompt
    assert "<goal-plan>" in prompt
    assert '"max_parallelism"' in prompt and '"helpers"' in prompt and '"model"' in prompt
    assert "execution\nbudget" in prompt or "execution budget" in prompt.replace("\n", " ")
    # The plan turn must not invite autonomous continuation.
    assert 'state="continue|complete|blocked|needs_input"' not in prompt


def test_absent_plan_first_keeps_the_continuation_prompt_byte_identical() -> None:
    """CLI and Feishu callers must not see a changed prompt when the field is absent."""
    plain = GoalRun(tab_id="tab", objective="Verify the change")
    legacy = GoalRun(tab_id="tab", objective="Verify the change", plan_first=False)
    assert build_continuation_prompt(legacy) == build_continuation_prompt(plain)
    assert "goal-plan" not in build_continuation_prompt(plain)
    assert "revise that plan" not in build_continuation_prompt(plain)
    assert build_dispatch_prompt(plain) == build_continuation_prompt(plain)


def test_approved_plan_goals_keep_revision_instructions_in_continuations() -> None:
    goal = GoalRun(tab_id="tab", objective="Add a bounded cache", plan_first=True)
    prompt = build_dispatch_prompt(goal)
    assert "updated <goal-plan> block" in prompt
    assert "before work started" in prompt


@pytest.mark.asyncio
async def test_plan_first_start_dispatches_only_the_plan_turn(tmp_path: Path) -> None:
    manager, sent = controller(tmp_path)
    goal = await manager.create_and_start("tab", create_request())
    assert len(sent) == 1
    prepared, prompt = sent[0]
    assert prompt == build_plan_prompt(prepared)
    assert "Take no implementing actions" in prompt
    assert goal.plan_first is True
    assert goal.plan_pending is True
    assert goal.plan_turn_id == goal.current_turn_id == sent[0][0].pending_step_id
    assert goal.plan is None
    assert goal.plan_approved is False
    assert goal.status == GoalRunStatus.ACTIVE


@pytest.mark.asyncio
async def test_planning_turn_parks_on_needs_input_and_exposes_the_plan(
    tmp_path: Path,
) -> None:
    manager, sent = controller(tmp_path)
    goal = await manager.create_and_start("tab", create_request())
    turn_id = goal.current_turn_id
    assert turn_id is not None
    current = await manager.on_turn_completed("tab", turn_id, "completed", plan_response())
    assert current is not None
    assert current.status == GoalRunStatus.BLOCKED
    assert current.dispatch_state.value == "idle"
    assert current.plan is not None
    assert current.plan.steps[0].title == "Extract the cache"
    assert current.plan.budget.helpers[0].model == "opus"
    assert current.plan.budget.max_parallelism == 2
    assert current.plan_turn_id == turn_id
    assert current.plan_pending is False
    assert current.plan_approved is False
    assert current.plan_warning is None
    # No implementing turn was dispatched behind the user's back.
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_approval_resumes_the_goal_and_switches_to_implementation(
    tmp_path: Path,
) -> None:
    manager, sent = controller(tmp_path)
    goal = await manager.create_and_start("tab", create_request())
    planning_turn = goal.current_turn_id
    assert planning_turn is not None
    await manager.on_turn_completed("tab", planning_turn, "completed", plan_response())

    approved = await manager.steer(
        goal.id,
        "steer:approve",
        "Approve the plan. Start implementing it now.",
        "approve-turn",
    )
    assert approved.plan_approved is True
    assert approved.status == GoalRunStatus.ACTIVE
    assert len(sent) == 2
    prepared, prompt = sent[-1]
    assert prompt != build_plan_prompt(prepared)
    assert "Continue the active Goal." in prompt
    assert "Approve the plan. Start implementing it now." in prompt
    assert "updated <goal-plan> block" in prompt
    assert prepared.status == GoalRunStatus.ACTIVE
    implemented_turn = approved.current_turn_id
    assert implemented_turn is not None

    implemented = await manager.on_turn_completed(
        "tab",
        implemented_turn,
        "completed",
        """Done.
<goal-checkpoint>
{"verified_progress":[{"item":"cache","evidence":"tests pass"}],"decisions":[],
 "remaining":[],"blocker":null,"next_step":null}
</goal-checkpoint>
<goal-status state="complete">cache verified</goal-status>""",
    )
    assert implemented is not None and implemented.status == GoalRunStatus.COMPLETE
    assert implemented.plan_approved is True


@pytest.mark.asyncio
async def test_resume_also_answers_a_pending_plan(tmp_path: Path) -> None:
    manager, _ = controller(tmp_path)
    goal = await manager.create_and_start("tab", create_request())
    assert goal.current_turn_id is not None
    await manager.on_turn_completed("tab", goal.current_turn_id, "completed", plan_response())
    await manager.pause(goal.id, "pause-1")
    resumed = await manager.resume(goal.id, "resume-1")
    assert resumed.plan_approved is True


@pytest.mark.asyncio
async def test_revised_plan_reopens_approval_on_its_own_turn(tmp_path: Path) -> None:
    manager, _ = controller(tmp_path)
    goal = await manager.create_and_start("tab", create_request())
    first_turn = goal.current_turn_id
    assert first_turn is not None
    await manager.on_turn_completed("tab", first_turn, "completed", plan_response())
    revised = await manager.steer(
        goal.id,
        "steer:revise",
        "Revise the plan: split the cache from the loader.",
        "revise-1",
    )
    revision_turn = revised.current_turn_id
    assert revision_turn is not None
    assert revision_turn != first_turn
    current = await manager.on_turn_completed(
        "tab",
        revision_turn,
        "completed",
        plan_response(plan={**PLAN_JSON, "summary": "Revised"}),
    )
    assert current is not None
    assert current.plan is not None and current.plan.summary == "Revised"
    assert current.plan_turn_id == revision_turn
    assert current.plan_approved is False
    assert current.status == GoalRunStatus.BLOCKED


@pytest.mark.asyncio
async def test_cancelled_plan_turn_does_not_spend_the_planning_turn(
    tmp_path: Path,
) -> None:
    manager, sent = controller(tmp_path)
    goal = await manager.create_and_start("tab", create_request())
    turn_id = goal.current_turn_id
    assert turn_id is not None
    cancelled = await manager.on_turn_completed("tab", turn_id, "cancelled", "")
    assert cancelled is not None
    assert cancelled.status == GoalRunStatus.PAUSED
    assert cancelled.plan_pending is True
    resumed = await manager.resume(goal.id, "resume-2")
    assert len(sent) == 2
    assert "Take no implementing actions" in sent[-1][1]
    assert resumed.plan_turn_id == resumed.current_turn_id


@pytest.mark.asyncio
async def test_invalid_plan_block_is_reported_and_never_approves_itself(
    tmp_path: Path,
) -> None:
    manager, _ = controller(tmp_path)
    goal = await manager.create_and_start("tab", create_request())
    assert goal.current_turn_id is not None
    current = await manager.on_turn_completed(
        "tab",
        goal.current_turn_id,
        "completed",
        plan_response(plan={"summary": "", "steps": [], "unknown": 1}),
    )
    assert current is not None
    assert current.status == GoalRunStatus.BLOCKED
    assert current.plan is None
    assert current.plan_approved is False
    assert current.plan_warning and "ignored invalid plan" in current.plan_warning


@pytest.mark.asyncio
async def test_goals_without_plan_first_never_parse_a_plan(tmp_path: Path) -> None:
    manager, sent = controller(tmp_path)
    goal = await manager.create_and_start("tab", create_request(plan_first=False))
    prepared, prompt = sent[0]
    assert prompt == build_continuation_prompt(prepared)
    assert goal.plan_first is False and goal.plan_pending is False
    assert goal.plan_turn_id is None
    assert goal.current_turn_id is not None
    current = await manager.on_turn_completed(
        "tab", goal.current_turn_id, "completed", plan_response()
    )
    assert current is not None
    assert current.plan is None
    assert current.plan_turn_id is None
    assert current.status == GoalRunStatus.BLOCKED


def test_plan_block_is_hidden_from_the_visible_goal_turn() -> None:
    sanitizer = _GoalProtocolSanitizer()
    visible = sanitizer.feed(plan_response(), final=True)
    assert "goal-plan" not in visible
    assert "max_parallelism" not in visible
    assert "Scope: add the cache and verify it." in visible


def test_plan_contract_rejects_out_of_range_budget() -> None:
    from pydantic import ValidationError

    from claude_hub.models.goal_run import GoalPlan

    with pytest.raises(ValidationError):
        GoalPlan.model_validate({"summary": "x", "budget": {"max_parallelism": 0}})
    with pytest.raises(ValidationError):
        GoalPlan.model_validate({"summary": "x", "steps": [{"detail": "no title"}]})


@pytest.mark.asyncio
async def test_native_goal_creation_refuses_plan_first(tmp_path: Path) -> None:
    manager, _ = controller(tmp_path)

    class Connection:
        snapshot = {"thread_id": "thread-1", "goal": None}

    async def resolver(tab_id: str):
        return Connection()

    manager.native.resolver = resolver  # type: ignore[assignment]
    with pytest.raises(GoalPolicyError, match="provider-native Goals"):
        await manager.create_and_start("tab", create_request())
    assert manager.store.current_for_tab("tab") is None
