"""SQLite snapshot persistence for Chat Goals."""

from __future__ import annotations

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
from ..local_storage import document_exists, read_document, write_document

REVIEW_OPTION_NAMES = set(GoalRunCreate.model_fields) - {
    "attachments",
    "objective",
    "client_request_id",
    "plan_first",
}
_LEGACY_DROPPED_GOAL_FIELDS = frozenset(
    {
        "strict_review_mode",
        "strict_review_candidates",
        "strict_review_briefing",
        "strict_review_mr_description",
    }
)


def _normalize_persisted_goal_item(raw: dict[str, object]) -> dict[str, object]:
    item = {k: v for k, v in raw.items() if k not in _LEGACY_DROPPED_GOAL_FIELDS}
    if item.get("status") == "budget_limited":
        item["status"] = "paused"
        item["status_message"] = "Previous Goal limit removed; resume when ready"
    preset = item.get("strict_review_preset")
    if preset in {"mr_slim", "bug_only"}:
        item["strict_review_preset"] = "cross_review_slim"
    elif preset == "readonly_brief":
        item["strict_review_push_branch"] = False
        item["strict_review_push_pending"] = False
        item["strict_review_push_target"] = None
    return item


def _normalized_create_input(tab_id: str, request: GoalRunCreate) -> dict[str, object]:
    from .images import image_fingerprint

    supplied: dict[str, object] = {"tab_id": tab_id, "objective": request.objective}
    fingerprint = image_fingerprint(request.attachments)
    if fingerprint:
        supplied["images"] = fingerprint
    if request.plan_first:
        supplied["plan_first"] = True
    supplied.update(
        request.model_dump(mode="json", include=REVIEW_OPTION_NAMES, exclude_defaults=True)
    )
    return supplied


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
            if not document_exists(self.path):
                return
            payload = read_document(self.path)
            if payload.get("version") not in {1, 2}:
                raise ValueError("unsupported goal snapshot version")
            # Retire old limits and legacy per-run review matrices without
            # starting previously stopped work or losing read-only intent.
            goals = [
                GoalRun.model_validate(_normalize_persisted_goal_item(item))
                for item in payload.get("goals", [])
                if isinstance(item, dict)
            ]
            self._goals = {goal.id: goal for goal in goals}
            self._create_requests = dict(payload.get("create_requests", {}))
            normalized_inputs: dict[str, dict[str, object]] = {}
            for key, value in payload.get("create_inputs", {}).items():
                if not isinstance(value, dict):
                    continue
                cleaned = {
                    name: item
                    for name, item in value.items()
                    if name
                    in {
                        "tab_id",
                        "objective",
                        "images",
                        "plan_first",
                        *REVIEW_OPTION_NAMES,
                    }
                }
                if cleaned.get("strict_review_preset") in {"mr_slim", "bug_only"}:
                    cleaned["strict_review_preset"] = "cross_review_slim"
                elif cleaned.get("strict_review_preset") == "readonly_brief":
                    cleaned.pop("strict_review_push_branch", None)
                normalized_inputs[key] = cleaned
            self._create_inputs = normalized_inputs

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "goals": [self._persisted_goal(goal) for goal in self._goals.values()],
            "create_requests": self._create_requests,
            "create_inputs": self._create_inputs,
        }
        write_document(self.path, payload)

    @staticmethod
    def _persisted_goal(goal: GoalRun) -> dict[str, object]:
        payload = goal.model_dump(mode="json", exclude_none=True)
        payload["idempotency"] = dict(goal.idempotency)
        if goal.signal_recovery_text is not None:
            payload["signal_recovery_text"] = goal.signal_recovery_text
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

    def has_goal_history(self, tab_id: str) -> bool:
        """Whether this Chat ever ran a Goal, including cleared ones."""
        with self._lock:
            return any(goal.tab_id == tab_id for goal in self._goals.values())

    def latest_for_tab(self, tab_id: str) -> GoalRun | None:
        """Include cleared records when reconciling a provider-owned Goal."""
        with self._lock:
            candidates = [goal for goal in self._goals.values() if goal.tab_id == tab_id]
            if not candidates:
                return None
            return max(candidates, key=lambda goal: goal.created_at).model_copy(deep=True)

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

    def review_steer_receipt(self, tab_id: str, request_id: str) -> GoalRun | None:
        """Find the original queue owner even after clearing/replacing its Goal."""
        with self._lock:
            for goal in self._goals.values():
                if goal.tab_id == tab_id and goal.idempotency.get(request_id, "").startswith(
                    "review-steer:"
                ):
                    return goal.model_copy(deep=True)
        return None

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
                },
            )
            supplied = _normalized_create_input(tab_id, request)
            if original != supplied:
                raise ValueError("client_request_id was already used for another create")
            return prior.model_copy(deep=True)

    def create(
        self,
        goal: GoalRun,
        client_request_id: str,
        request: GoalRunCreate | None = None,
    ) -> GoalRun:
        with self._lock:
            effective_request = request or GoalRunCreate(
                objective=goal.objective,
                client_request_id=client_request_id,
                plan_first=goal.plan_first,
                **goal.model_dump(include=REVIEW_OPTION_NAMES),
            )
            replay = self.replay_create(goal.tab_id, effective_request)
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
            self._create_inputs[client_request_id] = _normalized_create_input(
                goal.tab_id, effective_request
            )
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
