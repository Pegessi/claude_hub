"""Steer during strict review must preserve both the review and user intent."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from claude_hub.models.goal_run import (
    GOAL_STEER_INPUT_MAX_LENGTH,
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

COMPLETE = '<goal-status state="complete">Verified candidate</goal-status>'


class ReviewHarness:
    def __init__(self, path: Path, verdict: str = "PASS") -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.verdict = verdict
        self.sent: list[tuple[GoalRun, str]] = []
        self.controller = GoalRunController(
            GoalRunStore(path), self.dispatch, strict_reviewer=self.review
        )

    async def dispatch(self, goal: GoalRun, prompt: str) -> str | None:
        self.sent.append((goal, prompt))
        return goal.pending_step_id

    async def review(
        self, goal: GoalRun, assistant_text: str, round_number: int
    ) -> StrictReviewOutcome:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.verdict == "ERROR":
            raise RuntimeError("review transport unavailable")
        return StrictReviewOutcome(
            verdict=self.verdict,
            summary="Candidate review finished",
            findings=(
                [GoalStrictReviewFinding(severity="P1", summary="Missing boundary check")]
                if self.verdict == "BLOCK"
                else []
            ),
            reviewer_provider="test-reviewer",
            reviewer_subagent_id="review-child",
        )

    async def start(self, *, push: bool = False) -> tuple[GoalRun, asyncio.Task[GoalRun | None]]:
        goal = await self.controller.create_and_start(
            "tab-review",
            GoalRunCreate(
                objective="Verify candidate",
                client_request_id="create",
                strict_review=True,
                strict_review_push_branch=push,
            ),
        )
        assert goal.current_turn_id is not None
        completion = asyncio.create_task(
            self.controller.on_turn_completed(
                goal.tab_id, goal.current_turn_id, "completed", COMPLETE
            )
        )
        await asyncio.wait_for(self.started.wait(), 5)
        return goal, completion


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["PASS", "BLOCK", "ERROR"])
@pytest.mark.parametrize(
    "messages", [["Report progress"], ["Report progress", "Adjust the summary"]]
)
async def test_review_time_steers_deliver_one_combined_continuation(tmp_path, verdict, messages):
    harness = ReviewHarness(tmp_path / "goals.json", verdict)
    ctrl = harness.controller
    goal, completion = await harness.start()
    try:
        reviewer_task = ctrl._strict_review_tasks[goal.id]
        for index, message in enumerate(messages):
            queued = await ctrl.steer(goal.id, f"steer-{index}", message, f"user-{index}")
            assert queued.status == GoalRunStatus.ACTIVE
            assert queued.strict_review_in_progress
            assert queued.current_turn_id is None
            assert "queued" in (queued.status_message or "").lower()
            replay = await ctrl.steer(goal.id, f"steer-{index}", message, f"user-{index}")
            assert replay.pending_input == queued.pending_input
            assert ctrl._strict_review_tasks[goal.id] is reviewer_task
            assert not reviewer_task.done()
        assert len(harness.sent) == 1
        await ctrl.dispatch_next(goal.id)
        assert len(harness.sent) == 1
        persisted = GoalRunStore(tmp_path / "goals.json").get(goal.id)
        assert persisted.pending_input is not None
        assert all(message in persisted.pending_input for message in messages)

        harness.release.set()
        settled = await asyncio.wait_for(completion, 5)
        assert settled is not None
        assert not harness.cancelled
        assert settled.status == GoalRunStatus.ACTIVE
        assert settled.completed_at is None
        assert settled.dispatch_state == GoalDispatchState.DISPATCHED
        assert settled.strict_review_rounds == 1
        assert len(settled.strict_review_history) == 1
        assert len(harness.sent) == 2
        dispatched, prompt = harness.sent[-1]
        assert all(prompt.count(message) == 1 for message in messages)
        assert [prompt.index(message) for message in messages] == sorted(
            prompt.index(message) for message in messages
        )
        if verdict == "ERROR":
            assert settled.strict_review_history[-1].verdict == "BLOCK"
            assert "review transport unavailable" in prompt
        else:
            assert settled.strict_review_history[-1].verdict == verdict
            assert f"returned {verdict}" in prompt
            assert "Candidate review finished" in prompt
        if verdict == "BLOCK":
            assert "Missing boundary check" in prompt
        if verdict == "PASS":
            assert "did not pass" not in prompt
        assert dispatched.pending_input
        assert settled.pending_input is None
        assert settled.strict_review_feedback is None
        await ctrl.on_turn_completed(goal.tab_id, goal.current_turn_id or "", "completed", COMPLETE)
        assert len(harness.sent) == 2
    finally:
        harness.release.set()
        await completion


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,status",
    [
        ("pause", GoalRunStatus.PAUSED),
        ("clear", GoalRunStatus.CANCELLED),
        ("complete", GoalRunStatus.COMPLETE),
    ],
)
async def test_explicit_stop_still_cancels_review_with_queued_steer(tmp_path, action, status):
    harness = ReviewHarness(tmp_path / "goals.json")
    ctrl = harness.controller
    goal, completion = await harness.start()
    try:
        await ctrl.steer(goal.id, "steer", "Keep this user input", "user-turn")
        stopped = await getattr(ctrl, action)(goal.id, "stop")
        await asyncio.wait_for(completion, 5)
        assert stopped.status == status
        assert not stopped.strict_review_in_progress
        assert stopped.dispatch_state == GoalDispatchState.IDLE
        assert harness.cancelled
        assert len(harness.sent) == 1
        assert not stopped.strict_review_history
        if action == "pause":
            assert stopped.pending_input == "Keep this user input"
    finally:
        harness.release.set()
        await completion


@pytest.mark.asyncio
async def test_review_queue_overflow_rejects_without_losing_accepted_input(tmp_path):
    harness = ReviewHarness(tmp_path / "goals.json")
    ctrl = harness.controller
    goal, completion = await harness.start()
    try:
        text = "x" * GOAL_STEER_INPUT_MAX_LENGTH
        await ctrl.steer(goal.id, "first", text, "user-first")
        with pytest.raises(ValueError, match="limited"):
            await ctrl.steer(goal.id, "second", "must not replace first", "user-second")
        saved = ctrl.get(goal.id)
        assert saved.pending_input == text
        assert "second" not in saved.idempotency
        assert saved.strict_review_in_progress
        assert not harness.cancelled
    finally:
        harness.release.set()
        await completion


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["PASS", "BLOCK", "ERROR"])
async def test_last_round_queued_input_gets_worker_turn_and_fresh_review_budget(tmp_path, verdict):
    harness = ReviewHarness(tmp_path / "goals.json", verdict)
    ctrl = harness.controller
    goal, completion = await harness.start()
    try:
        reviewing = ctrl.get(goal.id)
        reviewing.strict_review_max_rounds = 1
        ctrl.store.put(reviewing)
        await ctrl.steer(goal.id, "human", "Address this before finishing", "human")
        harness.release.set()
        settled = await asyncio.wait_for(completion, 5)
        assert settled is not None
        assert settled.status == GoalRunStatus.ACTIVE
        assert len(harness.sent) == 2
        assert settled.strict_review_history[0].cycle == 1
        assert settled.strict_review_history[0].round_number == 1
        assert settled.strict_review_cycle == 2
        assert settled.strict_review_rounds == 0
    finally:
        harness.release.set()
        await completion


@pytest.mark.asyncio
async def test_review_error_without_steer_records_round_and_parks(tmp_path):
    harness = ReviewHarness(tmp_path / "goals.json", "ERROR")
    goal, completion = await harness.start()
    harness.release.set()
    settled = await asyncio.wait_for(completion, 5)
    assert settled is not None
    assert settled.status == GoalRunStatus.BLOCKED
    assert len(harness.sent) == 1
    assert settled.strict_review_history[0].verdict == "BLOCK"
    assert "review transport unavailable" in settled.strict_review_history[0].summary


@pytest.mark.asyncio
async def test_queued_continuation_failure_retains_review_and_input_for_resume(
    tmp_path,
):
    harness = ReviewHarness(tmp_path / "goals.json")
    ctrl = harness.controller
    goal, completion = await harness.start()

    async def fail(goal, prompt):
        raise RuntimeError("acceptance uncertain")

    try:
        await ctrl.steer(goal.id, "human", "Do not lose this", "human")
        ctrl.set_dispatch_callback(fail)
        harness.release.set()
        settled = await asyncio.wait_for(completion, 5)
        assert settled is not None
        assert settled.status == GoalRunStatus.PAUSED
        assert settled.dispatch_state == GoalDispatchState.UNCERTAIN
        assert settled.pending_input == "Do not lose this"
        assert settled.strict_review_history[0].verdict == "PASS"
        assert "returned PASS" in (settled.strict_review_feedback or "")
        cold = GoalRunStore(tmp_path / "goals.json").get(goal.id)
        assert cold.pending_input == settled.pending_input
        assert cold.strict_review_feedback == settled.strict_review_feedback
    finally:
        harness.release.set()
        await completion


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["finish", "pause", "clear", "complete", "uncertain"])
async def test_image_only_review_queue_lifetime_and_recovery(tmp_path, action):
    from claude_hub.models.chat_image import ChatImageAttachment
    from claude_hub.services.goal_run.images import (
        goal_image_input,
        image_fingerprint,
        pending_goal_images,
    )

    harness = ReviewHarness(tmp_path / "goals.json")
    ctrl = harness.controller
    goal, completion = await harness.start()
    image = ChatImageAttachment(
        filename="reference.png",
        mime_type="image/png",
        data_url="data:image/png;base64,iVBORw0KGgo=",
        preview_is_original=True,
    )
    delivered = []

    async def dispatch(goal, prompt):
        delivered.extend(pending_goal_images(goal))
        if action == "uncertain":
            raise RuntimeError("acceptance uncertain")
        return await harness.dispatch(goal, prompt)

    try:
        ctrl.set_dispatch_callback(dispatch)
        with goal_image_input(goal.tab_id, [image]):
            queued = await ctrl.steer(
                goal.id, "image", "", "image-turn", 1, image_fingerprint([image])
            )
        assert queued.pending_input == ""
        assert queued.pending_image_count == 1
        assert len(ctrl._review_steer_images[goal.id]) == 1
        if action in {"pause", "clear", "complete"}:
            stopped = await getattr(ctrl, action)(goal.id, "stop")
            await asyncio.wait_for(completion, 5)
            assert harness.cancelled
            assert not delivered
            assert not ctrl._review_steer_images
            if action == "pause":
                assert stopped.pending_image_count == 1
                with pytest.raises(ValueError, match="Reattach"):
                    await ctrl.resume(goal.id, "resume")
        else:
            harness.release.set()
            settled = await asyncio.wait_for(completion, 5)
            assert settled is not None
            assert delivered == [image]
            assert not ctrl._review_steer_images
            assert settled.strict_review_history[0].verdict == "PASS"
            if action == "uncertain":
                assert settled.pending_image_count == 1
                assert settled.pending_input == ""
                cold = GoalRunController(GoalRunStore(tmp_path / "goals.json"))
                with pytest.raises(ValueError, match="Reattach"):
                    pending_goal_images(cold.get(goal.id))
            else:
                assert settled.status == GoalRunStatus.ACTIVE
                assert settled.pending_image_count == 0
    finally:
        harness.release.set()
        await completion


def test_review_image_batch_enforces_aggregate_preview_bytes():
    from claude_hub.models.chat_image import ChatImageAttachment
    from claude_hub.services.goal_run.images import combine_review_images

    # Three individually permitted 14 MiB payloads cross the 40 MiB turn cap.
    data_url = "data:image/png;base64," + "AAAA" * (14 * 1024 * 1024 // 3 + 1)
    image = ChatImageAttachment(filename="large.png", mime_type="image/png", data_url=data_url)
    assert len(combine_review_images([image], [image])) == 2
    with pytest.raises(ValueError, match="40 MiB"):
        combine_review_images([image, image], [image])
    preview = image.model_copy(update={"preview_data_url": data_url})
    with pytest.raises(ValueError, match="40 MiB"):
        combine_review_images([preview], [image])


@pytest.mark.asyncio
async def test_earlier_dispatch_finalizer_cannot_drop_new_review_queue_images(tmp_path):
    from claude_hub.models.chat_image import ChatImageAttachment
    from claude_hub.services.goal_run.images import (
        goal_image_input,
        image_fingerprint,
        pending_goal_images,
    )

    harness = ReviewHarness(tmp_path / "goals.json")
    ctrl = harness.controller
    old_send_started, old_send_return = asyncio.Event(), asyncio.Event()
    captured = []
    calls = 0

    async def dispatch(goal, prompt):
        nonlocal calls
        calls += 1
        if calls == 2:
            old_send_started.set()
            await old_send_return.wait()
        captured.extend(pending_goal_images(goal))
        return await harness.dispatch(goal, prompt)

    ctrl.set_dispatch_callback(dispatch)
    initial = await ctrl.create_and_start(
        "tab-review",
        GoalRunCreate(objective="Verify candidate", client_request_id="create", strict_review=True),
    )
    old_send = asyncio.create_task(
        ctrl.on_turn_completed(
            initial.tab_id,
            initial.current_turn_id or "",
            "completed",
            '<goal-status state="continue">More work</goal-status>',
        )
    )
    await asyncio.wait_for(old_send_started.wait(), 5)
    worker = ctrl.get(initial.id)
    review = asyncio.create_task(
        ctrl.on_turn_completed(initial.tab_id, worker.current_turn_id or "", "completed", COMPLETE)
    )
    await asyncio.wait_for(harness.started.wait(), 5)
    image = ChatImageAttachment(
        filename="new.png",
        mime_type="image/png",
        data_url="data:image/png;base64,iVBORw0KGgo=",
    )
    try:
        with goal_image_input(initial.tab_id, [image]):
            await ctrl.steer(
                initial.id,
                "image",
                "Use this image",
                "image",
                1,
                image_fingerprint([image]),
            )
        old_send_return.set()
        await asyncio.wait_for(old_send, 5)
        await ctrl.steer(initial.id, "text", "Then answer this", "text")
        assert ctrl.get(initial.id).pending_image_count == 1
        harness.release.set()
        settled = await asyncio.wait_for(review, 5)
        assert settled.status == GoalRunStatus.ACTIVE
        assert captured == [image]
    finally:
        old_send_return.set()
        harness.release.set()
        await asyncio.gather(old_send, review)


@pytest.mark.asyncio
async def test_reattaching_review_queue_images_preserves_all_accepted_text(tmp_path):
    from claude_hub.models.chat_image import ChatImageAttachment
    from claude_hub.services.goal_run.images import goal_image_input, image_fingerprint

    harness = ReviewHarness(tmp_path / "goals.json")
    ctrl = harness.controller
    goal, completion = await harness.start()
    image = ChatImageAttachment(
        filename="ref.png",
        mime_type="image/png",
        data_url="data:image/png;base64,iVBORw0KGgo=",
    )
    with goal_image_input(goal.tab_id, [image]):
        await ctrl.steer(goal.id, "one", "Instruction A", "one", 1, image_fingerprint([image]))
    await ctrl.steer(goal.id, "two", "Instruction B", "two")
    await ctrl.pause(goal.id, "pause")
    await asyncio.wait_for(completion, 5)
    cold = GoalRunController(GoalRunStore(tmp_path / "goals.json"), harness.dispatch)
    with goal_image_input(goal.tab_id, [image]):
        recovered = await cold.steer(
            goal.id, "recover", "", "recover", 1, image_fingerprint([image])
        )
    assert recovered.status == GoalRunStatus.ACTIVE
    assert "Instruction A" in harness.sent[-1][1]
    assert "Instruction B" in harness.sent[-1][1]


@pytest.mark.asyncio
async def test_review_recovery_retry_does_not_append_the_same_amendment_twice(tmp_path):
    from claude_hub.models.chat_image import ChatImageAttachment
    from claude_hub.services.goal_run.images import goal_image_input, image_fingerprint

    harness = ReviewHarness(tmp_path / "goals.json")
    ctrl = harness.controller
    goal, completion = await harness.start()
    image = ChatImageAttachment(
        filename="ref.png",
        mime_type="image/png",
        data_url="data:image/png;base64,iVBORw0KGgo=",
    )
    with goal_image_input(goal.tab_id, [image]):
        await ctrl.steer(goal.id, "one", "Instruction A", "one", 1, image_fingerprint([image]))
    await ctrl.steer(goal.id, "two", "Instruction B", "two")
    await ctrl.pause(goal.id, "pause")
    await asyncio.wait_for(completion, 5)

    async def fail(goal, prompt):
        raise RuntimeError("acceptance uncertain")

    ctrl.set_dispatch_callback(fail)
    with goal_image_input(goal.tab_id, [image]):
        await ctrl.steer(
            goal.id,
            "retry-one",
            "Amendment C",
            "retry-one",
            1,
            image_fingerprint([image]),
        )
    cold = GoalRunController(GoalRunStore(tmp_path / "goals.json"), harness.dispatch)
    with goal_image_input(goal.tab_id, [image]):
        await cold.steer(
            goal.id,
            "retry-two",
            "Amendment C",
            "retry-two",
            1,
            image_fingerprint([image]),
        )
    prompt = harness.sent[-1][1]
    assert prompt.count("Instruction A") == 1
    assert prompt.count("Instruction B") == 1
    assert prompt.count("Amendment C") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["PASS", "BLOCK", "ERROR", "CANCEL"])
async def test_configured_review_queue_precedes_push_and_waits_for_reviewer(
    tmp_path, monkeypatch, outcome
):
    """Exercise the real slot runner and controller, not a replacement review callback."""
    from types import SimpleNamespace

    from claude_hub.models import AgentType
    from claude_hub.models.subagent import SubagentTurnStatus
    from claude_hub.services import subagents
    from claude_hub.services.goal_run import controller as controller_module
    from claude_hub.services.goal_run import strict_review as sr
    from claude_hub.services.ttyd_manager import ttyd_manager

    monkeypatch.setattr(
        ttyd_manager,
        "get_tab",
        lambda _: SimpleNamespace(
            agent_type=AgentType.CURSOR,
            env={},
            cwd=str(tmp_path),
        ),
    )
    candidates = [sr.CrossReviewCandidate("codex", "reviewer-alpha")]
    monkeypatch.setattr(sr, "select_cross_reviewer_candidates", lambda *a, **kw: candidates)
    started = {c.provider: asyncio.Event() for c in candidates}
    release = {c.provider: asyncio.Event() for c in candidates}
    finished = {c.provider: asyncio.Event() for c in candidates}
    closed = []

    class Service:
        async def create(self, request):
            return SimpleNamespace(id=request.agent_type.value), SimpleNamespace(turn_id="review")

        async def wait_turn(self, record_id, turn_id, wait_seconds):
            started[record_id].set()
            await release[record_id].wait()
            finished[record_id].set()
            text = (
                '<goal-review>{"verdict":"PASS","summary":"verified","findings":[]}</goal-review>'
            )
            if record_id == candidates[-1].provider:
                if outcome == "ERROR":
                    text = "Invalid reviewer output"
                elif outcome == "BLOCK":
                    text = '<goal-review>{"verdict":"BLOCK","summary":"boundary defect","findings":[{"severity":"P1","summary":"Missing check"}]}</goal-review>'
            return SimpleNamespace(id=record_id), SimpleNamespace(
                status=SubagentTurnStatus.COMPLETED,
                final_text=text,
            )

        async def close(self, record_id, reason):
            closed.append(record_id)

    monkeypatch.setattr(subagents, "get_subagent_service", lambda: Service())

    async def inspect_candidate(goal, text):
        return "checkout@feature:reviewed-head", None

    monkeypatch.setattr(controller_module, "inspect_push_candidate", inspect_candidate)
    sent = []

    async def dispatch(goal, prompt):
        sent.append(prompt)
        return goal.pending_step_id

    ctrl = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch)
    goal = await ctrl.create_and_start(
        "tab",
        GoalRunCreate(
            objective="review candidate",
            client_request_id="create",
            strict_review=True,
            strict_review_push_branch=True,
        ),
    )
    completion = asyncio.create_task(
        ctrl.on_turn_completed("tab", goal.current_turn_id, "completed", COMPLETE)
    )
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 5)
        await ctrl.steer(goal.id, "steer-one", "Report progress first", "user-one")
        if outcome == "CANCEL":
            stopped = await ctrl.pause(goal.id, "stop")
            await asyncio.wait_for(completion, 5)
            assert stopped.status == GoalRunStatus.PAUSED
            assert set(closed) == set(started)
            assert len(sent) == 1
            assert not stopped.strict_review_in_progress
            return
        await ctrl.steer(goal.id, "steer-two", "Then update the summary", "user-two")
        assert not completion.done()
        assert len(sent) == 1
        for event in release.values():
            event.set()
        result = await asyncio.wait_for(completion, 5)
        assert result.status == GoalRunStatus.ACTIVE
        assert not result.strict_review_push_pending
        assert not closed
        assert len(sent) == 2
        assert sent[-1].count("Report progress first") == 1
        assert sent[-1].count("Then update the summary") == 1
        assert "Do not push yet" in sent[-1]
        assert result.strict_review_history[-1].verdict == (
            "PASS" if outcome == "PASS" else "BLOCK"
        )
        if outcome == "ERROR":
            assert "could not complete" in sent[-1]
        if outcome == "PASS":
            assert "A PASS covers the reviewed candidate" in sent[-1]
    finally:
        for event in release.values():
            event.set()
        await completion


@pytest.mark.asyncio
async def test_changed_push_target_at_cap_still_delivers_queued_steer(tmp_path, monkeypatch):
    from claude_hub.models.goal_run import GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS
    from claude_hub.services.goal_run import controller as controller_module

    calls = 0

    async def inspect_candidate(goal, text):
        nonlocal calls
        calls += 1
        return f"checkout@feature:head-{calls}", None

    monkeypatch.setattr(controller_module, "inspect_push_candidate", inspect_candidate)
    harness = ReviewHarness(tmp_path / "goals.json")
    goal, completion = await harness.start(push=True)
    try:
        # The first candidate inspection happens before the callback; arrange
        # a changed post-review candidate without replacing lifecycle logic.
        reviewing = harness.controller.get(goal.id)
        reviewing.strict_review_push_attempts = GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS - 1
        harness.controller.store.put(reviewing)
        await harness.controller.steer(goal.id, "steer", "Inspect this before any push", "user")
        harness.release.set()
        result = await asyncio.wait_for(completion, 5)
        assert result.status == GoalRunStatus.ACTIVE
        assert not result.strict_review_push_pending
        assert "Inspect this before any push" in harness.sent[-1][1]
        assert "returned PASS" in harness.sent[-1][1]
    finally:
        harness.release.set()
        await completion
