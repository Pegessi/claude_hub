"""Lifecycle controller for Hub-managed Chat Goals."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from ...models.chat_image import ChatImageAttachment
from ...models.goal_run import (
    GOAL_CHECKPOINT_HISTORY_LIMIT,
    GOAL_RECENT_TURN_IDS_LIMIT,
    GOAL_STEER_INPUT_MAX_LENGTH,
    GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS,
    GOAL_STRICT_REVIEW_HISTORY_LIMIT,
    GOAL_STRICT_REVIEW_MAX_ROUNDS,
    GoalCheckpoint,
    GoalDispatchState,
    GoalPlan,
    GoalRun,
    GoalRunCreate,
    GoalRunStatus,
    GoalStrictReviewRound,
    GoalTurnUsage,
    GoalUsageQuality,
    utc_now,
)
from ..goal_signals import parse_goal_signal
from .images import (
    combine_review_images,
    goal_image_input,
    image_fingerprint,
    pending_goal_images,
)
from .native import NativeGoalCoordinator
from .policy import GoalPolicyError, can_complete_protocol_failure, ensure_unfinished
from .prompts import build_dispatch_prompt
from .store import GoalRunStore
from .strict_review import (
    StrictReviewerCallback,
    StrictReviewOutcome,
    block_unverifiable_push,
    evaluate_review_continuation,
    format_strict_review_feedback,
    inspect_push_candidate,
)
from .strict_review import invalidate_push_authorization as _invalidate_push_authorization
from .strict_review import (
    run_subagent_strict_review,
)

DispatchCallback = Callable[[GoalRun, str], Awaitable[str | None] | str | None]
CancelCallback = Callable[[GoalRun], Awaitable[None] | None]
_CHECKPOINT_RE = re.compile(
    r"<goal-checkpoint>\s*(\{.*?\})\s*</goal-checkpoint>\s*(?=<goal-status\b)",
    re.IGNORECASE | re.DOTALL,
)
_MAX_CHECKPOINT_JSON_CHARS = 64 * 1024
_PLAN_RE = re.compile(
    r"<goal-plan>\s*(\{.*?\})\s*</goal-plan>",
    re.IGNORECASE | re.DOTALL,
)
_MAX_PLAN_JSON_CHARS = 16 * 1024


def _append_pending_input(pending: str | None, text: str) -> str:
    combined = (
        pending + "\n\n--- Next user message ---\n\n" + text
        if pending and text
        else pending or text
    )
    if len(combined) > GOAL_STEER_INPUT_MAX_LENGTH:
        raise ValueError(
            f"Queued Goal steer messages are limited to {GOAL_STEER_INPUT_MAX_LENGTH} characters"
        )
    return combined


def _parse_checkpoint(assistant_text: str, turn_id: str) -> GoalCheckpoint | None:
    start = assistant_text.lower().rfind("<goal-checkpoint>")
    match = _CHECKPOINT_RE.match(assistant_text[start:]) if start >= 0 else None
    if match is None:
        return None
    raw = match.group(1)
    if len(raw.encode("utf-8")) > _MAX_CHECKPOINT_JSON_CHARS:
        raise ValueError("goal checkpoint exceeds 64 KiB")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("goal checkpoint must be a JSON object")
    if "turn_id" in payload or "created_at" in payload:
        raise ValueError("goal checkpoint metadata is Hub-managed")
    payload["turn_id"] = turn_id
    payload["created_at"] = utc_now()
    return GoalCheckpoint.model_validate(payload)


def _parse_plan(assistant_text: str) -> GoalPlan | None:
    """Read the optional machine-readable plan block from a bounded tail.

    Absent means "no plan"; invalid means the block was ignored. Either way the
    turn's prose and checkpoint still stand, so a malformed plan can never
    silently authorize work the user did not approve.
    """
    match = _PLAN_RE.search(assistant_text)
    if match is None:
        return None
    raw = match.group(1)
    if len(raw.encode("utf-8")) > _MAX_PLAN_JSON_CHARS:
        raise ValueError("goal plan exceeds 16 KiB")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("goal plan must be a JSON object")
    return GoalPlan.model_validate(payload)


class GoalRunController:
    def __init__(
        self,
        store: GoalRunStore,
        dispatch: DispatchCallback | None = None,
        cancel: CancelCallback | None = None,
        strict_reviewer: StrictReviewerCallback | None = None,
    ) -> None:
        self.store = store
        self.native = NativeGoalCoordinator(store)
        self.dispatch = dispatch
        self.cancel = cancel
        self.strict_reviewer: StrictReviewerCallback | None = strict_reviewer
        self._locks: dict[str, asyncio.Lock] = {}
        self._mutation_locks: dict[str, asyncio.Lock] = {}
        # Claim each durable step once. Cancellation waits only for provider
        # acceptance, never for the HTTP/observer task that called dispatch.
        self._acceptances: dict[str, asyncio.Future[None]] = {}
        self._strict_review_tasks: dict[str, asyncio.Task[Any]] = {}
        self._review_steer_images: dict[str, list[ChatImageAttachment]] = {}

    def _lock_for(self, goal_id: str) -> asyncio.Lock:
        return self._locks.setdefault(goal_id, asyncio.Lock())

    def _mutation_lock_for(self, goal_id: str) -> asyncio.Lock:
        return self._mutation_locks.setdefault(goal_id, asyncio.Lock())

    def set_dispatch_callback(self, dispatch: DispatchCallback | None) -> None:
        self.dispatch = dispatch

    def set_cancel_callback(self, cancel: CancelCallback | None) -> None:
        self.cancel = cancel

    def set_strict_reviewer_callback(self, reviewer: StrictReviewerCallback | None) -> None:
        self.strict_reviewer = reviewer

    def create(self, tab_id: str, request: GoalRunCreate) -> GoalRun:
        return self.store.create(
            GoalRun(
                tab_id=tab_id,
                objective=request.objective,
                pending_image_count=len(request.attachments),
                pending_image_fingerprint=image_fingerprint(request.attachments),
                plan_first=request.plan_first,
                # A ``plan_first`` start spends its first turn on the plan.
                plan_pending=request.plan_first,
                **request.model_dump(
                    include={
                        "strict_review",
                        "strict_review_preset",
                        "strict_review_mode",
                        "strict_review_candidates",
                        "strict_review_briefing",
                        "strict_review_mr_description",
                        "strict_review_push_branch",
                    }
                ),
            ),
            request.client_request_id,
            request,
        )

    async def create_and_start(self, tab_id: str, request: GoalRunCreate) -> GoalRun:
        native = await self.native.create(tab_id, request)
        if native is not None:
            return native
        goal = self.create(tab_id, request)
        async with self._lock_for(goal.id):
            goal = self.store.get(goal.id)
            if (
                goal.status == GoalRunStatus.ACTIVE
                and goal.dispatch_state == GoalDispatchState.IDLE
            ):
                goal = self._prepare_dispatch_locked(goal.id)
        if goal.dispatch_state == GoalDispatchState.PENDING:
            return await self._dispatch_prepared(goal)
        return goal

    def get(self, goal_id: str) -> GoalRun:
        return self.store.get(goal_id)

    def current(self, tab_id: str) -> GoalRun | None:
        return self.store.current_for_tab(tab_id)

    async def refresh_current(self, tab_id: str) -> GoalRun | None:
        _, goal = await self.native.refresh(tab_id)
        return goal

    def has_goal_history(self, tab_id: str) -> bool:
        return self.store.has_goal_history(tab_id)

    def replay_create(self, tab_id: str, request: GoalRunCreate) -> GoalRun | None:
        return self.store.replay_create(tab_id, request)

    def _mutate_once(
        self,
        goal_id: str,
        client_request_id: str,
        operation: str,
        mutate: Callable[[GoalRun], None],
    ) -> GoalRun:
        goal = self.store.get(goal_id)
        prior = goal.idempotency.get(client_request_id)
        if prior is not None:
            if prior != operation:
                raise GoalPolicyError("client_request_id was already used for another mutation")
            return goal
        mutate(goal)
        goal.idempotency[client_request_id] = operation
        return self.store.put(goal)

    async def _cancel_strict_review(self, goal_id: str) -> None:
        task = self._strict_review_tasks.pop(goal_id, None)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _cancel_current(self, goal: GoalRun) -> GoalRun:
        was_reviewing = goal.strict_review_in_progress
        self._review_steer_images.pop(goal.id, None)
        await self._cancel_strict_review(goal.id)
        if was_reviewing:
            async with self._lock_for(goal.id):
                latest = self.store.get(goal.id)
                changed = False
                if latest.strict_review_in_progress:
                    latest.strict_review_in_progress = False
                    latest.strict_review_subagent_id = None
                    changed = True
                if (
                    latest.dispatch_state == GoalDispatchState.DISPATCHED
                    and latest.current_turn_id is None
                ):
                    latest.dispatch_state = GoalDispatchState.IDLE
                    changed = True
                if latest.status_message and latest.status_message.startswith(
                    "Running strict cross-model review"
                ):
                    latest.status_message = "strict cross-model review was stopped"
                    changed = True
                goal = self.store.put(latest) if changed else latest
        if goal.current_turn_id is None and goal.dispatch_state not in {
            GoalDispatchState.PENDING,
            GoalDispatchState.DISPATCHED,
            GoalDispatchState.UNCERTAIN,
        }:
            return goal
        if self.cancel is None:
            goal.current_turn_id = None
            goal.dispatch_state = GoalDispatchState.IDLE
            goal.pending_step_id = None
            return self.store.put(goal)
        try:
            acceptance = self._acceptances.get(goal.pending_step_id or "")
            if acceptance is not None:
                await asyncio.shield(acceptance)
            latest = self.store.get(goal.id)
            result = self.cancel(latest.model_copy(deep=True))
            if inspect.isawaitable(result):
                await result
        except (Exception, asyncio.CancelledError) as exc:
            latest = self.store.get(goal.id)
            latest.dispatch_state = GoalDispatchState.UNCERTAIN
            latest.status_message = f"turn cancellation failed: {exc}"
            if isinstance(exc, asyncio.CancelledError):
                self.store.put(latest)
                raise
            return self.store.put(latest)
        latest = self.store.get(goal.id)
        latest.current_turn_id = None
        latest.dispatch_state = GoalDispatchState.IDLE
        latest.pending_step_id = None
        return self.store.put(latest)

    async def pause(self, goal_id: str, client_request_id: str) -> GoalRun:
        native = await self.native.mutate(goal_id, client_request_id, "pause")
        if native is not None:
            return native
        async with self._mutation_lock_for(goal_id):
            async with self._lock_for(goal_id):
                goal = self._pause_locked(goal_id, client_request_id)
            # Replaying a pause after resume must not cancel the newer turn.
            if goal.status != GoalRunStatus.PAUSED:
                return goal
            return await self._cancel_current(goal)

    def _pause_locked(self, goal_id: str, client_request_id: str) -> GoalRun:
        existing = self.store.get(goal_id)
        replay = existing.idempotency.get(client_request_id)
        if replay is not None:
            if replay != "pause":
                raise GoalPolicyError("client_request_id was already used for another mutation")
            return existing

        def apply(goal: GoalRun) -> None:
            ensure_unfinished(goal)
            _invalidate_push_authorization(goal)
            goal.status = GoalRunStatus.PAUSED
            goal.paused_at = utc_now()
            goal.resume_after_turn = False

        return self._mutate_once(goal_id, client_request_id, "pause", apply)

    async def resume(
        self,
        goal_id: str,
        client_request_id: str,
        *,
        after_current_turn: bool = False,
        step_id: str | None = None,
        dispatch: DispatchCallback | None = None,
    ) -> GoalRun:
        """Resume now, optionally scoping one dispatch to a caller-owned step."""
        existing = self.store.get(goal_id)
        handled, current = await self.native.refresh(existing.tab_id)
        if handled:
            if step_id is not None or dispatch is not None:
                # Provider thread recovery owns its continuation; never add a Hub turn.
                return current or self.store.get(goal_id)
            native = await self.native.mutate(goal_id, client_request_id, "resume")
            assert native is not None
            return native
        if after_current_turn and (step_id is not None or dispatch is not None):
            raise GoalPolicyError("A fixed dispatch requires an immediate Goal resume")
        if dispatch is not None and step_id is None:
            raise GoalPolicyError("A dispatch override requires a fixed step_id")
        if step_id is not None and not step_id.strip():
            raise GoalPolicyError("step_id must not be empty")
        async with self._mutation_lock_for(goal_id):
            existing = self.store.get(goal_id)
            if existing.pending_image_count:
                raise GoalPolicyError(
                    "Image delivery interrupted. Reattach images and steer the Goal to continue."
                )
            if client_request_id not in existing.idempotency:
                ensure_unfinished(existing)
                if (
                    existing.status != GoalRunStatus.ACTIVE
                    and existing.dispatch_state != GoalDispatchState.IDLE
                ):
                    # The provider may call the completion observer while we
                    # await cancellation; never hold its state lock here.
                    reconciled = await self._cancel_current(existing)
                    if reconciled.dispatch_state != GoalDispatchState.IDLE:
                        raise GoalPolicyError(
                            reconciled.status_message or "Goal turn could not be reconciled"
                        )
            async with self._lock_for(goal_id):
                goal = self._resume_locked(
                    goal_id,
                    client_request_id,
                    after_current_turn,
                    step_id=step_id,
                    dispatch=dispatch,
                )
        if goal.dispatch_state == GoalDispatchState.PENDING:
            # A replay must not apply its one-shot callback to a later Goal step.
            if step_id is not None and goal.pending_step_id != step_id:
                return goal
            return await self._dispatch_prepared(goal, dispatch=dispatch)
        return goal

    def _resume_locked(
        self,
        goal_id: str,
        client_request_id: str,
        after_current_turn: bool = False,
        *,
        step_id: str | None = None,
        dispatch: DispatchCallback | None = None,
    ) -> GoalRun:
        existing = self.store.get(goal_id)
        replay = existing.idempotency.get(client_request_id)
        if replay is not None:
            if replay != "resume":
                raise GoalPolicyError("client_request_id was already used for another mutation")
            return existing

        def apply(goal: GoalRun) -> None:
            ensure_unfinished(goal)
            if (
                goal.status == GoalRunStatus.BLOCKED
                or goal.strict_review_push_attempts >= GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS
            ):
                goal.strict_review_push_attempts = 0
            if goal.strict_review and (
                goal.status == GoalRunStatus.BLOCKED
                or goal.strict_review_rounds >= goal.strict_review_max_rounds
            ):
                goal.strict_review_cycle += 1
                goal.strict_review_rounds = 0
                goal.strict_review_max_rounds = GOAL_STRICT_REVIEW_MAX_ROUNDS
                goal.strict_review_feedback = None
                goal.strict_review_history = goal.strict_review_history[
                    -GOAL_STRICT_REVIEW_HISTORY_LIMIT:
                ]
            goal.status = GoalRunStatus.ACTIVE
            goal.paused_at = None
            deferred = after_current_turn and goal.dispatch_state == GoalDispatchState.IDLE
            goal.resume_after_turn = deferred
            goal.status_message = (
                "resumes when the current Chat turn finishes" if deferred else None
            )

        goal = self._mutate_once(goal_id, client_request_id, "resume", apply)
        if goal.dispatch_state != GoalDispatchState.IDLE or goal.resume_after_turn:
            return goal
        return self._prepare_dispatch_locked(goal.id, step_id=step_id, dispatch=dispatch)

    async def steer(
        self,
        goal_id: str,
        client_request_id: str,
        text: str,
        turn_id: str,
        image_count: int = 0,
        image_digest: str = "",
    ) -> GoalRun:
        """Deliver user input as the next Goal turn without leaving Goal mode.

        Strict review queues input without interruption. Otherwise, a running
        Goal turn is interrupted first. The Goal is parked as paused
        while that happens so the interrupted turn's completion cannot pause or
        advance it; the input is persisted before cancellation and survives an
        uncertain stop for an explicit resume.
        """
        if len(text) > GOAL_STEER_INPUT_MAX_LENGTH:
            raise ValueError(
                f"Goal steer messages are limited to {GOAL_STEER_INPUT_MAX_LENGTH} characters"
            )
        native = await self.native.mutate(
            goal_id,
            client_request_id,
            "steer",
            text=text,
            turn_id=turn_id,
            image_count=image_count,
            image_digest=image_digest,
        )
        if native is not None:
            return native
        operation = "steer:" + hashlib.sha256((text + "\0" + image_digest).encode()).hexdigest()
        async with self._mutation_lock_for(goal_id):
            async with self._lock_for(goal_id):
                existing = self.store.get(goal_id)
                replay = existing.idempotency.get(client_request_id)
                if replay is not None:
                    if replay not in {"steer", operation, "review-" + operation}:
                        raise GoalPolicyError(
                            "client_request_id was already used for another mutation"
                        )
                    return existing

                if existing.strict_review_in_progress and existing.status == GoalRunStatus.ACTIVE:
                    # No await between validation, persistence and retaining the
                    # originals: the review cannot finish halfway through enqueue.
                    combined_text = _append_pending_input(existing.pending_input, text)
                    incoming = pending_goal_images(
                        existing.model_copy(
                            update={
                                "pending_image_count": image_count,
                                "pending_image_fingerprint": image_digest,
                            }
                        )
                    )
                    images = combine_review_images(
                        self._review_steer_images.get(goal_id, []), incoming
                    )
                    existing.pending_input = combined_text
                    existing.pending_review_steer = True
                    existing.pending_review_recovery_digest = ""
                    existing.pending_image_count = len(images)
                    existing.pending_image_fingerprint = image_fingerprint(images)
                    existing.review_steer_turn_ids = (existing.review_steer_turn_ids + [turn_id])[
                        -GOAL_RECENT_TURN_IDS_LIMIT:
                    ]
                    existing.status_message = (
                        "User input queued; waiting for strict cross-model review to finish"
                    )
                    existing.idempotency[client_request_id] = "review-" + operation
                    queued = self.store.put(existing)
                    if images:
                        self._review_steer_images[goal_id] = images
                    return queued

                def apply(goal: GoalRun) -> None:
                    ensure_unfinished(goal)
                    if goal.status not in {
                        GoalRunStatus.ACTIVE,
                        GoalRunStatus.BLOCKED,
                    } and not (goal.status == GoalRunStatus.PAUSED and goal.pending_image_count):
                        raise GoalPolicyError("Resume the Goal before steering it")
                    recovering_review = (
                        goal.status == GoalRunStatus.PAUSED
                        and goal.pending_image_count
                        and goal.pending_review_steer
                    )
                    recovery_digest = hashlib.sha256(text.encode()).hexdigest() if text else ""
                    if recovering_review:
                        pending_input = (
                            goal.pending_input
                            if recovery_digest
                            and recovery_digest == goal.pending_review_recovery_digest
                            else _append_pending_input(goal.pending_input, text)
                        )
                        if recovery_digest:
                            goal.pending_review_recovery_digest = recovery_digest
                    else:
                        pending_input = text
                        goal.pending_review_recovery_digest = ""
                    was_blocked = goal.status == GoalRunStatus.BLOCKED
                    goal.pending_review_steer = bool(recovering_review)
                    goal.strict_review_push_attempts = 0
                    push_was_pending = _invalidate_push_authorization(goal)
                    goal.status = GoalRunStatus.PAUSED
                    goal.paused_at = utc_now()
                    goal.status_message = "steering the Goal with a new message"
                    if goal.pending_image_count and not image_count:
                        raise GoalPolicyError(
                            "Reattach images before steering the interrupted Goal"
                        )
                    goal.pending_input = pending_input
                    goal.signal_recovery_attempts = 0
                    goal.signal_recovery_text = None
                    goal.pending_image_count = image_count
                    goal.pending_image_fingerprint = image_digest
                    goal.resume_after_turn = False
                    if goal.strict_review and (
                        (was_blocked and not push_was_pending)
                        or goal.strict_review_rounds >= goal.strict_review_max_rounds
                    ):
                        goal.strict_review_cycle += 1
                        goal.strict_review_rounds = 0
                        goal.strict_review_max_rounds = GOAL_STRICT_REVIEW_MAX_ROUNDS
                        goal.strict_review_feedback = None
                        goal.strict_review_history = goal.strict_review_history[
                            -GOAL_STRICT_REVIEW_HISTORY_LIMIT:
                        ]

                goal = self._mutate_once(goal_id, client_request_id, operation, apply)
            goal = await self._cancel_current(goal)
            if goal.dispatch_state != GoalDispatchState.IDLE:
                raise GoalPolicyError(goal.status_message or "Goal turn could not be reconciled")
            async with self._lock_for(goal_id):
                goal = self.store.get(goal_id)
                goal.status = GoalRunStatus.ACTIVE
                goal.paused_at = None
                goal.status_message = None
                self.store.put(goal)
                goal = self._prepare_dispatch_locked(goal_id, step_id=turn_id)
        if goal.dispatch_state == GoalDispatchState.PENDING:
            return await self._dispatch_prepared(goal)
        return goal

    async def update_objective(
        self, goal_id: str, client_request_id: str, objective: str
    ) -> GoalRun:
        native = await self.native.mutate(
            goal_id, client_request_id, "objective", objective=objective
        )
        if native is not None:
            return native
        operation = "objective:" + hashlib.sha256(objective.encode()).hexdigest()
        async with self._mutation_lock_for(goal_id):
            cancelled_push = False
            invalidated_push = False
            async with self._lock_for(goal_id):
                existing = self.store.get(goal_id)
                replay = existing.idempotency.get(client_request_id)
                if replay is not None:
                    if replay != operation:
                        raise GoalPolicyError(
                            "client_request_id was already used for another mutation"
                        )
                    return existing
                ensure_unfinished(existing)
                if existing.dispatch_state == GoalDispatchState.UNCERTAIN:
                    raise GoalPolicyError(
                        "Reconcile the current Goal turn before editing its objective"
                    )
                if existing.strict_review_push_pending and existing.objective != objective:
                    invalidated_push = _invalidate_push_authorization(existing)
                    if existing.dispatch_state != GoalDispatchState.IDLE:
                        cancelled_push = True
                        existing.status = GoalRunStatus.PAUSED
                        existing.paused_at = utc_now()
                    self.store.put(existing)
            if cancelled_push:
                existing = await self._cancel_current(existing)
                if existing.dispatch_state != GoalDispatchState.IDLE:
                    raise GoalPolicyError(
                        existing.status_message or "Goal turn could not be reconciled"
                    )
            async with self._lock_for(goal_id):

                def apply(goal: GoalRun) -> None:
                    ensure_unfinished(goal)
                    if goal.objective != objective:
                        goal.strict_review_push_attempts = 0
                        goal.objective = objective
                        goal.objective_revision += 1
                        goal.signal_recovery_attempts = 0
                        goal.signal_recovery_text = None
                        if goal.strict_review and not invalidated_push:
                            goal.strict_review_cycle += 1
                            goal.strict_review_rounds = 0
                            goal.strict_review_max_rounds = GOAL_STRICT_REVIEW_MAX_ROUNDS
                            goal.strict_review_feedback = None
                            goal.strict_review_history = goal.strict_review_history[
                                -GOAL_STRICT_REVIEW_HISTORY_LIMIT:
                            ]
                    if cancelled_push:
                        goal.status = GoalRunStatus.ACTIVE
                        goal.paused_at = None
                        goal.status_message = None

                goal = self._mutate_once(goal_id, client_request_id, operation, apply)
                if cancelled_push:
                    goal = self._prepare_dispatch_locked(goal_id)
            if cancelled_push and goal.dispatch_state == GoalDispatchState.PENDING:
                return await self._dispatch_prepared(goal)
            return goal

    async def complete(self, goal_id: str, client_request_id: str) -> GoalRun:
        native = await self.native.mutate(goal_id, client_request_id, "complete")
        if native is not None:
            return native
        async with self._mutation_lock_for(goal_id):
            async with self._lock_for(goal_id):
                goal = self._complete_locked(goal_id, client_request_id)
            return await self._cancel_current(goal)

    def _complete_locked(self, goal_id: str, client_request_id: str) -> GoalRun:
        existing = self.store.get(goal_id)
        replay = existing.idempotency.get(client_request_id)
        if replay is not None:
            if replay != "complete":
                raise GoalPolicyError("client_request_id was already used for another mutation")
            return existing

        def apply(goal: GoalRun) -> None:
            if can_complete_protocol_failure(goal):
                current = self.store.current_for_tab(goal.tab_id)
                if current is None or current.id != goal.id:
                    raise GoalPolicyError("only the current Goal can be completed manually")
            else:
                ensure_unfinished(goal)
            goal.status = GoalRunStatus.COMPLETE
            goal.status_message = (
                "Manually completed by user; automatic verification was not claimed"
            )
            goal.completed_at = utc_now()
            goal.resume_after_turn = False
            goal.signal_recovery_text = None

        return self._mutate_once(goal_id, client_request_id, "complete", apply)

    async def clear(self, goal_id: str, client_request_id: str) -> GoalRun:
        native = await self.native.mutate(goal_id, client_request_id, "clear")
        if native is not None:
            return native
        async with self._mutation_lock_for(goal_id):
            async with self._lock_for(goal_id):
                goal = self._clear_locked(goal_id, client_request_id)
            return await self._cancel_current(goal)

    def _clear_locked(self, goal_id: str, client_request_id: str) -> GoalRun:
        existing = self.store.get(goal_id)
        replay = existing.idempotency.get(client_request_id)
        if replay is not None:
            if replay != "clear":
                raise GoalPolicyError("client_request_id was already used for another mutation")
            return existing

        def apply(goal: GoalRun) -> None:
            goal.status = GoalRunStatus.CANCELLED
            goal.completed_at = utc_now()
            goal.resume_after_turn = False

        return self._mutate_once(goal_id, client_request_id, "clear", apply)

    @staticmethod
    def _usage(usage: GoalTurnUsage | dict[str, Any] | None) -> GoalTurnUsage:
        if usage is None:
            return GoalTurnUsage()
        if isinstance(usage, GoalTurnUsage):
            return usage
        total = usage.get("total_tokens", usage.get("total"))
        if total is None and isinstance(usage.get("usage"), dict):
            total = usage["usage"].get("total_tokens")
        quality = usage.get("quality", "exact" if total is not None else "unavailable")
        return GoalTurnUsage(total_tokens=total, quality=quality, raw=usage)

    async def on_turn_completed(
        self,
        tab_id: str,
        turn_id: str,
        status: str,
        assistant_text: str,
        usage: GoalTurnUsage | dict[str, Any] | None = None,
        *,
        retryable: bool = False,
        delivery_uncertain: bool = False,
    ) -> GoalRun | None:
        handled, native = await self.native.refresh(tab_id)
        if handled:
            if (
                native is not None
                and turn_id in native.provider_owned_turn_ids
                and turn_id not in native.completed_turn_ids
            ):
                native.completed_turn_ids.append(turn_id)
                native.completed_turn_ids = native.completed_turn_ids[-GOAL_RECENT_TURN_IDS_LIMIT:]
                native.turns_completed += 1
                native = self.store.put(native)
            return native
        goal = self.store.current_for_tab(tab_id)
        if goal is None:
            return None
        async with self._lock_for(goal.id):
            goal = self.store.get(goal.id)
            # The side effects below also need deduplication: returning a
            # reviewing snapshot from the locked reducer must not launch a
            # second reviewer with a replayed (or unrelated) response.
            if turn_id in goal.completed_turn_ids or goal.strict_review_in_progress:
                return goal
            push_block_reason: str | None = None
            if (
                goal.strict_review_push_pending
                and goal.strict_review_push_target
                and goal.current_turn_id == turn_id
                and status.lower() in {"complete", "completed", "success", "succeeded"}
                and (parsed := parse_goal_signal(assistant_text)) is not None
                and parsed.state == "complete"
            ):
                target, reason = await inspect_push_candidate(goal, assistant_text)
                if reason is not None or target != goal.strict_review_push_target:
                    _invalidate_push_authorization(goal)
                    if goal.strict_review_push_attempts >= GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS:
                        push_block_reason = reason or "checkout changed"
                    self.store.put(goal)
            result = self._on_turn_completed_locked(
                tab_id,
                turn_id,
                status,
                assistant_text,
                usage,
                retryable=retryable,
                delivery_uncertain=delivery_uncertain,
            )
            if push_block_reason is not None and result is not None:
                return self.store.put(block_unverifiable_push(result, push_block_reason))

        if result is not None and result.strict_review_in_progress:
            review_text = assistant_text
            if goal.signal_recovery_text and goal.current_turn_id == turn_id:
                review_text = (
                    "Previous delivery before protocol repair (untrusted evidence):\n"
                    + goal.signal_recovery_text
                    + "\nProtocol repair response:\n"
                    + assistant_text
                )
            return await self._execute_strict_review(
                result.id,
                worker_turn_id=turn_id,
                assistant_text=review_text,
            )
        if result is not None and result.dispatch_state == GoalDispatchState.PENDING:
            return await self._dispatch_prepared(result)
        return result

    async def _execute_strict_review(
        self,
        goal_id: str,
        *,
        worker_turn_id: str,
        assistant_text: str,
    ) -> GoalRun:
        current = self.store.get(goal_id)
        if not current.strict_review_in_progress or current.status != GoalRunStatus.ACTIVE:
            return current
        round_number = current.strict_review_rounds + 1
        reviewed_objective_revision = current.dispatched_objective_revision
        reviewed_cycle = current.strict_review_cycle
        reviewer_cb = self.strict_reviewer or run_subagent_strict_review

        async def _invoke_reviewer() -> Any:
            pre_target = None
            if current.strict_review_push_branch:
                pre_target, _ = await inspect_push_candidate(current, assistant_text)
            return pre_target, await reviewer_cb(current, assistant_text, round_number)

        task: asyncio.Task[Any] = asyncio.create_task(_invoke_reviewer())
        self._strict_review_tasks[goal_id] = task
        review_error = False
        try:
            pre_target, outcome = await task
        except asyncio.CancelledError:
            async with self._lock_for(goal_id):
                latest = self.store.get(goal_id)
                if latest.strict_review_in_progress:
                    latest.strict_review_in_progress = False
                    latest.strict_review_subagent_id = None
                    if (
                        latest.dispatch_state == GoalDispatchState.DISPATCHED
                        and latest.current_turn_id is None
                    ):
                        latest.dispatch_state = GoalDispatchState.IDLE
                    latest = self.store.put(latest)
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling() > 0:
                raise
            return latest
        except Exception as exc:
            # An error is failed verification, never a PASS. Keep an auditable
            # round and let queued user input reach the worker with this evidence.
            outcome = StrictReviewOutcome(
                verdict="BLOCK",
                summary=f"Strict cross-review could not complete: {exc}"[:2000],
                findings=[],
                reviewer_provider="unavailable",
            )
            review_error = True
        finally:
            if self._strict_review_tasks.get(goal_id) is task:
                self._strict_review_tasks.pop(goal_id, None)

        prepared: GoalRun | None = None
        async with self._lock_for(goal_id):
            latest = self.store.get(goal_id)
            if not latest.strict_review_in_progress or latest.status != GoalRunStatus.ACTIVE:
                return latest
            post_target: str | None = None
            skip_reason: str | None = None
            if latest.strict_review_push_branch and outcome.verdict == "PASS":
                post_target, skip_reason = await inspect_push_candidate(latest, assistant_text)
            stale_review = (
                latest.objective_revision != reviewed_objective_revision
                or latest.dispatched_objective_revision != latest.objective_revision
                or latest.strict_review_cycle != reviewed_cycle
            )
            changed_target = (
                latest.strict_review_push_branch
                and outcome.verdict == "PASS"
                and pre_target != post_target
            )
            retry_changed_target = changed_target and latest.pending_input is None
            if retry_changed_target and not stale_review:
                latest.strict_review_push_attempts += 1
                if latest.strict_review_push_attempts >= GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS:
                    latest.strict_review_subagent_id = None
                    return self.store.put(
                        block_unverifiable_push(latest, "checkout changed during review")
                    )
            if retry_changed_target or stale_review:
                # Objective or cycle changed while reviewer ran: do not charge the new
                # cycle's round counter or pollute its history with a stale verdict.
                latest.strict_review_in_progress = False
                latest.strict_review_subagent_id = None
                latest.strict_review_feedback = None
                latest.dispatch_state = GoalDispatchState.IDLE
                self.store.put(latest)
                prepared = self._prepare_dispatch_locked(latest.id)
            else:
                latest.strict_review_in_progress = False
                latest.strict_review_rounds = round_number
                latest.strict_review_subagent_id = outcome.reviewer_subagent_id
                round_record = GoalStrictReviewRound(
                    cycle=latest.strict_review_cycle,
                    round_number=round_number,
                    worker_turn_id=worker_turn_id,
                    reviewer_subagent_id=outcome.reviewer_subagent_id,
                    reviewer_provider=outcome.reviewer_provider,
                    reviewer_model=outcome.reviewer_model,
                    verdict="PASS" if outcome.verdict == "PASS" else "BLOCK",
                    summary=outcome.summary,
                    findings=outcome.findings,
                )
                latest.strict_review_history.append(round_record)
                latest.strict_review_history = latest.strict_review_history[
                    -GOAL_STRICT_REVIEW_HISTORY_LIMIT:
                ]
                latest.dispatch_state = GoalDispatchState.IDLE
                if latest.pending_input is not None:
                    latest.strict_review_feedback = format_strict_review_feedback(round_record)
                    latest.status_message = (
                        "Strict cross-review finished; addressing queued user input"
                    )
                    # A queued human steer takes precedence over automatic PASS
                    # completion or BLOCK parking, including the last review round.
                    # Start a fresh budget only when this cycle has exhausted it.
                    if round_number >= latest.strict_review_max_rounds:
                        latest.strict_review_cycle += 1
                        latest.strict_review_rounds = 0
                        latest.strict_review_max_rounds = GOAL_STRICT_REVIEW_MAX_ROUNDS
                    self.store.put(latest)
                    prepared = self._prepare_dispatch_locked(latest.id)
                elif review_error:
                    latest.status = GoalRunStatus.BLOCKED
                    latest.status_message = round_record.summary
                    return self.store.put(latest)
                elif (
                    round_record.verdict == "PASS"
                    and latest.strict_review_push_branch
                    and not skip_reason
                ):
                    if latest.strict_review_push_attempts >= GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS:
                        return self.store.put(
                            block_unverifiable_push(latest, "automatic push retry limit reached")
                        )
                    latest.strict_review_push_attempts += 1
                    latest.strict_review_feedback = None
                    latest.strict_review_push_pending = True
                    latest.strict_review_push_target = post_target
                    latest.status_message = "Review passed; preparing the feature branch push"
                    if self.dispatch is None:
                        _invalidate_push_authorization(latest)
                        latest.status = GoalRunStatus.PAUSED
                        latest.status_message = (
                            "Review passed; automatic push continuation is not connected"
                        )
                        return self.store.put(latest)
                    self.store.put(latest)
                    prepared = self._prepare_dispatch_locked(latest.id)
                elif round_record.verdict == "PASS":
                    latest.strict_review_feedback = None
                    latest.status = GoalRunStatus.COMPLETE
                    latest.completed_at = utc_now()
                    latest.status_message = (
                        f"Passed strict cross-review (round {round_number}, "
                        f"{round_record.reviewer_provider}): {round_record.summary[:240]}"
                    )
                    if skip_reason:
                        latest.status_message += f"; automatic push skipped: {skip_reason}"
                    return self.store.put(latest)
                else:
                    active_cycle_history = [
                        item
                        for item in latest.strict_review_history
                        if item.cycle == latest.strict_review_cycle
                    ]
                    decision = evaluate_review_continuation(
                        active_cycle_history,
                        base_max_rounds=latest.strict_review_max_rounds,
                        hard_max_rounds=GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS,
                    )
                    latest.strict_review_feedback = format_strict_review_feedback(round_record)
                    if not decision.should_continue:
                        latest.status = GoalRunStatus.BLOCKED
                        if decision.reason == "impasse":
                            latest.status_message = (
                                f"Strict cross-review stopped early on round {round_number}/"
                                f"{latest.strict_review_max_rounds} due to a recurring blocker "
                                f"impasse ({round_record.reviewer_provider}): "
                                f"{round_record.summary[:200]} — steer the Goal or complete manually"
                            )
                        else:
                            latest.status_message = (
                                f"Strict cross-review blocked after {round_number}/"
                                f"{latest.strict_review_max_rounds} rounds: "
                                f"{round_record.summary[:240]}"
                            )
                        return self.store.put(latest)

                    if decision.reason == "converging_grace":
                        latest.strict_review_max_rounds = decision.effective_max_rounds
                        latest.status_message = (
                            f"Cross-review round {round_number} blockers are converging; "
                            f"granted grace round {round_number + 1}/"
                            f"{latest.strict_review_max_rounds} ({round_record.reviewer_provider})…"
                        )
                    else:
                        latest.status_message = (
                            f"Cross-review round {round_number} requested fixes "
                            f"({round_record.reviewer_provider}); continuing…"
                        )
                    if self.dispatch is None:
                        latest.status = GoalRunStatus.PAUSED
                        latest.status_message = "automatic continuation is not connected"
                        return self.store.put(latest)
                    self.store.put(latest)
                    prepared = self._prepare_dispatch_locked(latest.id)
        if prepared is not None and prepared.dispatch_state == GoalDispatchState.PENDING:
            return await self._dispatch_prepared(prepared)
        return self.store.get(goal_id)

    def _on_turn_completed_locked(
        self,
        tab_id: str,
        turn_id: str,
        status: str,
        assistant_text: str,
        usage: GoalTurnUsage | dict[str, Any] | None = None,
        *,
        retryable: bool = False,
        delivery_uncertain: bool = False,
    ) -> GoalRun | None:
        goal = self.store.current_for_tab(tab_id)
        if goal is None or turn_id in goal.completed_turn_ids:
            return goal
        if goal.status != GoalRunStatus.ACTIVE:
            return goal
        if goal.resume_after_turn and goal.dispatch_state == GoalDispatchState.IDLE:
            # Remember ordinary boundaries too, without charging Goal usage or
            # turn counts. A late duplicate must not release a newer deferral.
            goal.completed_turn_ids.append(turn_id)
            goal.completed_turn_ids = goal.completed_turn_ids[-GOAL_RECENT_TURN_IDS_LIMIT:]
            if status.lower() not in {"complete", "completed", "success", "succeeded"}:
                goal.status = GoalRunStatus.PAUSED
                goal.paused_at = utc_now()
                goal.resume_after_turn = False
                goal.status_message = (
                    f"current Chat turn ended with status {status}; resume the Goal to continue"
                )
                return self.store.put(goal)
            return self._release_deferred_resume_locked(goal)
        if (
            goal.dispatch_state not in {GoalDispatchState.PENDING, GoalDispatchState.DISPATCHED}
            or goal.current_turn_id != turn_id
        ):
            return goal

        goal.completed_turn_ids.append(turn_id)
        # Keep recent IDs for UI reconciliation without growing the snapshot
        # forever. Older callbacks are still rejected by current_turn_id above.
        goal.completed_turn_ids = goal.completed_turn_ids[-GOAL_RECENT_TURN_IDS_LIMIT:]
        goal.turns_completed += 1
        goal.pending_input = None
        goal.pending_review_steer = False
        goal.pending_review_recovery_digest = ""
        goal.pending_image_count = 0
        goal.pending_image_fingerprint = ""
        goal.current_turn_id = None
        goal.dispatch_state = GoalDispatchState.IDLE
        goal.pending_step_id = None
        turn_usage = self._usage(usage)
        previous_quality = goal.usage_quality
        if turn_usage.total_tokens is not None:
            goal.token_usage = (goal.token_usage or 0) + turn_usage.total_tokens
            ranks = {
                GoalUsageQuality.EXACT: 0,
                GoalUsageQuality.ESTIMATED: 1,
                GoalUsageQuality.UNAVAILABLE: 2,
            }
            if goal.turns_completed == 1 and previous_quality == GoalUsageQuality.UNAVAILABLE:
                goal.usage_quality = turn_usage.quality
            else:
                goal.usage_quality = max(
                    (previous_quality, turn_usage.quality), key=lambda item: ranks[item]
                )
        else:
            goal.usage_quality = GoalUsageQuality.UNAVAILABLE

        if status.lower() not in {"complete", "completed", "success", "succeeded"}:
            _invalidate_push_authorization(goal)
        if status.lower() == "cancelled":
            goal.status = GoalRunStatus.PAUSED
            goal.paused_at = utc_now()
            goal.status_message = "current Goal turn was stopped; resume to continue"
            return self.store.put(goal)
        if status.lower() not in {"complete", "completed", "success", "succeeded"}:
            if delivery_uncertain or retryable:
                # A failed observation is not proof that remote work failed.
                # Reuse explicit resume/reconciliation, never replay a prompt
                # automatically while the provider may still be executing it.
                goal.status = GoalRunStatus.PAUSED
                goal.paused_at = utc_now()
                if delivery_uncertain:
                    goal.current_turn_id = turn_id
                    goal.dispatch_state = GoalDispatchState.UNCERTAIN
                    goal.status_message = (
                        "turn delivery is uncertain; inspect remote execution before resuming"
                    )
                else:
                    goal.status_message = "turn failed recoverably; resume the Goal to retry"
                return self.store.put(goal)
            goal.status = GoalRunStatus.FAILED
            goal.status_message = f"turn ended with status {status}"
            goal.completed_at = utc_now()
            return self.store.put(goal)

        parsed_signal = parse_goal_signal(assistant_text)
        if parsed_signal is None or parsed_signal.state != "complete":
            invalidated = _invalidate_push_authorization(goal)
            if (
                invalidated
                and goal.strict_review_push_attempts >= GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS
                and (parsed_signal is None or parsed_signal.state not in {"blocked", "needs_input"})
            ):
                return self.store.put(
                    block_unverifiable_push(goal, "push continuation did not complete")
                )
        if parsed_signal is None and goal.dispatched_objective_revision != goal.objective_revision:
            goal.signal_recovery_attempts = 0
            goal.signal_recovery_text = None
            self.store.put(goal)
            return self._prepare_dispatch_locked(goal.id)
        if parsed_signal is None:
            return self._recover_missing_signal_locked(goal, assistant_text)
        protocol_tail = parsed_signal.protocol_text
        goal.signal_recovery_attempts = 0
        goal.signal_recovery_text = None

        # The planning turn is spent once it completes successfully; the next
        # turn implements. A cancelled or failed turn keeps the marker so the
        # retried turn still plans.
        planning_turn = goal.plan_pending and goal.plan_turn_id == turn_id
        if planning_turn:
            goal.plan_pending = False

        try:
            checkpoint = _parse_checkpoint(protocol_tail, turn_id)
        except (
            ValueError,
            TypeError,
            json.JSONDecodeError,
            RecursionError,
            MemoryError,
        ) as exc:
            goal.checkpoint_warning = f"ignored invalid checkpoint: {exc}"
        else:
            if checkpoint is None:
                goal.checkpoint_warning = "assistant response omitted a valid goal-checkpoint"
            else:
                goal.checkpoint = checkpoint
                goal.checkpoint_history.append(checkpoint)
                goal.checkpoint_history = goal.checkpoint_history[-GOAL_CHECKPOINT_HISTORY_LIMIT:]
                goal.checkpoint_warning = None

        signal, message = parsed_signal.state, parsed_signal.message
        goal.status_message = message

        # A plan block is accepted only when it belongs to the turn the Hub asked
        # to plan, or when this turn really parks for the user's input: that is
        # the agent asking for approval again (a revision), and the Goal then
        # blocks like any other needs_input turn. An implementing turn that
        # merely echoes the plan keeps running and must not rewrite the approved
        # scope or re-open its approval.
        planned_now = False
        if goal.plan_first and (planning_turn or signal == "needs_input"):
            try:
                plan = _parse_plan(protocol_tail)
            except (
                ValueError,
                TypeError,
                json.JSONDecodeError,
                RecursionError,
                MemoryError,
            ) as exc:
                goal.plan_warning = f"ignored invalid plan: {exc}"
            else:
                if plan is not None:
                    goal.plan = plan
                    goal.plan_turn_id = turn_id
                    # A revised plan needs approval again, on its own turn.
                    goal.plan_answered = False
                    goal.plan_warning = None
                    planned_now = True
        if goal.dispatched_objective_revision != goal.objective_revision:
            _invalidate_push_authorization(goal)
            if planning_turn or planned_now:
                # The edited objective has not been planned yet, so the
                # replacement turn must plan it instead of implementing the
                # previous scope.
                goal.plan_pending = True
            self.store.put(goal)
            return self._prepare_dispatch_locked(goal.id)
        if signal in {"blocked", "needs_input"}:
            goal.status = GoalRunStatus.BLOCKED
            return self.store.put(goal)
        if planning_turn or planned_now:
            # The Hub, not the model, owns this pause. A plan was produced and
            # the user has not answered it, so the Goal parks even if the model
            # asked to continue or complete: approving is what starts implementation.
            goal.status = GoalRunStatus.BLOCKED
            goal.status_message = "The plan awaits your approval before the Goal continues"
            return self.store.put(goal)
        if signal == "complete":
            if goal.strict_review_push_pending:
                goal.strict_review_push_pending = False
                goal.strict_review_push_target = None
                goal.status = GoalRunStatus.COMPLETE
                goal.completed_at = utc_now()
                goal.status_message = "Strict review passed; branch push continuation completed"
                return self.store.put(goal)
            if goal.strict_review:
                next_round = goal.strict_review_rounds + 1
                effective_limit = min(
                    GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS,
                    max(goal.strict_review_max_rounds, GOAL_STRICT_REVIEW_MAX_ROUNDS),
                )
                if next_round > effective_limit:
                    goal.status = GoalRunStatus.BLOCKED
                    goal.status_message = (
                        f"Strict cross-review reached the {effective_limit}-round "
                        "limit without consensus; inspect findings and steer or complete manually"
                    )
                    return self.store.put(goal)
                goal.strict_review_in_progress = True
                goal.dispatch_state = GoalDispatchState.DISPATCHED
                goal.status_message = (
                    f"Running strict cross-model review (round {next_round}/" f"{effective_limit})…"
                )
                return self.store.put(goal)
            goal.status = GoalRunStatus.COMPLETE
            goal.completed_at = utc_now()
            return self.store.put(goal)

        if self.dispatch is None:
            goal.status = GoalRunStatus.PAUSED
            goal.status_message = "automatic continuation is not connected"
            return self.store.put(goal)

        # Commit this turn and its accounting before the transport can observe
        # or act on the next durable dispatch intent.
        self.store.put(goal)
        return self._prepare_dispatch_locked(goal.id)

    def _recover_missing_signal_locked(self, goal: GoalRun, assistant_text: str) -> GoalRun:
        if not assistant_text.replace("\u200b", "").replace("\ufeff", "").strip():
            goal.status = GoalRunStatus.FAILED
            goal.status_message = "provider completed without an assistant response"
            goal.completed_at = utc_now()
        elif goal.signal_recovery_attempts or self.dispatch is None:
            goal.status = GoalRunStatus.BLOCKED
            goal.status_message = (
                "Goal status signal could not be recovered; inspect the delivery and "
                "provide input, resume, or complete manually"
            )
        else:
            goal.signal_recovery_attempts = 1
            goal.signal_recovery_text = assistant_text[-16_000:]
            goal.status_message = (
                "Recovering the missing Goal status signal (one read-only repair)…"
            )
            self.store.put(goal)
            return self._prepare_dispatch_locked(goal.id)
        return self.store.put(goal)

    def _release_deferred_resume_locked(self, goal: GoalRun) -> GoalRun:
        if not (
            goal.status == GoalRunStatus.ACTIVE
            and goal.resume_after_turn
            and goal.dispatch_state == GoalDispatchState.IDLE
        ):
            return goal
        goal.resume_after_turn = False
        goal.status_message = None
        self.store.put(goal)
        return self._prepare_dispatch_locked(goal.id)

    async def release_deferred_resume(self, goal_id: str) -> GoalRun:
        """Dispatch a deferred resume whose blocking Chat turn already ended."""
        async with self._lock_for(goal_id):
            goal = self._release_deferred_resume_locked(self.store.get(goal_id))
        if goal.dispatch_state == GoalDispatchState.PENDING:
            return await self._dispatch_prepared(goal)
        return goal

    async def dispatch_next(self, goal_id: str) -> GoalRun:
        """Persist dispatch intent, then invoke the injected transport callback."""
        async with self._lock_for(goal_id):
            goal = self._prepare_dispatch_locked(goal_id)
        if goal.dispatch_state == GoalDispatchState.PENDING:
            return await self._dispatch_prepared(goal)
        return goal

    def _prepare_dispatch_locked(
        self,
        goal_id: str,
        step_id: str | None = None,
        *,
        dispatch: DispatchCallback | None = None,
    ) -> GoalRun:
        goal = self.store.get(goal_id)
        if goal.execution_owner == "provider_native" or goal.status != GoalRunStatus.ACTIVE:
            return goal
        if goal.strict_review_in_progress or goal.dispatch_state != GoalDispatchState.IDLE:
            return goal
        if dispatch is None and self.dispatch is None:
            goal.status = GoalRunStatus.PAUSED
            goal.status_message = "automatic continuation is not connected"
            return self.store.put(goal)
        # A steer reuses the composer's turn id so its optimistic bubble
        # reconciles with the authoritative turn_started.
        goal.pending_step_id = step_id or str(uuid4())
        # The Hub-generated step id is also the provider turn id. Persist it
        # before crossing the transport boundary so a provider that completes
        # synchronously can be correlated while dispatch is still pending.
        goal.current_turn_id = goal.pending_step_id
        goal.dispatch_state = GoalDispatchState.PENDING
        goal.dispatched_objective_revision = goal.objective_revision
        if goal.plan_pending:
            # Key the plan card to the turn that produces or revises it before
            # the provider can complete synchronously.
            goal.plan_turn_id = goal.pending_step_id
        return self.store.put(goal)

    async def _dispatch_prepared(
        self, prepared: GoalRun, *, dispatch: DispatchCallback | None = None
    ) -> GoalRun:
        handled, native = await self.native.refresh(prepared.tab_id)
        if handled:
            return native or self.store.get(prepared.id)
        step_id = prepared.pending_step_id
        if prepared.dispatch_state != GoalDispatchState.PENDING or step_id is None:
            return self.store.get(prepared.id)
        # No await between checking and claiming: duplicates can observe this
        # step but cannot invoke the transport again.
        if step_id in self._acceptances:
            return self.store.get(prepared.id)
        acceptance = asyncio.get_running_loop().create_future()
        self._acceptances[step_id] = acceptance
        try:
            latest = self.store.get(prepared.id)
            if (
                latest.status != GoalRunStatus.ACTIVE
                or latest.dispatch_state != GoalDispatchState.PENDING
                or latest.pending_step_id != step_id
            ):
                return latest
            callback = self.dispatch if dispatch is None else dispatch
            assert callback is not None
            # Detach only this dispatch's batch. An earlier send may return
            # after its worker completed and a later review already queued images.
            images = self._review_steer_images.pop(prepared.id, None)
            if images is not None:
                with goal_image_input(prepared.tab_id, images):
                    result = callback(
                        prepared.model_copy(deep=True), build_dispatch_prompt(prepared)
                    )
                    next_turn_id = await result if inspect.isawaitable(result) else result
            else:
                result = callback(prepared.model_copy(deep=True), build_dispatch_prompt(prepared))
                next_turn_id = await result if inspect.isawaitable(result) else result
            async with self._lock_for(prepared.id):
                goal = self.store.get(prepared.id)
                if (
                    goal.dispatch_state != GoalDispatchState.PENDING
                    or goal.pending_step_id != step_id
                ):
                    return goal
                goal.current_turn_id = next_turn_id or step_id
                goal.dispatch_state = GoalDispatchState.DISPATCHED
                if (
                    goal.plan_turn_id is not None
                    and not goal.plan_pending
                    and prepared.pending_input is not None
                ):
                    # The plan's approval request is answered only once the turn
                    # carrying the user's own message is really dispatched. The
                    # Hub cannot tell approval from a change request without
                    # reading that prose, so it records "answered", and a plain
                    # resume or an automatic continuation stays unapproved.
                    goal.plan_answered = True
                if goal.pending_input == prepared.pending_input:
                    goal.pending_input = None
                    goal.pending_review_steer = False
                    goal.pending_review_recovery_digest = ""
                if goal.strict_review_feedback == prepared.strict_review_feedback:
                    goal.strict_review_feedback = None
                if goal.pending_image_fingerprint == prepared.pending_image_fingerprint:
                    goal.pending_image_count = 0
                    goal.pending_image_fingerprint = ""
                return self.store.put(goal)
        except (Exception, asyncio.CancelledError) as exc:
            async with self._lock_for(prepared.id):
                goal = self.store.get(prepared.id)
                # A completion observed while dispatch was awaiting the provider
                # is stronger evidence than the callback's eventual exception.
                if (
                    goal.dispatch_state == GoalDispatchState.PENDING
                    and goal.pending_step_id == step_id
                ):
                    _invalidate_push_authorization(goal)
                    if goal.status == GoalRunStatus.ACTIVE:
                        goal.status = GoalRunStatus.PAUSED
                    goal.status_message = f"continuation dispatch failed: {exc}"
                    goal.dispatch_state = GoalDispatchState.UNCERTAIN
                    self.store.put(goal)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return goal
        finally:
            self._acceptances.pop(step_id, None)
            acceptance.set_result(None)

    def recover(self) -> list[GoalRun]:
        """Fail closed for dispatches whose provider acceptance is uncertain."""
        recovered: list[GoalRun] = []
        for goal in self.store.list():
            if goal.execution_owner == "provider_native":
                continue
            invalidated = _invalidate_push_authorization(goal)
            was_active = goal.status == GoalRunStatus.ACTIVE
            was_reviewing = goal.strict_review_in_progress
            if was_active or was_reviewing:
                goal.status = GoalRunStatus.PAUSED
                goal.paused_at = utc_now()
                goal.resume_after_turn = False
                goal.strict_review_in_progress = False
                goal.strict_review_subagent_id = None
                goal.status_message = "backend restarted; resume the Goal explicitly"
            if goal.dispatch_state in {
                GoalDispatchState.PENDING,
                GoalDispatchState.DISPATCHED,
            }:
                if was_reviewing and goal.current_turn_id is None:
                    goal.dispatch_state = GoalDispatchState.IDLE
                else:
                    goal.dispatch_state = GoalDispatchState.UNCERTAIN
                    goal.status_message = (
                        "restart occurred during continuation dispatch; resume explicitly"
                    )
                recovered.append(self.store.put(goal))
            elif was_active or was_reviewing or invalidated:
                recovered.append(self.store.put(goal))
        return recovered
