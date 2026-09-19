"""Atomic JSON snapshot persistence for Chat Goals."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from ...models.goal_run import (
    TERMINAL_GOAL_STATUSES,
    GoalDispatchState,
    GoalRun,
    GoalRunCreate,
    GoalRunStatus,
    utc_now,
)


class GoalRunStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._goals: dict[str, GoalRun] = {}
        self._create_requests: dict[str, str] = {}
        self._create_inputs: dict[str, dict[str, object]] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") != 1:
                raise ValueError("unsupported goal snapshot version")
            goals = [GoalRun.model_validate(item) for item in payload.get("goals", [])]
            self._goals = {goal.id: goal for goal in goals}
            self._create_requests = dict(payload.get("create_requests", {}))
            self._create_inputs = dict(payload.get("create_inputs", {}))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "goals": [self._persisted_goal(goal) for goal in self._goals.values()],
            "create_requests": self._create_requests,
            "create_inputs": self._create_inputs,
        }
        fd, raw_tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        tmp = Path(raw_tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _persisted_goal(goal: GoalRun) -> dict[str, object]:
        payload = goal.model_dump(mode="json", exclude_none=True)
        payload["idempotency"] = dict(goal.idempotency)
        return payload

    def list(self) -> list[GoalRun]:
        with self._lock:
            return [goal.model_copy(deep=True) for goal in self._goals.values()]

    def get(self, goal_id: str) -> GoalRun:
        with self._lock:
            try:
                return self._goals[goal_id].model_copy(deep=True)
            except KeyError:
                raise KeyError(f"goal '{goal_id}' not found") from None

    def current_for_tab(self, tab_id: str) -> GoalRun | None:
        with self._lock:
            candidates = [goal for goal in self._goals.values() if goal.tab_id == tab_id]
            if not candidates:
                return None
            current = max(candidates, key=lambda goal: goal.created_at)
            if (
                current.status == GoalRunStatus.CANCELLED
                and current.dispatch_state == GoalDispatchState.IDLE
            ):
                return None
            return current.model_copy(deep=True)

    def replay_create(self, tab_id: str, request: GoalRunCreate) -> GoalRun | None:
        """Return an exact create replay, rejecting request-id reuse with new input."""
        with self._lock:
            prior_id = self._create_requests.get(request.client_request_id)
            if prior_id is None:
                return None
            prior = self._goals[prior_id]
            original = self._create_inputs.get(
                request.client_request_id,
                {
                    "tab_id": prior.tab_id,
                    "objective": prior.objective,
                    "token_budget": prior.token_budget,
                    "max_turns": prior.max_turns,
                },
            )
            supplied = {"tab_id": tab_id, **request.model_dump(exclude={"client_request_id"})}
            if original != supplied:
                raise ValueError("client_request_id was already used for another create")
            return prior.model_copy(deep=True)

    def create(self, goal: GoalRun, client_request_id: str) -> GoalRun:
        with self._lock:
            replay = self.replay_create(
                goal.tab_id,
                GoalRunCreate(
                    objective=goal.objective,
                    token_budget=goal.token_budget,
                    max_turns=goal.max_turns,
                    client_request_id=client_request_id,
                ),
            )
            if replay is not None:
                return replay
            if any(
                existing.tab_id == goal.tab_id
                and (
                    existing.status not in TERMINAL_GOAL_STATUSES
                    or existing.dispatch_state != GoalDispatchState.IDLE
                )
                for existing in self._goals.values()
            ):
                raise ValueError("tab already has an unfinished goal")
            self._goals[goal.id] = goal.model_copy(deep=True)
            self._create_requests[client_request_id] = goal.id
            self._create_inputs[client_request_id] = {
                "tab_id": goal.tab_id,
                "objective": goal.objective,
                "token_budget": goal.token_budget,
                "max_turns": goal.max_turns,
            }
            try:
                self._save()
            except Exception:
                self._goals.pop(goal.id, None)
                self._create_requests.pop(client_request_id, None)
                self._create_inputs.pop(client_request_id, None)
                raise
            return goal.model_copy(deep=True)

    def put(self, goal: GoalRun) -> GoalRun:
        with self._lock:
            if goal.id not in self._goals:
                raise KeyError(f"goal '{goal.id}' not found")
            previous = self._goals[goal.id]
            goal = goal.model_copy(deep=True)
            goal.updated_at = utc_now()
            self._goals[goal.id] = goal.model_copy(deep=True)
            try:
                self._save()
            except Exception:
                self._goals[goal.id] = previous
                raise
            return goal.model_copy(deep=True)
