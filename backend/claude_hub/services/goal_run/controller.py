"""Lifecycle controller for Hub-managed Chat Goals."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from ...models.goal_run import (
    TERMINAL_GOAL_STATUSES,
    GoalDispatchState,
    GoalRun,
    GoalRunCreate,
    GoalRunStatus,
    GoalTurnUsage,
    GoalUsageQuality,
    utc_now,
)
from .policy import GoalPolicyError, can_continue, ensure_unfinished
from .store import GoalRunStore

DispatchCallback = Callable[[GoalRun, str], Awaitable[str | None] | str | None]
CancelCallback = Callable[[GoalRun], Awaitable[None] | None]
_STATUS_RE = re.compile(
    r'<goal-status\s+state=["\'](continue|complete|blocked|needs_input)["\']\s*>(.*?)</goal-status>\s*$',
    re.IGNORECASE | re.DOTALL,
)


def build_continuation_prompt(goal: GoalRun) -> str:
    """Build the bounded, provider-neutral envelope for the next turn."""
    return f"""Continue the active Goal.

Objective (authoritative; do not narrow, reinterpret, or silently replace it):
{goal.objective}

Before claiming progress or completion, verify relevant repository, runtime, test, or external
state with available tools. You may use provider-native subagents when useful, while remaining
responsible for their results and staying within the user's existing authority.

End the final assistant response with exactly one trailing structured signal:
<goal-status state=\"continue|complete|blocked|needs_input\">brief evidence, blocker, or next step</goal-status>
Use one concrete state value. Use complete only when the whole objective is verified; use continue
when another autonomous turn is needed; use blocked or needs_input when progress cannot continue.
"""


class GoalRunController:
    def __init__(
        self,
        store: GoalRunStore,
        dispatch: DispatchCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> None:
        self.store = store
        self.dispatch = dispatch
        self.cancel = cancel

    def set_dispatch_callback(self, dispatch: DispatchCallback | None) -> None:
        self.dispatch = dispatch

    def set_cancel_callback(self, cancel: CancelCallback | None) -> None:
        self.cancel = cancel

    def create(self, tab_id: str, request: GoalRunCreate) -> GoalRun:
        return self.store.create(
            GoalRun(
                tab_id=tab_id,
                objective=request.objective,
                token_budget=request.token_budget,
                max_turns=request.max_turns,
            ),
            request.client_request_id,
        )

    async def create_and_start(self, tab_id: str, request: GoalRunCreate) -> GoalRun:
        goal = self.create(tab_id, request)
        if goal.status == GoalRunStatus.ACTIVE and goal.dispatch_state == GoalDispatchState.IDLE:
            goal = await self.dispatch_next(goal.id)
        return goal

    def get(self, goal_id: str) -> GoalRun:
        return self.store.get(goal_id)

    def current(self, tab_id: str) -> GoalRun | None:
        return self.store.current_for_tab(tab_id)

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
        goal.updated_at = utc_now()
        return self.store.put(goal)

    async def _cancel_current(self, goal: GoalRun) -> GoalRun:
        if self.cancel is None or goal.current_turn_id is None:
            return goal
        try:
            result = self.cancel(goal.model_copy(deep=True))
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            latest = self.store.get(goal.id)
            latest.status_message = f"turn cancellation failed: {exc}"
            return self.store.put(latest)
        return self.store.get(goal.id)

    async def pause(self, goal_id: str, client_request_id: str) -> GoalRun:
        existing = self.store.get(goal_id)
        replay = existing.idempotency.get(client_request_id)
        if replay is not None:
            if replay != "pause":
                raise GoalPolicyError("client_request_id was already used for another mutation")
            return existing

        def apply(goal: GoalRun) -> None:
            ensure_unfinished(goal)
            goal.status = GoalRunStatus.PAUSED
            goal.paused_at = utc_now()
            goal.dispatch_state = GoalDispatchState.IDLE
            goal.pending_step_id = None

        goal = self._mutate_once(goal_id, client_request_id, "pause", apply)
        return await self._cancel_current(goal)

    async def resume(self, goal_id: str, client_request_id: str) -> GoalRun:
        def apply(goal: GoalRun) -> None:
            ensure_unfinished(goal)
            allowed, reason = can_continue(goal)
            if not allowed:
                goal.status = GoalRunStatus.BUDGET_LIMITED
                goal.status_message = reason
                raise GoalPolicyError(reason or "goal cannot continue")
            goal.status = GoalRunStatus.ACTIVE
            goal.paused_at = None
            goal.status_message = None

        goal = self._mutate_once(goal_id, client_request_id, "resume", apply)
        if goal.dispatch_state != GoalDispatchState.IDLE:
            return goal
        return await self.dispatch_next(goal.id)

    def complete(self, goal_id: str, client_request_id: str) -> GoalRun:
        def apply(goal: GoalRun) -> None:
            ensure_unfinished(goal)
            goal.status = GoalRunStatus.COMPLETE
            goal.completed_at = utc_now()
            goal.dispatch_state = GoalDispatchState.IDLE
            goal.pending_step_id = None

        return self._mutate_once(goal_id, client_request_id, "complete", apply)

    async def clear(self, goal_id: str, client_request_id: str) -> GoalRun:
        existing = self.store.get(goal_id)
        replay = existing.idempotency.get(client_request_id)
        if replay is not None:
            if replay != "clear":
                raise GoalPolicyError("client_request_id was already used for another mutation")
            return existing

        def apply(goal: GoalRun) -> None:
            ensure_unfinished(goal)
            goal.status = GoalRunStatus.CANCELLED
            goal.completed_at = utc_now()
            goal.dispatch_state = GoalDispatchState.IDLE
            goal.pending_step_id = None

        goal = self._mutate_once(goal_id, client_request_id, "clear", apply)
        return await self._cancel_current(goal)

    def update_budget(
        self, goal_id: str, client_request_id: str, token_budget: int | None
    ) -> GoalRun:
        def apply(goal: GoalRun) -> None:
            ensure_unfinished(goal)
            goal.token_budget = token_budget
            allowed, reason = can_continue(goal)
            if not allowed:
                goal.status = GoalRunStatus.BUDGET_LIMITED
                goal.status_message = reason
            elif goal.status == GoalRunStatus.BUDGET_LIMITED:
                goal.status = GoalRunStatus.PAUSED
                goal.status_message = None

        fingerprint = f"update_budget:{token_budget!r}"
        return self._mutate_once(goal_id, client_request_id, fingerprint, apply)

    @staticmethod
    def _usage(usage: GoalTurnUsage | dict[str, Any] | None) -> GoalTurnUsage:
        if usage is None:
            return GoalTurnUsage()
        if isinstance(usage, GoalTurnUsage):
            return usage
        total = usage.get("total_tokens")
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
    ) -> GoalRun | None:
        goal = self.store.current_for_tab(tab_id)
        if goal is None or turn_id in goal.completed_turn_ids:
            return goal
        if goal.status != GoalRunStatus.ACTIVE:
            return goal

        goal.completed_turn_ids.append(turn_id)
        goal.turns_completed += 1
        goal.current_turn_id = None
        goal.dispatch_state = GoalDispatchState.IDLE
        turn_usage = self._usage(usage)
        if turn_usage.total_tokens is not None:
            goal.token_usage = (goal.token_usage or 0) + turn_usage.total_tokens
            goal.usage_quality = turn_usage.quality
        elif goal.token_usage is None:
            goal.usage_quality = GoalUsageQuality.UNAVAILABLE

        if status.lower() not in {"complete", "completed", "success", "succeeded"}:
            goal.status = GoalRunStatus.FAILED
            goal.status_message = f"turn ended with status {status}"
            goal.completed_at = utc_now()
            return self.store.put(goal)

        match = _STATUS_RE.search(assistant_text)
        if match is None:
            goal.status = GoalRunStatus.FAILED
            goal.status_message = "assistant response omitted a valid trailing goal-status signal"
            goal.completed_at = utc_now()
            return self.store.put(goal)

        signal, message = match.group(1).lower(), match.group(2).strip() or None
        goal.status_message = message
        if signal == "complete":
            goal.status = GoalRunStatus.COMPLETE
            goal.completed_at = utc_now()
            return self.store.put(goal)
        if signal in {"blocked", "needs_input"}:
            goal.status = GoalRunStatus.BLOCKED
            return self.store.put(goal)

        allowed, reason = can_continue(goal)
        if not allowed:
            goal.status = GoalRunStatus.BUDGET_LIMITED
            goal.status_message = reason
            return self.store.put(goal)

        if self.dispatch is None:
            goal.status = GoalRunStatus.PAUSED
            goal.status_message = "automatic continuation is not connected"
            return self.store.put(goal)

        # Commit this turn and its accounting before the transport can observe
        # or act on the next durable dispatch intent.
        self.store.put(goal)
        return await self.dispatch_next(goal.id)

    async def dispatch_next(self, goal_id: str) -> GoalRun:
        """Persist dispatch intent, then invoke the injected transport callback."""
        goal = self.store.get(goal_id)
        if goal.status != GoalRunStatus.ACTIVE:
            return goal
        allowed, reason = can_continue(goal)
        if not allowed:
            goal.status = GoalRunStatus.BUDGET_LIMITED
            goal.status_message = reason
            return self.store.put(goal)
        if self.dispatch is None:
            goal.status = GoalRunStatus.PAUSED
            goal.status_message = "automatic continuation is not connected"
            return self.store.put(goal)
        if goal.dispatch_state != GoalDispatchState.IDLE:
            return goal

        goal.pending_step_id = str(uuid4())
        goal.dispatch_state = GoalDispatchState.PENDING
        goal = self.store.put(goal)
        try:
            result = self.dispatch(goal.model_copy(deep=True), build_continuation_prompt(goal))
            next_turn_id = await result if inspect.isawaitable(result) else result
        except Exception as exc:
            goal = self.store.get(goal.id)
            if goal.status == GoalRunStatus.ACTIVE:
                goal.status = GoalRunStatus.FAILED
                goal.status_message = f"continuation dispatch failed: {exc}"
                goal.completed_at = utc_now()
                goal.dispatch_state = GoalDispatchState.IDLE
                goal.pending_step_id = None
                return self.store.put(goal)
            return goal
        goal = self.store.get(goal.id)
        if goal.status != GoalRunStatus.ACTIVE:
            return goal
        goal.current_turn_id = next_turn_id or goal.pending_step_id
        goal.dispatch_state = GoalDispatchState.DISPATCHED
        return self.store.put(goal)

    def recover(self) -> list[GoalRun]:
        """Fail closed for dispatches whose provider acceptance is uncertain."""
        recovered: list[GoalRun] = []
        for goal in self.store.list():
            if goal.status == GoalRunStatus.ACTIVE and goal.dispatch_state in {
                GoalDispatchState.PENDING,
                GoalDispatchState.DISPATCHED,
            }:
                goal.status = GoalRunStatus.PAUSED
                goal.dispatch_state = GoalDispatchState.UNCERTAIN
                goal.status_message = (
                    "restart occurred during continuation dispatch; resume explicitly"
                )
                recovered.append(self.store.put(goal))
        return recovered
