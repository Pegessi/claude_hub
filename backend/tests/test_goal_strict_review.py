"""Strict cross-review Goal gate tests: consensus PASS, P0/P1 BLOCK loop, and native override."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from claude_hub.models import AgentType
from claude_hub.models.goal_run import (
    GoalDispatchState,
    GoalRun,
    GoalRunCreate,
    GoalRunStatus,
    GoalStrictReviewFinding,
)
from claude_hub.services.goal_run import (
    GoalRunController,
    GoalRunStore,
    StrictReviewOutcome,
)
from claude_hub.services.goal_run.controller import (
    build_continuation_prompt,
    build_dispatch_prompt,
)
from claude_hub.services.goal_run.native import NativeGoalConnection
from claude_hub.services.goal_run.strict_review import (
    parse_strict_review_output,
    select_cross_reviewer_candidates,
)


def complete_envelope(summary: str = "Implemented and verified") -> str:
    return (
        f"{summary}\n"
        "<goal-checkpoint>\n"
        '{"verified_progress":[{"item":"cache","evidence":"pytest -q passed"}],'
        '"decisions":["bounded LRU"],"remaining":[],"blocker":null,"next_step":null}\n'
        "</goal-checkpoint>\n"
        f'<goal-status state="complete">{summary}</goal-status>'
    )


def test_parse_strict_review_output_enforces_p0_p1_block() -> None:
    raw_pass = """Checked git diff and tests.
<goal-review>
{
  "verdict": "PASS",
  "summary": "All acceptance items verified with zero blocking defects.",
  "findings": [
    {"severity": "P2", "file": "cache.py:42", "summary": "Consider naming constant", "fix": null}
  ]
}
</goal-review>"""
    parsed_pass = parse_strict_review_output(
        raw_pass,
        reviewer_provider="maso",
        reviewer_model="[redacted]_reasoning_high",
        reviewer_subagent_id="sa-1",
    )
    assert parsed_pass.verdict == "PASS"
    assert len(parsed_pass.findings) == 1

    # Even if the model claims PASS, a P1 finding must fail-closed to BLOCK.
    raw_contradictory = """<goal-review>
{
  "verdict": "PASS",
  "summary": "Looks mostly fine.",
  "findings": [
    {"severity": "P1", "file": "api.py:88", "summary": "Missing lock on concurrent write", "fix": "Acquire _lock"}
  ]
}
</goal-review>"""
    parsed_block = parse_strict_review_output(
        raw_contradictory,
        reviewer_provider="maso",
    )
    assert parsed_block.verdict == "BLOCK"


def test_select_cross_reviewer_candidates_picks_heterogeneous_models() -> None:
    codex_targets = select_cross_reviewer_candidates(
        AgentType.CODEX,
        {},
        maso_base_url="http://127.0.0.1:11004",
        maso_models=["[redacted]", "[redacted]"],
    )
    assert codex_targets[0].provider == "maso"
    assert codex_targets[0].model == "[redacted]_reasoning_high"

    argon_targets = select_cross_reviewer_candidates(
        AgentType.MASO,
        {"MASO_MODEL": "[redacted]_reasoning_high"},
        maso_base_url="http://127.0.0.1:11004",
        maso_models=["[redacted]", "[redacted]"],
    )
    assert argon_targets[0].provider == "maso"
    assert argon_targets[0].model == "[redacted]_reasoning_high"
    assert any(t.provider == "codex" for t in argon_targets)


@pytest.mark.asyncio
async def test_strict_review_blocks_p1_continues_and_completes_on_consensus(tmp_path: Path) -> None:
    sent_prompts: list[tuple[str, str]] = []
    review_calls: list[int] = []

    async def dispatch(goal: GoalRun, prompt: str) -> str | None:
        assert goal.pending_step_id is not None
        sent_prompts.append((goal.pending_step_id, prompt))
        return goal.pending_step_id

    async def fake_reviewer(
        goal: GoalRun, assistant_text: str, round_number: int
    ) -> StrictReviewOutcome:
        review_calls.append(round_number)
        if round_number == 1:
            return StrictReviewOutcome(
                verdict="BLOCK",
                summary="Found race condition on cache eviction.",
                findings=[
                    GoalStrictReviewFinding(
                        severity="P1",
                        file="backend/cache.py:31",
                        summary="Eviction mutates dict without lock",
                        fix="Hold self._lock around popitem()",
                    )
                ],
                reviewer_provider="maso",
                reviewer_model="[redacted]_reasoning_high",
                reviewer_subagent_id="sa-r1",
            )
        return StrictReviewOutcome(
            verdict="PASS",
            summary="Lock fix verified and tests pass.",
            findings=[],
            reviewer_provider="maso",
            reviewer_model="[redacted]_reasoning_high",
            reviewer_subagent_id="sa-r2",
        )

    ctrl = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"),
        dispatch=dispatch,
        strict_reviewer=fake_reviewer,
    )
    goal = await ctrl.create_and_start(
        "tab-1",
        GoalRunCreate(
            objective="Implement thread-safe cache",
            client_request_id="req-strict-1",
            strict_review=True,
        ),
    )
    assert goal.strict_review is True
    assert goal.status == GoalRunStatus.ACTIVE
    assert len(sent_prompts) == 1
    assert "Strict cross-model review is enabled" in sent_prompts[0][1]

    # Turn 1 claims complete -> reviewer round 1 returns BLOCK -> controller dispatches turn 2
    first_turn_id = sent_prompts[0][0]
    after_r1 = await ctrl.on_turn_completed(
        "tab-1", first_turn_id, "completed", complete_envelope("Initial implementation")
    )
    assert after_r1 is not None
    assert review_calls == [1]
    assert after_r1.status == GoalRunStatus.ACTIVE
    assert after_r1.strict_review_rounds == 1
    assert len(after_r1.strict_review_history) == 1
    assert after_r1.strict_review_history[0].verdict == "BLOCK"
    assert len(sent_prompts) == 2
    assert "backend/cache.py:31" in sent_prompts[1][1]
    assert "Eviction mutates dict without lock" in sent_prompts[1][1]

    # Turn 2 fixes bug and claims complete -> reviewer round 2 returns PASS -> COMPLETE
    second_turn_id = sent_prompts[1][0]
    after_r2 = await ctrl.on_turn_completed(
        "tab-1", second_turn_id, "completed", complete_envelope("Fixed lock and added test")
    )
    assert after_r2 is not None
    assert review_calls == [1, 2]
    assert after_r2.status == GoalRunStatus.COMPLETE
    assert after_r2.strict_review_rounds == 2
    assert len(after_r2.strict_review_history) == 2
    assert after_r2.strict_review_history[1].verdict == "PASS"
    assert len(sent_prompts) == 2


@pytest.mark.asyncio
async def test_strict_review_parks_blocked_after_max_rounds(tmp_path: Path) -> None:
    sent: list[str] = []

    async def dispatch(goal: GoalRun, prompt: str) -> str | None:
        assert goal.pending_step_id is not None
        sent.append(goal.pending_step_id)
        return goal.pending_step_id

    async def always_block(
        goal: GoalRun, assistant_text: str, round_number: int
    ) -> StrictReviewOutcome:
        return StrictReviewOutcome(
            verdict="BLOCK",
            summary=f"Still failing in round {round_number}",
            findings=[
                GoalStrictReviewFinding(
                    severity="P0",
                    file="main.py:10",
                    summary="Crashes on startup",
                    fix="Initialize config first",
                )
            ],
            reviewer_provider="codex",
        )

    ctrl = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"),
        dispatch=dispatch,
        strict_reviewer=always_block,
    )
    goal = await ctrl.create_and_start(
        "tab-max",
        GoalRunCreate(
            objective="Fix startup",
            client_request_id="req-max",
            strict_review=True,
        ),
    )
    assert goal.strict_review_max_rounds == 3

    for expected_round in (1, 2):
        step = sent[-1]
        res = await ctrl.on_turn_completed("tab-max", step, "completed", complete_envelope())
        assert res is not None
        assert res.status == GoalRunStatus.ACTIVE
        assert res.strict_review_rounds == expected_round

    # Round 3 hits max_rounds and parks as BLOCKED for human judgment.
    step3 = sent[-1]
    final = await ctrl.on_turn_completed("tab-max", step3, "completed", complete_envelope())
    assert final is not None
    assert final.status == GoalRunStatus.BLOCKED
    assert final.strict_review_rounds == 3
    assert "3/3 rounds" in (final.status_message or "")


@pytest.mark.asyncio
async def test_strict_review_bypasses_provider_native_to_preserve_hub_gate(tmp_path: Path) -> None:
    dispatched: list[str] = []
    native_calls: list[str] = []

    async def dispatch(goal: GoalRun, prompt: str) -> str | None:
        dispatched.append(prompt)
        return goal.pending_step_id

    async def resolver(tab_id: str) -> NativeGoalConnection:
        async def read():
            return {"thread_id": "codex-t1", "goal": None}

        async def set_goal(updates):
            native_calls.append("set")
            return updates

        async def clear_goal():
            native_calls.append("clear")
            return True

        async def stop_turn():
            native_calls.append("stop")

        async def active_turn():
            return None

        async def send_input(goal, text, turn_id):
            native_calls.append("send")

        return NativeGoalConnection(
            snapshot={"thread_id": "codex-t1", "goal": None},
            read=read,
            set_goal=set_goal,
            clear_goal=clear_goal,
            stop_turn=stop_turn,
            active_turn_id=active_turn,
            send_input=send_input,
            turn_in_flight=lambda: False,
        )

    ctrl = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch=dispatch)
    ctrl.native.resolver = resolver

    goal = await ctrl.create_and_start(
        "codex-tab",
        GoalRunCreate(
            objective="Build with strict review and plan first on Codex",
            client_request_id="codex-strict-1",
            plan_first=True,
            strict_review=True,
        ),
    )
    assert goal.execution_owner == "hub_managed"
    assert goal.strict_review is True
    assert goal.plan_first is True
    assert not native_calls
    assert len(dispatched) == 1
    assert "<goal-plan>" in dispatched[0]

    # Refreshing current goal must NOT cancel the Hub-managed strict Goal!
    refreshed = await ctrl.refresh_current("codex-tab")
    assert refreshed is not None
    assert refreshed.id == goal.id
    assert refreshed.status == GoalRunStatus.ACTIVE


@pytest.mark.asyncio
async def test_pause_cancels_in_flight_strict_review(tmp_path: Path) -> None:
    review_started = asyncio.Event()
    review_cancelled = False

    async def dispatch(goal: GoalRun, prompt: str) -> str | None:
        return goal.pending_step_id

    async def slow_reviewer(
        goal: GoalRun, assistant_text: str, round_number: int
    ) -> StrictReviewOutcome:
        nonlocal review_cancelled
        review_started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            review_cancelled = True
            raise
        raise AssertionError("unreachable")

    ctrl = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"),
        dispatch=dispatch,
        strict_reviewer=slow_reviewer,
    )
    goal = await ctrl.create_and_start(
        "tab-cancel",
        GoalRunCreate(
            objective="Cancel review test",
            client_request_id="req-cancel",
            strict_review=True,
        ),
    )
    assert goal.current_turn_id is not None
    completion_task = asyncio.create_task(
        ctrl.on_turn_completed("tab-cancel", goal.current_turn_id, "completed", complete_envelope())
    )
    await asyncio.wait_for(review_started.wait(), timeout=5)
    paused = await ctrl.pause(goal.id, "pause-req")
    assert paused.status == GoalRunStatus.PAUSED
    assert paused.strict_review_in_progress is False
    assert paused.dispatch_state == GoalDispatchState.IDLE
    assert paused.status_message == "strict cross-model review was stopped"
    # on_turn_completed should return cleanly without raising CancelledError
    settled = await completion_task
    assert settled is not None
    assert review_cancelled is True


@pytest.mark.asyncio
async def test_steer_after_max_rounds_resets_budget_and_persists_valid_history(
    tmp_path: Path,
) -> None:
    sent: list[str] = []
    verdicts = ["BLOCK", "BLOCK", "BLOCK", "PASS"]

    async def dispatch(goal: GoalRun, prompt: str) -> str | None:
        assert goal.pending_step_id is not None
        sent.append(goal.pending_step_id)
        return goal.pending_step_id

    async def reviewer(
        goal: GoalRun, assistant_text: str, round_number: int
    ) -> StrictReviewOutcome:
        verdict = verdicts.pop(0)
        findings = (
            [
                GoalStrictReviewFinding(
                    severity="P1",
                    file="x.py:1",
                    summary="Defect",
                    fix="Fix x",
                )
            ]
            if verdict == "BLOCK"
            else []
        )
        return StrictReviewOutcome(
            verdict=verdict,
            summary=f"Verdict {verdict}",
            findings=findings,
            reviewer_provider="maso",
        )

    store_path = tmp_path / "goals.json"
    ctrl = GoalRunController(
        GoalRunStore(store_path),
        dispatch=dispatch,
        strict_reviewer=reviewer,
    )
    goal = await ctrl.create_and_start(
        "tab-steer",
        GoalRunCreate(
            objective="Ship feature",
            client_request_id="req-steer",
            strict_review=True,
        ),
    )
    for _ in range(3):
        res = await ctrl.on_turn_completed("tab-steer", sent[-1], "completed", complete_envelope())
    assert res is not None
    assert res.status == GoalRunStatus.BLOCKED
    assert res.strict_review_rounds == 3

    # Human steers the blocked Goal -> resets review budget -> 4th review passes -> reload works!
    steered = await ctrl.steer(goal.id, "steer-1", "Apply alternative fix", "user-steer-turn")
    assert steered.status == GoalRunStatus.ACTIVE
    assert steered.strict_review_rounds == 0

    done = await ctrl.on_turn_completed(
        "tab-steer", "user-steer-turn", "completed", complete_envelope("Fixed after steer")
    )
    assert done is not None
    assert done.status == GoalRunStatus.COMPLETE
    assert len(done.strict_review_history) == 3
    reloaded = GoalRunStore(store_path)
    assert reloaded.get(goal.id).status == GoalRunStatus.COMPLETE


@pytest.mark.asyncio
async def test_clearing_strict_goal_on_codex_does_not_resurrect_terminal_native_goal(
    tmp_path: Path,
) -> None:
    native_goal: Any = {
        "threadId": "codex-t2",
        "objective": "Old completed native goal",
        "status": "complete",
        "createdAt": 100,
        "updatedAt": 105,
        "tokensUsed": 50,
        "timeUsedSeconds": 10,
        "tokenBudget": None,
    }
    cleared_calls = 0

    async def dispatch(goal: GoalRun, prompt: str) -> str | None:
        return goal.pending_step_id

    async def resolver(tab_id: str) -> NativeGoalConnection:
        async def read():
            return {"thread_id": "codex-t2", "goal": native_goal}

        async def set_goal(updates):
            return updates

        async def clear_goal():
            nonlocal native_goal, cleared_calls
            cleared_calls += 1
            native_goal = None
            return True

        async def stop_turn():
            return None

        async def active_turn():
            return None

        async def send_input(goal, text, turn_id):
            return None

        return NativeGoalConnection(
            snapshot={"thread_id": "codex-t2", "goal": native_goal},
            read=read,
            set_goal=set_goal,
            clear_goal=clear_goal,
            stop_turn=stop_turn,
            active_turn_id=active_turn,
            send_input=send_input,
            turn_in_flight=lambda: False,
        )

    ctrl = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch=dispatch)
    ctrl.native.resolver = resolver

    strict_goal = await ctrl.create_and_start(
        "codex-tab-2",
        GoalRunCreate(
            objective="New strict goal",
            client_request_id="req-strict-codex",
            strict_review=True,
        ),
    )
    assert cleared_calls == 1
    assert native_goal is None
    await ctrl.clear(strict_goal.id, "clear-strict")
    assert await ctrl.refresh_current("codex-tab-2") is None


def test_store_create_without_explicit_request_replays_strict_review(tmp_path: Path) -> None:
    store = GoalRunStore(tmp_path / "goals.json")
    goal = GoalRun(tab_id="tab-x", objective="Test", strict_review=True, plan_first=True)
    first = store.create(goal, "cid-1")
    replay = store.create(goal, "cid-1")
    assert replay.id == first.id


def test_legacy_goal_without_strict_review_keeps_verbatim_prompt() -> None:
    plain = GoalRun(tab_id="tab", objective="Verify")
    assert "Strict cross-model review" not in build_continuation_prompt(plain)
    assert build_dispatch_prompt(plain) == build_continuation_prompt(plain)
