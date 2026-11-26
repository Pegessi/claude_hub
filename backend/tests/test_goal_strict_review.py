"""Strict cross-review Goal gate tests: consensus PASS, P0/P1 BLOCK loop, quota routing, context, and adaptive convergence."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from claude_hub.models import AgentType, ProviderQuota, QuotaLimit
from claude_hub.models.goal_run import (
    GoalCheckpoint,
    GoalDispatchState,
    GoalRun,
    GoalRunCreate,
    GoalRunStatus,
    GoalStrictReviewFinding,
    GoalStrictReviewRound,
    GoalVerifiedProgress,
)
from claude_hub.services.goal_run import (
    GoalRunController,
    GoalRunStore,
    StrictReviewOutcome,
)
from claude_hub.services.goal_run.native import NativeGoalConnection
from claude_hub.services.goal_run.prompts import (
    build_continuation_prompt,
    build_dispatch_prompt,
)
from claude_hub.services.goal_run.strict_review import (
    _run_git_command,
    build_strict_review_prompt,
    collect_strict_review_context,
    evaluate_review_continuation,
    parse_strict_review_output,
    run_subagent_strict_review,
    select_cross_reviewer_candidates,
)


@pytest.fixture
def push_checkout(tmp_path, monkeypatch):
    """Use an actual clean checkout even in controller lifecycle tests."""
    from claude_hub.services.ttyd_manager import ttyd_manager

    repo = tmp_path / "checkout"
    repo.mkdir()
    for args in [
        ("init", "-b", "feature"),
        (
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        ),
    ]:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    monkeypatch.setattr(ttyd_manager, "get_tab", lambda _: SimpleNamespace(cwd=str(repo)))
    return repo


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
        reviewer_model="gemini-example-model-1dd583607df6_reasoning_high",
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


def test_parse_strict_review_output_30_p2_plus_1_p1_forces_block_and_retains_p1() -> None:
    import json

    findings_list = [
        {
            "severity": "P2",
            "file": f"minor_{i}.py:{i}",
            "summary": f"Optional style observation {i}",
            "fix": None,
        }
        for i in range(1, 31)
    ]
    findings_list.append(
        {
            "severity": "P1",
            "file": "critical_bug.py:99",
            "summary": "Dropped write under concurrent lock contention",
            "fix": "Acquire lock before state mutation",
        }
    )
    raw = (
        "<goal-review>\n"
        + json.dumps(
            {
                "verdict": "PASS",
                "summary": "Many minor observations plus one late P1.",
                "findings": findings_list,
            }
        )
        + "\n</goal-review>"
    )
    parsed = parse_strict_review_output(raw, reviewer_provider="maso")
    assert parsed.verdict == "BLOCK"
    assert len(parsed.findings) == 30
    assert parsed.findings[0].severity == "P1"
    assert parsed.findings[0].file == "critical_bug.py:99"


@pytest.fixture(autouse=True)
def _configured_review_models(monkeypatch: pytest.MonkeyPatch) -> None:
    from claude_hub.services import cross_reviewer

    monkeypatch.setattr(
        cross_reviewer,
        "DEFAULT_ARGON_REVIEW_MODEL",
        "gemini-example-model-1dd583607df6_reasoning_high",
    )
    monkeypatch.setattr(
        cross_reviewer,
        "DEFAULT_ASTRA_REVIEW_MODEL",
        "gpt-example-model-d050a243e4ed_reasoning_high",
    )


def test_select_cross_reviewer_candidates_picks_heterogeneous_models() -> None:
    codex_targets = select_cross_reviewer_candidates(
        AgentType.CODEX,
        {},
        maso_base_url="http://127.0.0.1:11004",
        maso_models=["gemini-example-model-1dd583607df6", "gpt-example-model-d050a243e4ed"],
    )
    assert codex_targets[0].provider == "maso"
    assert codex_targets[0].model == "gemini-example-model-1dd583607df6_reasoning_high"
    assert all(t.provider != "codex" for t in codex_targets[:-1])
    assert codex_targets[-1].provider == "codex"
    assert codex_targets[-1].effort == "high"

    argon_targets = select_cross_reviewer_candidates(
        AgentType.MASO,
        {"MASO_MODEL": "gemini-example-model-1dd583607df6_reasoning_high"},
        maso_base_url="http://127.0.0.1:11004",
        maso_models=["gemini-example-model-1dd583607df6", "gpt-example-model-d050a243e4ed"],
    )
    assert argon_targets[0].provider == "maso"
    assert argon_targets[0].model == "gpt-example-model-d050a243e4ed_reasoning_high"
    assert any(t.provider == "codex" for t in argon_targets)
    assert all(
        t.model != "gemini-example-model-1dd583607df6_reasoning_high" for t in argon_targets[:-1]
    )
    assert argon_targets[-1].provider == "maso"
    assert argon_targets[-1].model == "gemini-example-model-1dd583607df6_reasoning_high"


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
                reviewer_model="gemini-example-model-1dd583607df6_reasoning_high",
                reviewer_subagent_id="sa-r1",
            )
        return StrictReviewOutcome(
            verdict="PASS",
            summary="Lock fix verified and tests pass.",
            findings=[],
            reviewer_provider="maso",
            reviewer_model="gemini-example-model-1dd583607df6_reasoning_high",
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
    assert "Situational hygiene guidance" in sent_prompts[0][1]

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
    distinct_Round_issues = {
        1: ("main.py:10", "Startup config parser crashes on empty env"),
        2: ("router.py:44", "Missing authentication guard on admin endpoint"),
        3: ("store.py:88", "Unbounded list growth on event replay"),
    }

    async def dispatch(goal: GoalRun, prompt: str) -> str | None:
        assert goal.pending_step_id is not None
        sent.append(goal.pending_step_id)
        return goal.pending_step_id

    async def distinct_block(
        goal: GoalRun, assistant_text: str, round_number: int
    ) -> StrictReviewOutcome:
        file_loc, summary = distinct_Round_issues[round_number]
        return StrictReviewOutcome(
            verdict="BLOCK",
            summary=f"Blocking issue in round {round_number}",
            findings=[
                GoalStrictReviewFinding(
                    severity="P0",
                    file=file_loc,
                    summary=summary,
                    fix="Fix defect",
                )
            ],
            reviewer_provider="codex",
        )

    ctrl = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"),
        dispatch=dispatch,
        strict_reviewer=distinct_block,
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

    # Round 3 hits max_rounds (non-converging count 1 -> 1 -> 1) and parks as BLOCKED.
    step3 = sent[-1]
    final = await ctrl.on_turn_completed("tab-max", step3, "completed", complete_envelope())
    assert final is not None
    assert final.status == GoalRunStatus.BLOCKED
    assert final.strict_review_rounds == 3
    assert "3/3 rounds" in (final.status_message or "")


@pytest.mark.asyncio
async def test_strict_review_converging_blockers_grants_grace_round_and_completes(
    tmp_path: Path,
) -> None:
    sent: list[str] = []

    async def dispatch(goal: GoalRun, prompt: str) -> str | None:
        assert goal.pending_step_id is not None
        sent.append(goal.pending_step_id)
        return goal.pending_step_id

    # Monotonically decreasing blocker counts across rounds 1..3: 4 -> 2 -> 1, then PASS on round 4!
    round_blocker_counts = {1: 4, 2: 2, 3: 1, 4: 0}

    async def converging_reviewer(
        goal: GoalRun, assistant_text: str, round_number: int
    ) -> StrictReviewOutcome:
        count = round_blocker_counts[round_number]
        if count == 0:
            return StrictReviewOutcome(
                verdict="PASS",
                summary="All remaining blockers resolved in grace round 4.",
                findings=[],
                reviewer_provider="maso",
                reviewer_model="gpt-example-model-d050a243e4ed_reasoning_high",
            )
        findings = [
            GoalStrictReviewFinding(
                severity="P1",
                file=f"mod_r{round_number}_{idx}.py:{idx * 10}",
                summary=f"Distinct defect r{round_number} item {idx} in pipeline handler",
                fix="Apply targeted fix",
            )
            for idx in range(1, count + 1)
        ]
        return StrictReviewOutcome(
            verdict="BLOCK",
            summary=f"{count} blocking defect(s) remain in round {round_number}",
            findings=findings,
            reviewer_provider="maso",
            reviewer_model="gpt-example-model-d050a243e4ed_reasoning_high",
        )

    ctrl = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"),
        dispatch=dispatch,
        strict_reviewer=converging_reviewer,
    )
    goal = await ctrl.create_and_start(
        "tab-converge",
        GoalRunCreate(
            objective="Ship multi-part refactor",
            client_request_id="req-converge",
            strict_review=True,
        ),
    )
    assert goal.strict_review_max_rounds == 3

    # Rounds 1 and 2 continue within base budget
    for expected_round in (1, 2):
        res = await ctrl.on_turn_completed(
            "tab-converge", sent[-1], "completed", complete_envelope(f"Turn {expected_round}")
        )
        assert res is not None
        assert res.status == GoalRunStatus.ACTIVE
        assert res.strict_review_rounds == expected_round
        assert res.strict_review_max_rounds == 3

    # Round 3 sees blockers drop from 2 -> 1: grants convergence grace round 4 (max_rounds becomes 4)!
    after_r3 = await ctrl.on_turn_completed(
        "tab-converge", sent[-1], "completed", complete_envelope("Turn 3")
    )
    assert after_r3 is not None
    assert after_r3.status == GoalRunStatus.ACTIVE
    assert after_r3.strict_review_rounds == 3
    assert after_r3.strict_review_max_rounds == 4
    assert "grace round 4/4" in (after_r3.status_message or "")

    # Round 4 passes and completes the Goal!
    after_r4 = await ctrl.on_turn_completed(
        "tab-converge", sent[-1], "completed", complete_envelope("Turn 4")
    )
    assert after_r4 is not None
    assert after_r4.status == GoalRunStatus.COMPLETE
    assert after_r4.strict_review_rounds == 4
    assert len(after_r4.strict_review_history) == 4
    assert after_r4.strict_review_history[-1].verdict == "PASS"


@pytest.mark.asyncio
async def test_strict_review_recurring_blocker_impasse_stops_early_and_resets_on_resume(
    tmp_path: Path,
) -> None:
    sent: list[str] = []
    pass_after_resume = False

    async def dispatch(goal: GoalRun, prompt: str) -> str | None:
        assert goal.pending_step_id is not None
        sent.append(goal.pending_step_id)
        return goal.pending_step_id

    async def impasse_reviewer(
        goal: GoalRun, assistant_text: str, round_number: int
    ) -> StrictReviewOutcome:
        if pass_after_resume:
            return StrictReviewOutcome(
                verdict="PASS",
                summary="Resolved after user resume.",
                findings=[],
                reviewer_provider="maso",
            )
        return StrictReviewOutcome(
            verdict="BLOCK",
            summary=f"Same blocker persists in round {round_number}",
            findings=[
                GoalStrictReviewFinding(
                    severity="P1",
                    file="backend/auth.py:42",
                    summary="Token signature verification skipped when header is lowercase",
                    fix="Normalize header keys before lookup",
                )
            ],
            reviewer_provider="maso",
        )

    ctrl = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"),
        dispatch=dispatch,
        strict_reviewer=impasse_reviewer,
    )
    goal = await ctrl.create_and_start(
        "tab-impasse",
        GoalRunCreate(
            objective="Harden token verification",
            client_request_id="req-impasse",
            strict_review=True,
        ),
    )

    # Round 1 blocks and continues
    r1 = await ctrl.on_turn_completed("tab-impasse", sent[-1], "completed", complete_envelope())
    assert r1 is not None
    assert r1.status == GoalRunStatus.ACTIVE
    assert r1.strict_review_rounds == 1

    # Round 2 reports the identical blocker on backend/auth.py -> stops early with impasse!
    r2 = await ctrl.on_turn_completed("tab-impasse", sent[-1], "completed", complete_envelope())
    assert r2 is not None
    assert r2.status == GoalRunStatus.BLOCKED
    assert r2.strict_review_rounds == 2
    assert "impasse" in (r2.status_message or "").lower()

    # Resuming the BLOCKED Goal advances strict_review_cycle and resets the active round counter
    # while preserving prior-cycle audit history!
    pass_after_resume = True
    resumed = await ctrl.resume(goal.id, "resume-after-impasse")
    assert resumed.status == GoalRunStatus.ACTIVE
    assert resumed.strict_review_cycle == 2
    assert resumed.strict_review_rounds == 0
    assert len(resumed.strict_review_history) == 2
    assert all(r.cycle == 1 for r in resumed.strict_review_history)

    done = await ctrl.on_turn_completed("tab-impasse", sent[-1], "completed", complete_envelope())
    assert done is not None
    assert done.status == GoalRunStatus.COMPLETE
    assert done.strict_review_rounds == 1
    assert len(done.strict_review_history) == 3
    assert done.strict_review_history[-1].cycle == 2
    assert done.strict_review_history[-1].round_number == 1


@pytest.mark.asyncio
async def test_collect_strict_review_context_git_worktree_lessons_and_non_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Plain non-Git directory succeeds with zero Git errors or ceremony
    plain_dir = tmp_path / "plain_analysis_dir"
    plain_dir.mkdir()
    (plain_dir / "report.txt").write_text("Throughput +14%\n", encoding="utf-8")

    plain_ctx = await collect_strict_review_context(
        str(plain_dir),
        objective="Analyze Arena throughput results",
        assistant_text="Wrote report.txt with summary table.",
    )
    assert plain_ctx.is_git_repo is False
    assert "not a Git repository" in plain_ctx.git_summary
    assert plain_ctx.primary_cwd == str(plain_dir)

    # 2. Real temporary Git repository + referenced *_worktree/<slug> + REVIEW.md + Workspace lessons
    primary_repo = tmp_path / "my_repo"
    primary_repo.mkdir()
    subprocess.run(["git", "-C", str(primary_repo), "init", "-b", "main"], check=True)
    subprocess.run(
        ["git", "-C", str(primary_repo), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(primary_repo), "config", "user.name", "Test User"], check=True)
    (primary_repo / "app.py").write_text("print('v1')\n", encoding="utf-8")
    (primary_repo / "REVIEW.md").write_text(
        "# Review Guide\nAlways verify lock ordering in app.py.\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(primary_repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(primary_repo), "commit", "-m", "init"], check=True)

    worktree_root = tmp_path / "my_repo_worktree"
    worktree_root.mkdir()
    wt_dir = worktree_root / "feat-lock"
    subprocess.run(
        ["git", "-C", str(primary_repo), "worktree", "add", str(wt_dir), "-b", "feat/lock", "main"],
        check=True,
    )
    (wt_dir / "app.py").write_text("print('v2 with lock')\n", encoding="utf-8")

    from claude_hub.services.workspace_manager import workspace_manager

    fake_ws = SimpleNamespace(id="ws-test-1", path=str(primary_repo))
    monkeypatch.setattr(workspace_manager, "workspaces", {"ws-test-1": fake_ws})
    monkeypatch.setattr(
        workspace_manager,
        "_lesson_context_payload",
        lambda ws, query: [
            {
                "id": "lesson-lock-1",
                "title": "Check asyncio lock re-entrancy",
                "summary": "Never await transport callbacks inside _lock_for",
                "do": "Release lock before awaiting transport",
                "avoid": "Nested lock acquisition",
            }
        ],
    )

    git_ctx = await collect_strict_review_context(
        str(primary_repo),
        objective="Update app.py in isolated worktree",
        assistant_text=f"Implemented changes in {wt_dir} and verified.",
    )
    assert git_ctx.is_git_repo is True
    assert str(wt_dir.resolve()) in git_ctx.referenced_worktrees
    assert git_ctx.primary_cwd == str(wt_dir.resolve())
    assert "feat/lock" in git_ctx.git_summary
    assert "app.py" in git_ctx.git_summary
    assert "Always verify lock ordering" in git_ctx.review_md_excerpt
    assert "lesson-lock-1" in git_ctx.lessons_summary
    assert "Release lock before awaiting transport" in git_ctx.lessons_summary


def test_build_strict_review_prompt_includes_prior_blocker_ledger_and_rebuttal_rules() -> None:
    goal = GoalRun(
        tab_id="tab-prompt",
        objective="Fix race condition in dispatcher",
        strict_review=True,
        strict_review_rounds=1,
        strict_review_history=[
            GoalStrictReviewRound(
                round_number=1,
                worker_turn_id="turn-1",
                reviewer_provider="maso",
                reviewer_model="gemini-example-model-1dd583607df6_reasoning_high",
                verdict="BLOCK",
                summary="Race condition when task completes during cancel.",
                findings=[
                    GoalStrictReviewFinding(
                        severity="P1",
                        file="dispatcher.py:112",
                        summary="State overwritten after cancellation",
                        fix="Re-check status under lock",
                    )
                ],
            )
        ],
        checkpoint=GoalCheckpoint(
            turn_id="turn-2",
            verified_progress=[GoalVerifiedProgress(item="lock check", evidence="pytest passed")],
            decisions=["Re-checked state under lock"],
            remaining=[],
        ),
    )
    prompt = build_strict_review_prompt(
        goal,
        "turn-2",
        "Added state re-check under lock and verified.",
        2,
    )
    assert "prior P0/P1 blocker ledger" in prompt
    assert "[P1] dispatcher.py:112 — State overwritten after cancellation" in prompt
    assert "accept valid evidence-backed rebuttals" in prompt
    assert "Anti-Nitpick Guard" in prompt
    assert "COMPLETE actual git status and diff" in prompt


@pytest.mark.asyncio
async def test_run_subagent_strict_review_falls_back_to_same_model_clean_subagent_and_parks_only_when_all_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.models.subagent import SubagentTurnStatus
    from claude_hub.services import subagents as subagents_mod
    from claude_hub.services.goal_run import strict_review as sr_mod
    from claude_hub.services.ttyd_manager import ttyd_manager

    fake_tab = SimpleNamespace(
        id="tab-no-rev",
        agent_type=AgentType.CLAUDE,
        env={
            "ANTHROPIC_BASE_URL": "https://custom.claude.local",
            "CLAUDE_HUB_TAB_ID": "tab-no-rev",
        },
        cwd=str(tmp_path),
    )
    monkeypatch.setattr(ttyd_manager, "get_tab", lambda tab_id: fake_tab)

    # 1. When cross-providers (Codex/MASO) are exhausted/unavailable, falls back to an
    # independent clean-context same-provider subagent (keep=False).
    async def cross_exhausted_quota():
        return {
            "codex": ProviderQuota(
                provider="codex",
                available=True,
                state="over",
                limits=[QuotaLimit(scope="5h", label="5h", state="over")],
            )
        }

    monkeypatch.setattr(sr_mod, "read_cached_quota_map", cross_exhausted_quota)
    spawned: list[Any] = []

    class FakeCleanSubagentService:
        def __init__(self) -> None:
            self._last: Any = None

        async def create(self, req: Any):
            spawned.append(req)
            rec = SimpleNamespace(
                id="sa-same-clean-1",
                name=req.name,
                agent_type=req.agent_type,
                model=req.model,
            )
            turn = SimpleNamespace(
                turn_id="t-clean-1",
                status=SubagentTurnStatus.COMPLETED,
                final_text='<goal-review>{"verdict":"PASS","summary":"Verified via clean independent subagent","findings":[]}</goal-review>',
            )
            self._last = (rec, turn)
            return rec, turn

        async def wait_turn(self, subagent_id: str, turn_id: str, wait_seconds: float = 30.0):
            return self._last

    monkeypatch.setattr(subagents_mod, "get_subagent_service", lambda: FakeCleanSubagentService())

    async def dispatch(goal: GoalRun, prompt: str) -> str | None:
        return goal.pending_step_id

    ctrl = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch=dispatch)
    goal_ok = await ctrl.create_and_start(
        "tab-no-rev",
        GoalRunCreate(
            objective="Fallback to independent same-model subagent",
            client_request_id="req-same-subagent",
            strict_review=True,
        ),
    )
    assert goal_ok.current_turn_id is not None
    settled_ok = await ctrl.on_turn_completed(
        "tab-no-rev", goal_ok.current_turn_id, "completed", complete_envelope()
    )
    assert settled_ok is not None
    assert settled_ok.status == GoalRunStatus.COMPLETE
    assert len(spawned) == 1
    assert spawned[0].agent_type == AgentType.CLAUDE
    assert spawned[0].keep is False
    assert spawned[0].env == {"ANTHROPIC_BASE_URL": "https://custom.claude.local"}

    # 2. When ALL candidates including same-provider are over quota, parks Goal BLOCKED.
    async def all_exhausted_quota():
        return {
            "codex": ProviderQuota(
                provider="codex",
                available=True,
                state="over",
                limits=[QuotaLimit(scope="5h", label="5h", state="over")],
            ),
            "claude": ProviderQuota(
                provider="claude",
                available=True,
                state="over",
            ),
        }

    monkeypatch.setattr(sr_mod, "read_cached_quota_map", all_exhausted_quota)
    goal = await ctrl.create_and_start(
        "tab-no-rev",
        GoalRunCreate(
            objective="Test no available cross reviewer",
            client_request_id="req-no-rev",
            strict_review=True,
        ),
    )
    assert goal.current_turn_id is not None
    settled = await ctrl.on_turn_completed(
        "tab-no-rev", goal.current_turn_id, "completed", complete_envelope()
    )
    assert settled is not None
    assert settled.status == GoalRunStatus.BLOCKED
    assert "No eligible Chat reviewer subagent candidate" in (settled.status_message or "")


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
                    file=f"module_{round_number}.py:{round_number}",
                    summary=f"Distinct defect in module {round_number} handler",
                    fix="Fix handler",
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

    # Human steers the blocked Goal -> increments cycle, resets active round counter, preserves audit history!
    steered = await ctrl.steer(goal.id, "steer-1", "Apply alternative fix", "user-steer-turn")
    assert steered.status == GoalRunStatus.ACTIVE
    assert steered.strict_review_cycle == 2
    assert steered.strict_review_rounds == 0
    assert len(steered.strict_review_history) == 3

    done = await ctrl.on_turn_completed(
        "tab-steer", "user-steer-turn", "completed", complete_envelope("Fixed after steer")
    )
    assert done is not None
    assert done.status == GoalRunStatus.COMPLETE
    assert done.strict_review_rounds == 1
    assert len(done.strict_review_history) == 4
    assert done.strict_review_history[-1].cycle == 2
    assert done.strict_review_history[-1].round_number == 1
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


@pytest.mark.asyncio
async def test_collect_strict_review_context_rejects_symlink_escaped_review_md(
    tmp_path: Path,
) -> None:
    outside_secret = tmp_path / "outside_secret.txt"
    outside_secret.write_text("TOP_SECRET_TOKEN=xyz123\n", encoding="utf-8")

    repo_dir = tmp_path / "symlink_repo"
    repo_dir.mkdir()
    (repo_dir / "REVIEW.md").symlink_to(outside_secret)

    ctx = await collect_strict_review_context(
        str(repo_dir),
        objective="Check symlink safety",
        assistant_text="Completed.",
    )
    assert ctx.review_md_excerpt == ""
    assert "TOP_SECRET_TOKEN" not in ctx.render_prompt_block()


@pytest.mark.asyncio
async def test_collect_strict_review_context_rejects_fifo_review_md_without_blocking(
    tmp_path: Path,
) -> None:
    import os
    import time

    fifo_repo = tmp_path / "fifo_repo"
    fifo_repo.mkdir()
    os.mkfifo(str(fifo_repo / "REVIEW.md"))

    t0 = time.monotonic()
    ctx = await asyncio.wait_for(
        collect_strict_review_context(
            str(fifo_repo),
            objective="Check FIFO safety",
            assistant_text="Completed.",
        ),
        timeout=1.0,
    )
    elapsed = time.monotonic() - t0
    assert ctx.review_md_excerpt == ""
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_run_git_command_cancellation_kills_descendant_and_overflow_returns_nonzero(
    tmp_path: Path,
) -> None:
    import os
    import sys

    repo_dir = tmp_path / "git_cancel_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "-C", str(repo_dir), "init", "-b", "main"], check=True)

    # 1. Leader exits 0 immediately after spawning a descendant that holds stdout open and writes its PID.
    pid_file = tmp_path / "descendant.pid"
    alias_detach = (
        f"!{sys.executable} -c "
        '"import pathlib, subprocess, sys; '
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f'pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))"'
    )
    cmd_task = asyncio.create_task(
        _run_git_command(
            str(repo_dir),
            "-c",
            f"alias.detach={alias_detach}",
            "detach",
            timeout=10.0,
        )
    )
    for _ in range(50):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.02)
    assert pid_file.exists()
    descendant_pid = int(pid_file.read_text().strip())

    # Cancel _run_git_command while leader has already exited 0 and descendant holds stdout!
    cmd_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cmd_task

    # Descendant in the process group must have been killed by os.killpg!
    for _ in range(25):
        try:
            os.kill(descendant_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.02)
    with pytest.raises(ProcessLookupError):
        os.kill(descendant_pid, 0)

    # 2. Overflowing stdout beyond max_stdout_bytes returns non-zero cleanly without hanging.
    alias_flood = (
        f"!{sys.executable} -c " "\"import sys; sys.stdout.write('A' * 20000); sys.stdout.flush()\""
    )
    rc, out = await _run_git_command(
        str(repo_dir),
        "-c",
        f"alias.flood={alias_flood}",
        "flood",
        timeout=3.0,
        max_stdout_bytes=4096,
    )
    assert rc != 0
    assert out == ""


@pytest.mark.asyncio
async def test_run_git_command_detached_child_holding_stdout_does_not_hang_timeout(
    tmp_path: Path,
) -> None:
    import sys
    import time

    repo_dir = tmp_path / "git_timeout_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "-C", str(repo_dir), "init", "-b", "main"], check=True)

    # Git alias spawns a background child that inherits stdout and sleeps 30s,
    # while the parent also sleeps 30s. _run_git_command must time out and return promptly.
    alias_cmd = (
        f"!{sys.executable} -c "
        '"import subprocess, sys, time; '
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        'time.sleep(30)"'
    )
    t0 = time.monotonic()
    rc, out = await _run_git_command(
        str(repo_dir),
        "-c",
        f"alias.hang={alias_cmd}",
        "hang",
        timeout=0.25,
    )
    elapsed = time.monotonic() - t0
    assert rc == 124
    assert out == ""
    assert elapsed < 3.0


@pytest.mark.asyncio
async def test_run_subagent_strict_review_uses_active_maso_session_model_over_changed_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.models import StreamModelOption
    from claude_hub.models.subagent import SubagentTurnStatus
    from claude_hub.services.agent_stream import tailer as tailer_mod
    from claude_hub.services.goal_run import strict_review as sr_mod
    from claude_hub.services.ttyd_manager import ttyd_manager

    # Tab env has no MASO_MODEL, but the active MasoHttpSession is running Argon.
    # Meanwhile the MASO service's global default has changed to Astra.
    fake_tab = SimpleNamespace(
        id="tab-maso-live",
        agent_type=AgentType.MASO,
        env={},
        cwd=str(tmp_path),
    )
    monkeypatch.setattr(ttyd_manager, "get_tab", lambda tab_id: fake_tab)

    fake_transport = SimpleNamespace(
        session=SimpleNamespace(tab_id="tab-maso-live"),
        current_model=lambda: "gemini-example-model-1dd583607df6_reasoning_high",
    )
    fake_tailer = SimpleNamespace(native_transport=fake_transport)
    fake_manager = SimpleNamespace(
        get_tailer=lambda sid: fake_tailer if sid == "tab-maso-live" else None,
        tailers=lambda: [fake_tailer],
    )
    monkeypatch.setattr(tailer_mod, "_TAILER_MANAGERS", [fake_manager])

    async def fake_discover(_env: dict[str, str]):
        return (
            "http://127.0.0.1:11004",
            [
                StreamModelOption(id="gemini-example-model-1dd583607df6", label="Argon"),
                StreamModelOption(id="gpt-example-model-d050a243e4ed", label="Astra"),
            ],
            "gpt-example-model-d050a243e4ed_reasoning_high",
        )

    monkeypatch.setattr(sr_mod, "discover_maso_service", fake_discover)

    created_requests: list[Any] = []

    class FakeSubagentService:
        def __init__(self) -> None:
            self._last: Any = None

        async def create(self, req: Any):
            created_requests.append(req)
            rec = SimpleNamespace(
                id="sa-live-1",
                name=req.name,
                agent_type=req.agent_type,
                model=req.model,
            )
            turn = SimpleNamespace(
                turn_id="t-1",
                status=SubagentTurnStatus.COMPLETED,
                final_text='<goal-review>{"verdict":"PASS","summary":"Verified","findings":[]}</goal-review>',
            )
            self._last = (rec, turn)
            return rec, turn

        async def wait_turn(self, subagent_id: str, turn_id: str, wait_seconds: float = 30.0):
            return self._last

    from claude_hub.services import subagents as subagents_mod

    monkeypatch.setattr(subagents_mod, "get_subagent_service", lambda: FakeSubagentService())

    goal = GoalRun(tab_id="tab-maso-live", objective="Verify active MASO model", strict_review=True)
    outcome = await run_subagent_strict_review(goal, "Completed.", 1)
    assert outcome.verdict == "PASS"
    assert len(created_requests) == 1
    # Because the active session was Argon, the reviewer MUST be Astra (not Argon self-review)!
    assert created_requests[0].agent_type == AgentType.MASO
    assert created_requests[0].model == "gpt-example-model-d050a243e4ed_reasoning_high"


@pytest.mark.asyncio
async def test_update_objective_during_in_flight_review_does_not_charge_new_cycle(
    tmp_path: Path,
) -> None:
    review_entered = asyncio.Event()
    release_review = asyncio.Event()
    dispatched_prompts: list[str] = []

    async def dispatch(goal: GoalRun, prompt: str) -> str | None:
        dispatched_prompts.append(prompt)
        return goal.pending_step_id

    async def slow_block_reviewer(
        goal: GoalRun, assistant_text: str, round_number: int
    ) -> StrictReviewOutcome:
        review_entered.set()
        await release_review.wait()
        return StrictReviewOutcome(
            verdict="BLOCK",
            summary="Stale review against old objective",
            findings=[
                GoalStrictReviewFinding(
                    severity="P1",
                    file="old.py:1",
                    summary="Old objective issue",
                    fix="Fix old",
                )
            ],
            reviewer_provider="maso",
        )

    ctrl = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"),
        dispatch=dispatch,
        strict_reviewer=slow_block_reviewer,
    )
    goal = await ctrl.create_and_start(
        "tab-obj-race",
        GoalRunCreate(
            objective="Original objective v1",
            client_request_id="req-obj-v1",
            strict_review=True,
        ),
    )
    assert goal.current_turn_id is not None
    turn_task = asyncio.create_task(
        ctrl.on_turn_completed(
            "tab-obj-race", goal.current_turn_id, "completed", complete_envelope()
        )
    )
    await asyncio.wait_for(review_entered.wait(), timeout=5)

    # Edit objective while reviewer is in flight
    updated = await ctrl.update_objective(goal.id, "req-obj-v2", "Updated objective v2")
    assert updated.objective == "Updated objective v2"
    assert updated.strict_review_cycle == 2
    assert updated.strict_review_rounds == 0

    release_review.set()
    settled = await turn_task
    assert settled is not None
    assert settled.status == GoalRunStatus.ACTIVE
    # Stale review must NOT have charged round 1 or polluted history on cycle 2!
    assert settled.strict_review_cycle == 2
    assert settled.strict_review_rounds == 0
    assert settled.strict_review_history == []
    assert settled.strict_review_feedback is None
    assert len(dispatched_prompts) == 2
    assert "Updated objective v2" in dispatched_prompts[-1]


@pytest.mark.asyncio
async def test_plan_first_and_strict_review_planning_turn_complete_signal_parks_for_approval(
    tmp_path: Path,
) -> None:
    review_called = False

    async def dispatch(goal: GoalRun, prompt: str) -> str | None:
        return goal.pending_step_id

    async def reviewer(
        goal: GoalRun, assistant_text: str, round_number: int
    ) -> StrictReviewOutcome:
        nonlocal review_called
        review_called = True
        return StrictReviewOutcome(
            verdict="PASS",
            summary="Should not run on planning turn",
            findings=[],
            reviewer_provider="maso",
        )

    ctrl = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"),
        dispatch=dispatch,
        strict_reviewer=reviewer,
    )
    goal = await ctrl.create_and_start(
        "tab-plan-strict",
        GoalRunCreate(
            objective="Plan first with strict review",
            client_request_id="req-plan-strict",
            plan_first=True,
            strict_review=True,
        ),
    )
    assert goal.plan_pending is True
    assert goal.current_turn_id is not None

    # Model disobeys the planning turn prompt and emits state="complete" instead of "needs_input"
    disobedient_plan_turn = (
        "Here is the plan.\n"
        "<goal-plan>\n"
        '{"summary":"Add cache","steps":[{"title":"Step 1","detail":"Code","acceptance":"pytest"}],'
        '"budget":{"helpers":[],"max_parallelism":1,"duration_estimate":"5m"}}\n'
        "</goal-plan>\n"
        "<goal-checkpoint>\n"
        '{"verified_progress":[],"decisions":[],"remaining":["Step 1"],"blocker":null,"next_step":"Approve"}\n'
        "</goal-checkpoint>\n"
        '<goal-status state="complete">Plan complete</goal-status>'
    )
    after_plan = await ctrl.on_turn_completed(
        "tab-plan-strict", goal.current_turn_id, "completed", disobedient_plan_turn
    )
    assert after_plan is not None
    assert review_called is False
    assert after_plan.status == GoalRunStatus.BLOCKED
    assert after_plan.strict_review_in_progress is False
    assert after_plan.strict_review_rounds == 0
    assert after_plan.status_message == "The plan awaits your approval before the Goal continues"


def test_cross_review_preferences_and_consensus():
    from claude_hub.models.goal_run import CrossReviewModelPreference, GoalStrictReviewFinding
    from claude_hub.services.cross_reviewer import select_cross_reviewer_candidates
    from claude_hub.services.goal_run.strict_review import (
        StrictReviewOutcome,
        merge_strict_review_outcomes,
    )

    preferences = [
        CrossReviewModelPreference(provider="codex", model="reviewer-alpha"),
        CrossReviewModelPreference(provider="claude", model="reviewer-beta"),
    ]
    candidates = select_cross_reviewer_candidates(
        AgentType.CLAUDE, preferred_candidates=preferences
    )
    assert candidates[0].model == "reviewer-alpha"
    assert candidates[-1].model == "reviewer-beta" or candidates[-1].provider == "claude"
    a = StrictReviewOutcome("PASS", "clear", [], "codex", "reviewer-alpha")
    b = StrictReviewOutcome(
        "PASS",
        "issue",
        [
            GoalStrictReviewFinding(
                severity="P1",
                file="a.py:3",
                summary="Missing validation causes data loss",
                fix="Validate input",
            )
        ],
        "claude",
        "reviewer-beta",
    )
    merged = merge_strict_review_outcomes([a, b])
    assert merged.verdict == "BLOCK"
    assert merged.findings[0].severity == "P1"
    assert merged.reviewer_provider == "codex+claude"


def test_cross_review_presets_and_settings(tmp_path, monkeypatch):
    from pydantic import ValidationError

    from claude_hub.models.goal_run import GoalRunCreate
    from claude_hub.services import cross_review_settings as settings_module

    monkeypatch.setattr(settings_module, "resolve_runtime_home", lambda: tmp_path)
    with pytest.raises(ValidationError, match="Read-only audit preset cannot push branches"):
        settings_module.CrossReviewSettings(
            default_preset="readonly_brief", push_remote_branch=True
        )
    with pytest.raises(ValidationError, match="Read-only audit preset cannot push branches"):
        GoalRunCreate(
            objective="audit",
            client_request_id="id",
            strict_review_preset="readonly_brief",
            strict_review_push_branch=True,
        )
    request = GoalRunCreate(
        objective="audit", client_request_id="id", strict_review_preset="mr_slim"
    )
    assert request.strict_review
    saved = settings_module.CrossReviewSettings(review_mode="dual")
    settings_module.write_cross_review_settings(saved)
    assert settings_module.read_cross_review_settings() == saved


@pytest.mark.asyncio
async def test_dual_review_concurrent_and_backup(tmp_path, monkeypatch):
    from claude_hub.models.subagent import SubagentTurnStatus
    from claude_hub.services import subagents
    from claude_hub.services.goal_run import strict_review as sr
    from claude_hub.services.ttyd_manager import ttyd_manager

    monkeypatch.setattr(
        ttyd_manager,
        "get_tab",
        lambda _: SimpleNamespace(agent_type=AgentType.CURSOR, env={}, cwd=str(tmp_path)),
    )
    candidates = [
        sr.CrossReviewCandidate("codex", "reviewer-alpha"),
        sr.CrossReviewCandidate("claude", "reviewer-beta"),
        sr.CrossReviewCandidate("cursor", "reviewer-gamma"),
    ]
    monkeypatch.setattr(sr, "select_cross_reviewer_candidates", lambda *args, **kwargs: candidates)
    entered = 0
    both_started = asyncio.Event()
    calls = []

    class Service:
        async def create(self, req):
            nonlocal entered
            calls.append(req.call_id)
            entered += 1
            if entered == 2:
                both_started.set()
            if entered <= 2:
                await asyncio.wait_for(both_started.wait(), timeout=2)
            return SimpleNamespace(id=req.model), SimpleNamespace(turn_id=req.model)

        async def wait_turn(self, record_id, turn_id, wait_seconds):
            text = (
                "invalid"
                if record_id == "reviewer-beta"
                else '<goal-review>{"verdict":"PASS","summary":"verified","findings":[]}</goal-review>'
            )
            return SimpleNamespace(id=record_id), SimpleNamespace(
                status=SubagentTurnStatus.COMPLETED, final_text=text
            )

    monkeypatch.setattr(subagents, "get_subagent_service", lambda: Service())
    goal = GoalRun(tab_id="tab", objective="review", strict_review=True, strict_review_mode="dual")
    outcome = await run_subagent_strict_review(goal, "done", 1)
    assert outcome.verdict == "PASS"
    assert outcome.reviewer_provider == "codex+cursor"
    assert (
        len(calls) == 3
        and ":slot0:" in calls[0]
        and ":slot1:" in calls[1]
        and ":slot1:" in calls[2]
    )


def test_review_preset_rubrics():
    for preset, phrase in [
        ("mr_slim", "Mandatory Subtraction Gate"),
        ("bug_only", "P2/P3 advisory only"),
        ("readonly_brief", "repository untouched"),
    ]:
        goal = GoalRun(
            tab_id="tab",
            objective="review",
            strict_review_preset=preset,
            strict_review_briefing=True,
        )
        prompt = build_strict_review_prompt(goal, "turn", "done", 1)
        assert phrase in prompt
        assert "3-Minute Review Briefing" in prompt


@pytest.mark.asyncio
async def test_push_continuation_waits_for_review_pass(tmp_path, push_checkout):
    prompts = []

    async def dispatch(goal, prompt):
        prompts.append(prompt)
        return f"turn-{len(prompts)}"

    async def reviewer(goal, text, round_number):
        return StrictReviewOutcome("PASS", "verified", [], "codex")

    controller = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"), dispatch, strict_reviewer=reviewer
    )
    goal = await controller.create_and_start(
        "tab",
        GoalRunCreate(
            objective="review",
            client_request_id="push",
            strict_review=True,
            strict_review_push_branch=True,
        ),
    )
    assert "Do not push yet" in prompts[0]
    goal = await controller.on_turn_completed(
        "tab", goal.current_turn_id, "completed", complete_envelope()
    )
    assert goal.status == GoalRunStatus.ACTIVE and goal.strict_review_push_pending
    assert "independent strict review passed" in prompts[1]
    assert "never force push" in prompts[1]
    changed = await controller.update_objective(goal.id, "edit", "changed objective")
    assert not changed.strict_review_push_pending and changed.strict_review_cycle == 2
    goal = await controller.on_turn_completed(
        "tab", changed.current_turn_id, "completed", complete_envelope()
    )
    assert goal.strict_review_push_pending
    goal = await controller.on_turn_completed(
        "tab", goal.current_turn_id, "completed", complete_envelope("Push reported")
    )
    assert goal.status == GoalRunStatus.COMPLETE


def test_invalid_settings_and_effort_fall_back(tmp_path, monkeypatch):
    from pydantic import ValidationError

    from claude_hub.models.goal_run import CrossReviewModelPreference
    from claude_hub.services import cross_review_settings as settings

    monkeypatch.setattr(settings, "resolve_runtime_home", lambda: tmp_path)
    path = tmp_path / "cross_review_settings.json"
    for contents in ('{"review_mode":', '{"review_mode":"invalid"}'):
        path.write_text(contents)
        assert settings.read_cross_review_settings() == settings.CrossReviewSettings()
    for provider in ("maso", "claude", "cursor"):
        with pytest.raises(ValidationError, match="no separate reasoning-effort"):
            CrossReviewModelPreference(provider=provider, effort="high")
    assert CrossReviewModelPreference(provider="codex", effort="high").effort == "high"


@pytest.mark.asyncio
async def test_push_approval_interruption_and_recovery(tmp_path, push_checkout):
    async def dispatch(goal, prompt):
        return goal.pending_step_id

    async def reviewer(goal, text, round_number):
        return StrictReviewOutcome("PASS", "verified", [], "codex")

    manager = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"), dispatch, strict_reviewer=reviewer
    )
    goal = await manager.create_and_start(
        "synthetic",
        GoalRunCreate(
            objective="review",
            client_request_id="create",
            strict_review=True,
            strict_review_push_branch=True,
        ),
    )
    goal = await manager.on_turn_completed(
        "synthetic", goal.current_turn_id, "completed", complete_envelope()
    )
    assert goal.strict_review_push_pending and goal.strict_review_push_attempts == 1
    paused = await manager.pause(goal.id, "pause")
    assert not paused.strict_review_push_pending and paused.strict_review_push_target is None
    assert paused.strict_review_cycle == 2 and paused.strict_review_rounds == 0
    assert paused.strict_review_push_attempts == 1
    resumed = await manager.resume(goal.id, "resume")
    assert not resumed.strict_review_push_pending and resumed.strict_review_push_attempts == 1
    resumed = await manager.on_turn_completed(
        "synthetic", resumed.current_turn_id, "completed", complete_envelope()
    )
    assert resumed.strict_review_push_pending and resumed.strict_review_push_attempts == 2
    manager.recover()
    recovered = manager.get(goal.id)
    assert not recovered.strict_review_push_pending and recovered.strict_review_push_target is None
    assert recovered.strict_review_cycle == 3 and recovered.strict_review_push_attempts == 2


@pytest.mark.asyncio
async def test_push_approval_tracks_git_candidate(tmp_path, monkeypatch):
    from claude_hub.services.ttyd_manager import ttyd_manager

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.test")
    (repo / "file.txt").write_text("one")
    git("add", "file.txt")
    git("commit", "-m", "initial")
    monkeypatch.setattr(ttyd_manager, "get_tab", lambda _: SimpleNamespace(cwd=str(repo)))

    async def dispatch(goal, prompt):
        return goal.pending_step_id

    async def reviewer(goal, text, round_number):
        return StrictReviewOutcome("PASS", "verified", [], "codex")

    manager = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"), dispatch, strict_reviewer=reviewer
    )
    req = GoalRunCreate(
        objective="review",
        client_request_id="first",
        strict_review=True,
        strict_review_push_branch=True,
    )
    goal = await manager.create_and_start("protected", req)
    result = await manager.on_turn_completed(
        "protected", goal.current_turn_id, "completed", complete_envelope()
    )
    assert result.status == GoalRunStatus.COMPLETE and "Protected" in result.status_message
    git("checkout", "-b", "feature")
    goal = await manager.create_and_start(
        "feature", req.model_copy(update={"client_request_id": "second"})
    )
    result = await manager.on_turn_completed(
        "feature", goal.current_turn_id, "completed", complete_envelope()
    )
    assert (
        result.strict_review_push_target == f"{repo.resolve()}@feature:{git('rev-parse', 'HEAD')}"
    )
    (repo / "file.txt").write_text("two")
    git("add", "file.txt")
    git("commit", "-m", "change")
    result = await manager.on_turn_completed(
        "feature", result.current_turn_id, "completed", complete_envelope()
    )
    assert result.strict_review_cycle == 2
    assert result.strict_review_push_pending and result.strict_review_push_target.endswith(
        git("rev-parse", "HEAD")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,signal",
    [
        ("cancelled", "complete"),
        ("failed", "complete"),
        ("completed", "continue"),
        ("completed", "blocked"),
        ("completed", "needs_input"),
    ],
)
async def test_interrupted_push_never_retains_approval(tmp_path, push_checkout, status, signal):
    async def dispatch(goal, prompt):
        return goal.pending_step_id

    async def reviewer(goal, text, round_number):
        return StrictReviewOutcome("PASS", "verified", [], "codex")

    manager = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"), dispatch, strict_reviewer=reviewer
    )
    goal = await manager.create_and_start(
        "tab",
        GoalRunCreate(
            objective="review",
            client_request_id="id",
            strict_review=True,
            strict_review_push_branch=True,
        ),
    )
    goal = await manager.on_turn_completed(
        "tab", goal.current_turn_id, "completed", complete_envelope()
    )
    assert goal.strict_review_push_pending
    result = await manager.on_turn_completed(
        "tab", goal.current_turn_id, status, f'<goal-status state="{signal}">waiting</goal-status>'
    )
    assert not result.strict_review_push_pending
    assert result.strict_review_push_target is None
    assert result.strict_review_cycle == 2


@pytest.mark.asyncio
async def test_candidate_changes_during_review_never_authorize_push(tmp_path, monkeypatch):
    from claude_hub.services.ttyd_manager import ttyd_manager

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-b", "feature")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.test")
    (repo / "file.txt").write_text("initial")
    git("add", "file.txt")
    git("commit", "-m", "initial")
    monkeypatch.setattr(ttyd_manager, "get_tab", lambda _: SimpleNamespace(cwd=str(repo)))

    async def dispatch(goal, prompt):
        return goal.pending_step_id

    async def reviewer(goal, text, round_number):
        (repo / "file.txt").write_text("changed during review")
        return StrictReviewOutcome("PASS", "verified", [], "codex")

    manager = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"), dispatch, strict_reviewer=reviewer
    )
    goal = await manager.create_and_start(
        "tab",
        GoalRunCreate(
            objective="review",
            client_request_id="id",
            strict_review=True,
            strict_review_push_branch=True,
        ),
    )
    result = await manager.on_turn_completed(
        "tab", goal.current_turn_id, "completed", complete_envelope()
    )
    assert result.status == GoalRunStatus.ACTIVE
    assert not result.strict_review_push_pending and not result.strict_review_history
    assert result.current_turn_id is not None


@pytest.mark.asyncio
async def test_push_candidate_reuses_approved_referenced_worktree(tmp_path, monkeypatch):
    from claude_hub.services.goal_run.strict_review import inspect_push_candidate
    from claude_hub.services.ttyd_manager import ttyd_manager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    worktree = tmp_path / "workspace_worktree" / "feat-x"
    worktree.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "feat-x", str(worktree)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(ttyd_manager, "get_tab", lambda _: SimpleNamespace(cwd=str(workspace)))
    goal = GoalRun(tab_id="tab", objective="Review the change", strict_review=True)
    approved, reason = await inspect_push_candidate(goal, f"Implemented in {worktree}")
    assert reason is None and approved is not None
    goal.strict_review_push_target = approved
    assert await inspect_push_candidate(goal, "Pushed feat-x to origin") == (approved, None)
    other = worktree.parent / "other"
    other.mkdir()
    assert await inspect_push_candidate(goal, f"Pushed from {other}") == (
        None,
        "Push turn referenced a different worktree",
    )


@pytest.mark.asyncio
async def test_multiple_worktrees_skip_automatic_push(tmp_path, monkeypatch):
    from claude_hub.services.ttyd_manager import ttyd_manager

    root = tmp_path / "project_worktree"
    candidate = root / "candidate"
    baseline = root / "baseline"
    candidate.mkdir(parents=True)
    baseline.mkdir()
    subprocess.run(
        ["git", "init", "-b", "feature", str(candidate)], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(candidate),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(ttyd_manager, "get_tab", lambda _: SimpleNamespace(cwd=str(candidate)))
    calls = []

    async def dispatch(goal, prompt):
        calls.append(prompt)
        return goal.pending_step_id

    async def reviewer(goal, text, round_number):
        return StrictReviewOutcome("PASS", "verified", [], "codex")

    manager = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"), dispatch, strict_reviewer=reviewer
    )
    goal = await manager.create_and_start(
        "tab",
        GoalRunCreate(
            objective=f"Review {candidate} against {baseline}",
            client_request_id="multi",
            strict_review=True,
            strict_review_push_branch=True,
        ),
    )
    result = await manager.on_turn_completed(
        "tab", goal.current_turn_id, "completed", complete_envelope()
    )
    assert result.status == GoalRunStatus.COMPLETE
    assert result.strict_review_rounds == 1 and not result.strict_review_push_pending
    assert "automatic push skipped: Multiple worktrees referenced" in result.status_message
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_repeated_changed_push_candidate_stops_at_hard_cap(tmp_path, monkeypatch):
    from claude_hub.models.goal_run import GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS
    from claude_hub.services.ttyd_manager import ttyd_manager

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-b", "feature")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.test")
    git("commit", "--allow-empty", "-m", "initial")
    monkeypatch.setattr(ttyd_manager, "get_tab", lambda _: SimpleNamespace(cwd=str(repo)))

    async def dispatch(goal, prompt):
        return goal.pending_step_id

    async def reviewer(goal, text, round_number):
        return StrictReviewOutcome("PASS", "verified", [], "codex")

    manager = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"), dispatch, strict_reviewer=reviewer
    )
    goal = await manager.create_and_start(
        "tab",
        GoalRunCreate(
            objective="review",
            client_request_id="cap",
            strict_review=True,
            strict_review_push_branch=True,
        ),
    )
    goal = await manager.on_turn_completed(
        "tab", goal.current_turn_id, "completed", complete_envelope()
    )
    for index in range(GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS):
        assert goal.strict_review_push_pending
        git("commit", "--allow-empty", "-m", f"change-{index}")
        previous_turn = goal.current_turn_id
        goal = await manager.on_turn_completed(
            "tab", previous_turn, "completed", complete_envelope()
        )
    assert goal.status == GoalRunStatus.BLOCKED
    assert goal.strict_review_cycle == GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS + 1
    assert not goal.strict_review_push_pending and not goal.strict_review_in_progress
    assert previous_turn in goal.completed_turn_ids and goal.current_turn_id is None
    assert "push manually or steer the Goal" in goal.status_message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "signal", ['<goal-status state="continue">continue</goal-status>', "No status emitted"]
)
@pytest.mark.parametrize("recovery", ["resume", "steer"])
async def test_incomplete_push_turns_stop_at_cap_and_user_can_recover(
    tmp_path, monkeypatch, signal, recovery
):
    from claude_hub.models.goal_run import GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS
    from claude_hub.services.ttyd_manager import ttyd_manager

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-b", "feature")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.test")
    git("commit", "--allow-empty", "-m", "initial")
    monkeypatch.setattr(ttyd_manager, "get_tab", lambda _: SimpleNamespace(cwd=str(repo)))

    async def dispatch(goal, prompt):
        return goal.pending_step_id

    async def reviewer(goal, text, round_number):
        return StrictReviewOutcome("PASS", "verified", [], "codex")

    manager = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"), dispatch, strict_reviewer=reviewer
    )
    goal = await manager.create_and_start(
        "tab",
        GoalRunCreate(
            objective="review",
            client_request_id="cap",
            strict_review=True,
            strict_review_push_branch=True,
        ),
    )
    goal = await manager.on_turn_completed(
        "tab", goal.current_turn_id, "completed", complete_envelope()
    )
    for attempt in range(GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS):
        assert goal.strict_review_push_pending
        assert goal.strict_review_push_attempts == attempt + 1
        git("commit", "--allow-empty", "-m", f"change-{attempt}")
        goal = await manager.on_turn_completed("tab", goal.current_turn_id, "completed", signal)
        if attempt + 1 < GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS:
            assert goal.status == GoalRunStatus.ACTIVE
            goal = await manager.on_turn_completed(
                "tab", goal.current_turn_id, "completed", complete_envelope()
            )
    assert goal.status == GoalRunStatus.BLOCKED
    assert goal.dispatch_state == GoalDispatchState.IDLE and goal.current_turn_id is None
    assert not goal.strict_review_push_pending
    if recovery == "resume":
        goal = await manager.resume(goal.id, "user-recovery")
    else:
        goal = await manager.steer(goal.id, "user-recovery", "Try again", "steer-turn")
    assert goal.strict_review_push_attempts == 0
    goal = await manager.on_turn_completed(
        "tab", goal.current_turn_id, "completed", complete_envelope()
    )
    assert goal.strict_review_push_pending and goal.strict_review_push_attempts == 1


@pytest.mark.asyncio
async def test_repeated_changes_during_review_stop_at_push_cap(tmp_path, monkeypatch):
    from claude_hub.models.goal_run import GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS
    from claude_hub.services.ttyd_manager import ttyd_manager

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-b", "feature")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.test")
    git("commit", "--allow-empty", "-m", "initial")
    monkeypatch.setattr(ttyd_manager, "get_tab", lambda _: SimpleNamespace(cwd=str(repo)))

    async def dispatch(goal, prompt):
        return goal.pending_step_id

    async def reviewer(goal, text, round_number):
        git("commit", "--allow-empty", "-m", "changed during review")
        return StrictReviewOutcome("PASS", "verified", [], "codex")

    manager = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"), dispatch, strict_reviewer=reviewer
    )
    goal = await manager.create_and_start(
        "tab",
        GoalRunCreate(
            objective="review",
            client_request_id="cap",
            strict_review=True,
            strict_review_push_branch=True,
        ),
    )
    for _ in range(GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS):
        goal = await manager.on_turn_completed(
            "tab", goal.current_turn_id, "completed", complete_envelope()
        )
    assert goal.status == GoalRunStatus.BLOCKED
    assert goal.strict_review_push_attempts == GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS
    assert goal.dispatch_state == GoalDispatchState.IDLE and goal.current_turn_id is None
    assert not goal.strict_review_push_pending
    assert "checkout changed during review" in goal.status_message


@pytest.mark.asyncio
async def test_missing_chat_never_authorizes_a_push(tmp_path, monkeypatch):
    from claude_hub.services.ttyd_manager import ttyd_manager

    monkeypatch.setattr(ttyd_manager, "get_tab", lambda _: None)
    sent = []

    async def dispatch(goal, prompt):
        sent.append(prompt)
        return goal.pending_step_id

    async def reviewer(goal, text, round_number):
        return StrictReviewOutcome("PASS", "verified", [], "codex")

    ctrl = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"), dispatch, strict_reviewer=reviewer
    )
    goal = await ctrl.create_and_start(
        "missing",
        GoalRunCreate(
            objective="review",
            client_request_id="create",
            strict_review=True,
            strict_review_push_branch=True,
        ),
    )
    result = await ctrl.on_turn_completed(
        "missing", goal.current_turn_id, "completed", complete_envelope()
    )
    assert result.status == GoalRunStatus.COMPLETE
    assert not result.strict_review_push_pending
    assert "automatic push skipped" in result.status_message
    assert len(sent) == 1


@pytest.mark.parametrize(
    "effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
)
def test_review_effort_uses_subagent_runtime_contract(effort):
    from claude_hub.models.goal_run import CrossReviewModelPreference
    from claude_hub.services.subagents import REASONING_EFFORTS

    assert effort in REASONING_EFFORTS
    assert CrossReviewModelPreference(provider="codex", effort=effort).effort == effort


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
async def test_recoverable_push_failure_pauses_and_requires_fresh_review(
    tmp_path, push_checkout, uncertain
):
    async def dispatch(goal, prompt):
        return goal.pending_step_id

    async def reviewer(goal, text, round_number):
        return StrictReviewOutcome("PASS", "verified", [], "codex")

    ctrl = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"), dispatch, strict_reviewer=reviewer
    )
    goal = await ctrl.create_and_start(
        "tab",
        GoalRunCreate(
            objective="review",
            client_request_id="create",
            strict_review=True,
            strict_review_push_branch=True,
        ),
    )
    goal = await ctrl.on_turn_completed(
        "tab", goal.current_turn_id, "completed", complete_envelope()
    )
    turn_id = goal.current_turn_id
    result = await ctrl.on_turn_completed(
        "tab", turn_id, "failed", "", retryable=True, delivery_uncertain=uncertain
    )
    assert result.status == GoalRunStatus.PAUSED
    assert result.dispatch_state == (
        GoalDispatchState.UNCERTAIN if uncertain else GoalDispatchState.IDLE
    )
    assert result.current_turn_id == (turn_id if uncertain else None)
    assert not result.strict_review_push_pending
    assert result.strict_review_push_target is None
    assert result.strict_review_cycle == 2
    assert result.strict_review_push_attempts == 1
