"""Provider-owned Goal projection and user operations; never schedules turns."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...models.goal_run import (
    GOAL_RECENT_TURN_IDS_LIMIT,
    TERMINAL_GOAL_STATUSES,
    GoalDispatchState,
    GoalRun,
    GoalRunCreate,
    GoalRunStatus,
    GoalUsageQuality,
    utc_now,
)
from .images import image_fingerprint
from .policy import GoalPolicyError
from .store import GoalRunStore

if TYPE_CHECKING:
    from ..agent_stream.providers.contracts import ProviderGoal, ProviderGoalSnapshot


class NativeGoalError(RuntimeError):
    """The native owner could not be read or a requested control is unconfirmed."""


@dataclass
class NativeGoalConnection:
    """One verified provider thread, with user-origin mutation callbacks."""

    snapshot: ProviderGoalSnapshot
    read: Callable[[], Awaitable[ProviderGoalSnapshot | None]]
    set_goal: Callable[[ProviderGoal], Awaitable[ProviderGoal]]
    clear_goal: Callable[[], Awaitable[bool]]
    stop_turn: Callable[[], Awaitable[None]]
    active_turn_id: Callable[[], Awaitable[str | None]]
    turn_in_flight: Callable[[], bool]
    send_input: Callable[[GoalRun, str, str], Awaitable[None]]


NativeGoalResolver = Callable[[str], Awaitable[NativeGoalConnection | None]]


class NativeGoalCoordinator:
    def __init__(self, store: GoalRunStore) -> None:
        self.store = store
        self.resolver: NativeGoalResolver | None = None
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, tab_id: str) -> asyncio.Lock:
        return self._locks.setdefault(tab_id, asyncio.Lock())

    async def connect(self, tab_id: str) -> NativeGoalConnection | None:
        if self.resolver is None:
            return None
        try:
            connection = await self.resolver(tab_id)
        except (RuntimeError, ValueError, NotImplementedError) as exc:
            raise NativeGoalError(f"Could not read provider Goal: {exc}") from exc
        previous = self.store.current_for_tab(tab_id)
        if (
            connection is None
            and previous is not None
            and previous.execution_owner == "provider_native"
            and (
                previous.status not in TERMINAL_GOAL_STATUSES
                or previous.dispatch_state != GoalDispatchState.IDLE
                or previous.provider_stop_pending
            )
        ):
            raise NativeGoalError("The existing Goal requires its native provider API")
        return connection

    def _put(self, goal: GoalRun) -> GoalRun:
        previous = self.store.get(goal.id)
        if previous == goal:
            return previous
        return self.store.put(goal)

    async def synchronize(self, tab_id: str, connection: NativeGoalConnection) -> GoalRun | None:
        """Project a confirmed snapshot, retiring any former Hub dispatch intent."""
        snapshot = connection.snapshot
        raw = snapshot["goal"]
        previous = self.store.latest_for_tab(tab_id)
        active_turn = await connection.active_turn_id()
        if raw is None:
            if previous is None:
                return None
            if (
                previous.execution_owner == "provider_native"
                and previous.provider_thread_id == snapshot["thread_id"]
            ):
                previous.provider_goal_absent = True
            if previous.status in TERMINAL_GOAL_STATUSES and not previous.provider_stop_pending:
                # A missing current provider Goal does not rewrite historical outcomes.
                if (
                    previous.execution_owner == "provider_native"
                    and previous.dispatch_state != GoalDispatchState.IDLE
                ):
                    previous.dispatch_state = GoalDispatchState.IDLE
                    previous.current_turn_id = None
                self._put(previous)
                return None
            previous.execution_owner = "provider_native"
            previous.provider_thread_id = snapshot["thread_id"]
            previous.provider_goal_absent = True
            previous.provider_status = None
            if previous.status != GoalRunStatus.COMPLETE:
                previous.status = GoalRunStatus.CANCELLED
            previous.completed_at = previous.completed_at or utc_now()
            previous.pending_step_id = None
            previous.resume_after_turn = False
            if previous.provider_stop_pending and active_turn is not None:
                self._own_turn(previous, active_turn)
            previous.current_turn_id = active_turn if previous.provider_stop_pending else None
            previous.provider_stop_pending = (
                previous.provider_stop_pending and connection.turn_in_flight()
            )
            previous.dispatch_state = (
                GoalDispatchState.UNCERTAIN
                if previous.provider_stop_pending
                else GoalDispatchState.IDLE
            )
            self._put(previous)
            return previous if previous.provider_stop_pending else None

        created_at = raw["createdAt"]
        same_generation = previous is not None and (
            (
                previous.execution_owner == "hub_managed"
                and previous.status not in TERMINAL_GOAL_STATUSES
                and previous.objective == raw["objective"]
            )
            or (
                previous.execution_owner == "provider_native"
                and previous.provider_thread_id == snapshot["thread_id"]
                and not previous.provider_goal_absent
                and previous.status != GoalRunStatus.CANCELLED
                and (
                    previous.provider_goal_created_at == created_at
                    or (
                        previous.provider_goal_created_at is None
                        and previous.objective == raw["objective"]
                    )
                )
            )
        )
        if not same_generation:
            if previous is not None and (
                previous.status not in TERMINAL_GOAL_STATUSES
                or previous.dispatch_state != GoalDispatchState.IDLE
            ):
                previous.status = GoalRunStatus.CANCELLED
                previous.dispatch_state = GoalDispatchState.IDLE
                previous.pending_step_id = None
                previous.current_turn_id = None
                previous.resume_after_turn = False
                self._put(previous)
            # Native createdAt has second precision. A confirmed absence ends
            # the local generation even if a replacement reuses that timestamp.
            previous_id = previous.id if previous is not None else ""
            generation = hashlib.sha256(
                f"{tab_id}\0{snapshot['thread_id']}\0{created_at}\0{previous_id}".encode()
            ).hexdigest()
            previous = self.store.create(
                GoalRun(
                    tab_id=tab_id,
                    objective=raw["objective"],
                    execution_owner="provider_native",
                    provider_thread_id=snapshot["thread_id"],
                    provider_goal_created_at=created_at,
                ),
                f"native:{generation}",
            )
        assert previous is not None
        goal = previous.model_copy(deep=True)
        if goal.objective != raw["objective"]:
            goal.objective = raw["objective"]
            goal.objective_revision += 1
        goal.execution_owner = "provider_native"
        goal.provider_thread_id = snapshot["thread_id"]
        goal.provider_goal_created_at = created_at
        goal.provider_updated_at = raw["updatedAt"]
        goal.provider_status = raw["status"]
        goal.provider_goal_absent = False
        goal.status = GoalRunStatus(raw["status"])
        goal.token_usage = raw["tokensUsed"]
        goal.usage_quality = GoalUsageQuality.EXACT
        if active_turn is not None and (
            goal.status == GoalRunStatus.ACTIVE or goal.provider_stop_pending
        ):
            # Direct input remains blocked until a failed stop is resolved; a
            # successor observed in that interval still belongs to this Goal.
            self._own_turn(goal, active_turn)
        goal.current_turn_id = active_turn if active_turn in goal.provider_owned_turn_ids else None
        goal.pending_step_id = None
        goal.resume_after_turn = False
        goal.provider_stop_pending = goal.provider_stop_pending and connection.turn_in_flight()
        goal.dispatch_state = (
            GoalDispatchState.UNCERTAIN if goal.provider_stop_pending else GoalDispatchState.IDLE
        )
        if not goal.provider_stop_pending:
            goal.status_message = None
        goal.paused_at = (
            (goal.paused_at or utc_now()) if goal.status == GoalRunStatus.PAUSED else None
        )
        goal.completed_at = (
            (goal.completed_at or utc_now()) if goal.status == GoalRunStatus.COMPLETE else None
        )
        # Provider-native Goals neither emit nor consume the Hub checkpoint protocol.
        goal.checkpoint = None
        goal.checkpoint_warning = None
        return self._put(goal)

    @staticmethod
    def _own_turn(goal: GoalRun, turn_id: str) -> None:
        if turn_id not in goal.provider_owned_turn_ids:
            goal.provider_owned_turn_ids.append(turn_id)
            goal.provider_owned_turn_ids = goal.provider_owned_turn_ids[
                -GOAL_RECENT_TURN_IDS_LIMIT:
            ]
        goal.current_turn_id = turn_id

    @staticmethod
    def _mutation_reflected(
        operation: str,
        goal: GoalRun,
        current: GoalRun | None,
        objective: str | None,
        turn_id: str | None,
    ) -> bool:
        if goal.provider_stop_pending:
            return False
        if operation == "clear":
            return current is None
        if current is None or current.id != goal.id:
            return False
        if operation == "pause":
            return goal.status in {GoalRunStatus.PAUSED, GoalRunStatus.COMPLETE}
        if operation == "complete":
            return goal.status == GoalRunStatus.COMPLETE
        if operation == "resume":
            return goal.status == GoalRunStatus.ACTIVE
        if operation == "objective":
            return goal.objective == objective
        return (
            operation == "steer"
            and goal.status == GoalRunStatus.ACTIVE
            and turn_id in goal.provider_owned_turn_ids
        )

    async def refresh(self, tab_id: str) -> tuple[bool, GoalRun | None]:
        async with self.lock(tab_id):
            connection = await self.connect(tab_id)
            if connection is None:
                return False, self.store.current_for_tab(tab_id)
            return True, await self.synchronize(tab_id, connection)

    async def _read_back(self, tab_id: str, connection: NativeGoalConnection) -> GoalRun | None:
        snapshot = await connection.read()
        if snapshot is None or snapshot["thread_id"] != connection.snapshot["thread_id"]:
            raise NativeGoalError("Provider Goal ownership changed during the operation")
        connection.snapshot = snapshot
        return await self.synchronize(tab_id, connection)

    def _uncertain(self, goal_id: str, exc: BaseException) -> None:
        goal = self.store.get(goal_id)
        goal.dispatch_state = GoalDispatchState.UNCERTAIN
        goal.status_message = f"Provider Goal operation is unconfirmed: {exc}"
        self._put(goal)

    async def create(self, tab_id: str, request: GoalRunCreate) -> GoalRun | None:
        async with self.lock(tab_id):
            connection = await self.connect(tab_id)
            if connection is None:
                return None
            current = await self.synchronize(tab_id, connection)
            replay = self.store.replay_create(tab_id, request)
            if replay is not None:
                receipt = replay.idempotency.get(request.client_request_id, "")
                if receipt.startswith("pending:create:"):
                    baseline = receipt.removeprefix("pending:create:")
                    if (
                        current is None
                        or current.id != replay.id
                        or current.objective != request.objective
                        or str(current.provider_goal_created_at) == baseline
                        or (
                            request.attachments
                            and (
                                request.client_request_id not in replay.provider_owned_turn_ids
                                or current.status == GoalRunStatus.PAUSED
                            )
                        )
                    ):
                        raise NativeGoalError("Previous native Goal creation is not confirmed")
                    replay.idempotency[request.client_request_id] = "create"
                    replay.pending_image_count = 0
                    replay.pending_image_fingerprint = ""
                    return self._put(replay)
                return replay
            if current is not None and current.status not in TERMINAL_GOAL_STATUSES:
                raise GoalPolicyError("tab already has an unfinished provider Goal")
            goal = self.store.create(
                GoalRun(
                    tab_id=tab_id,
                    objective=request.objective,
                    execution_owner="provider_native",
                    provider_thread_id=connection.snapshot["thread_id"],
                    status=GoalRunStatus.PAUSED,
                    dispatch_state=GoalDispatchState.PENDING,
                    pending_image_count=len(request.attachments),
                    pending_image_fingerprint=image_fingerprint(request.attachments),
                    idempotency={
                        request.client_request_id: "pending:create:"
                        + str(
                            connection.snapshot["goal"]["createdAt"]
                            if connection.snapshot["goal"]
                            else None
                        )
                    },
                ),
                request.client_request_id,
                request,
            )
            try:
                if connection.snapshot["goal"] is not None:
                    await connection.clear_goal()
                raw = await connection.set_goal(
                    {
                        "objective": request.objective,
                        "status": "paused" if request.attachments else "active",
                    }
                )
                connection.snapshot = {"thread_id": connection.snapshot["thread_id"], "goal": raw}
                await self.synchronize(tab_id, connection)
                if request.attachments:
                    await connection.send_input(
                        self.store.get(goal.id), request.objective, request.client_request_id
                    )
                    accepted = self.store.get(goal.id)
                    self._own_turn(accepted, request.client_request_id)
                    self._put(accepted)
                    await connection.set_goal({"status": "active"})
                result = await self._read_back(tab_id, connection)
                if result is None or result.id != goal.id:
                    raise NativeGoalError("Provider did not retain the created Goal")
                result.idempotency[request.client_request_id] = "create"
                result.pending_image_count = 0
                result.pending_image_fingerprint = ""
                return self._put(result)
            except (Exception, asyncio.CancelledError) as exc:
                self._uncertain(goal.id, exc)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise NativeGoalError(str(exc)) from exc

    async def mutate(
        self,
        goal_id: str,
        request_id: str,
        operation: str,
        *,
        objective: str | None = None,
        text: str | None = None,
        turn_id: str | None = None,
        image_count: int = 0,
        image_digest: str = "",
    ) -> GoalRun | None:
        original = self.store.get(goal_id)
        async with self.lock(original.tab_id):
            connection = await self.connect(original.tab_id)
            if connection is None:
                return None
            current = await self.synchronize(original.tab_id, connection)
            goal = self.store.get(goal_id)
            fingerprint = operation
            if objective is not None or text is not None:
                fingerprint += (
                    ":"
                    + hashlib.sha256(
                        (objective or (text or "") + "\0" + image_digest).encode()
                    ).hexdigest()
                )
            prior = goal.idempotency.get(request_id)
            if prior is not None:
                if prior == "pending:" + fingerprint:
                    if not self._mutation_reflected(operation, goal, current, objective, turn_id):
                        raise NativeGoalError(f"Previous native Goal {operation} is not confirmed")
                    goal.idempotency[request_id] = fingerprint
                    if operation == "clear":
                        goal.status = GoalRunStatus.CANCELLED
                    return self._put(goal)
                if prior != fingerprint:
                    raise GoalPolicyError("client_request_id was already used for another mutation")
                return goal
            if current is None or current.id != goal_id:
                if operation == "clear" and current is None:
                    goal.idempotency[request_id] = fingerprint
                    goal.status = GoalRunStatus.CANCELLED
                    goal.dispatch_state = GoalDispatchState.IDLE
                    return self._put(goal)
                raise GoalPolicyError(
                    "The provider Goal changed or was cleared; refresh before continuing"
                )
            if (
                goal.status == GoalRunStatus.COMPLETE
                and operation != "clear"
                and not (goal.provider_stop_pending and operation in {"pause", "complete"})
            ):
                raise GoalPolicyError("The provider Goal is complete")
            should_stop = (
                goal.status == GoalRunStatus.ACTIVE
                or goal.current_turn_id is not None
                or goal.provider_stop_pending
            )
            goal.idempotency[request_id] = "pending:" + fingerprint
            goal.dispatch_state = GoalDispatchState.UNCERTAIN
            goal.provider_stop_pending = should_stop and operation in {
                "pause",
                "complete",
                "clear",
                "steer",
            }
            goal.pending_image_count = image_count
            goal.pending_image_fingerprint = image_digest
            self._put(goal)
            try:
                if operation in {"pause", "complete", "clear", "steer"}:
                    if (
                        connection.snapshot["goal"] is not None
                        and goal.status != GoalRunStatus.COMPLETE
                    ):
                        state = "complete" if operation == "complete" else "paused"
                        await connection.set_goal({"status": state})
                    if should_stop:
                        target = await connection.active_turn_id()
                        stopping = self.store.get(goal.id)
                        if target is not None:
                            self._own_turn(stopping, target)
                        self._put(stopping)
                        await connection.stop_turn()
                if operation == "clear":
                    await connection.clear_goal()
                elif operation == "resume":
                    await connection.set_goal({"status": "active"})
                elif operation == "objective":
                    assert objective is not None
                    await connection.set_goal({"objective": objective})
                elif operation == "steer":
                    assert text is not None and turn_id is not None
                    await connection.send_input(self.store.get(goal_id), text, turn_id)
                    accepted = self.store.get(goal_id)
                    self._own_turn(accepted, turn_id)
                    self._put(accepted)
                    await connection.set_goal({"status": "active"})
                result = await self._read_back(original.tab_id, connection)
                if operation == "clear" and result is not None:
                    raise NativeGoalError("Provider Goal clear was not confirmed")
                if result is not None and result.id != goal_id:
                    raise NativeGoalError("Provider Goal changed during the operation")
                result = result or self.store.get(goal_id)
                if operation == "clear":
                    result.status = GoalRunStatus.CANCELLED
                result.idempotency[request_id] = fingerprint
                result.pending_image_count = 0
                result.pending_image_fingerprint = ""
                result.provider_stop_pending = False
                result.dispatch_state = GoalDispatchState.IDLE
                return self._put(result)
            except (Exception, asyncio.CancelledError) as exc:
                self._uncertain(goal_id, exc)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise NativeGoalError(str(exc)) from exc
