"""One serialized owner for patrol scheduling, delivery claims and persistence."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from ...models import AgentReport, AgentReportState, TaskLifecycle, TaskPhase, WorkspaceTask
from .commands import CommandRunner, run_command
from .daily_digest import DecisionModel, build_daily_digest
from .digest_provider import recommend_daily_decisions
from .feishu_triage import TriageCallback, run_feishu_triage
from .models import PatrolChannel, PatrolConfig, PatrolNotifier, PatrolRunRecord, PatrolState
from .state import PatrolStateStore

logger = logging.getLogger(__name__)
TaskInputs = tuple[list[WorkspaceTask], dict[str, TaskLifecycle], dict[str, str]]
TaskSource = Callable[[], TaskInputs]


def _hub_tasks() -> TaskInputs:
    from .. import workspace_manager

    tasks = list(workspace_manager.tasks.values())
    lifecycles = workspace_manager.task_lifecycles(tasks)
    needs_user = {
        task.id: task for task in tasks if lifecycles[task.id].phase == TaskPhase.NEEDS_USER
    }
    latest: dict[str, AgentReport] = {}
    if needs_user:
        for report in workspace_manager.reports.values():
            task = needs_user.get(report.task_id or "")
            if task is None or report.review_cycle != task.review_cycle:
                continue
            lifecycle = lifecycles[task.id]
            if lifecycle.reason == "reviewer_needs_input":
                if report.state != AgentReportState.REVIEW_NEEDS_INPUT:
                    continue
            elif lifecycle.owner.id != report.session_id:
                continue
            if task.id not in latest or report.created_at > latest[task.id].created_at:
                latest[task.id] = report
    details = {
        task_id: report.message_zh or report.message_en or report.message
        for task_id, report in latest.items()
        if report.state
        in {
            AgentReportState.NEEDS_INPUT,
            AgentReportState.BLOCKED,
            AgentReportState.REVIEW_NEEDS_INPUT,
        }
    }
    return tasks, lifecycles, details


def _dot() -> PatrolNotifier:
    from ..dot import get_dot_service

    return get_dot_service()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def due_daily_slots(state: PatrolState, now: datetime) -> list[str]:
    """Return unclaimed local slots in the strictly bounded 90-minute grace window."""
    if not state.config.enabled or not state.config.daily_enabled:
        return []
    local = now.astimezone(ZoneInfo(state.config.timezone))
    due = []
    for slot in state.config.daily_slots:
        hour, minute = map(int, slot.split(":"))
        at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        key = f"{local.date().isoformat()}@{slot}"
        if at <= local < at + timedelta(minutes=90) and key not in state.fired_daily_slots:
            due.append(key)
    return due


def feishu_due(state: PatrolState, now: datetime) -> bool:
    config = state.config
    if not config.enabled or not config.feishu_enabled:
        return False
    local = now.astimezone(ZoneInfo(config.timezone))
    clock = local.strftime("%H:%M")
    if not config.feishu_window_start <= clock <= config.feishu_window_end:
        return False
    last = next(
        (
            record.started_at
            for record in reversed(state.recent_runs)
            if record.channel == "feishu" and not record.dry_run
        ),
        None,
    )
    if last:
        checked = datetime.fromisoformat(last)
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        if now - checked < timedelta(minutes=config.feishu_interval_minutes):
            return False
    return True


class DemandPatrolService:
    def __init__(
        self,
        *,
        store: PatrolStateStore | None = None,
        runner: CommandRunner = run_command,
        dot: PatrolNotifier | None = None,
        triage: TriageCallback | None = None,
        decision_model: DecisionModel = recommend_daily_decisions,
        task_source: TaskSource = _hub_tasks,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.store = store or PatrolStateStore()
        self.runner = runner
        self._dot_override = dot
        self.triage = triage
        self.decision_model = decision_model
        self.task_source = task_source
        self.clock = clock
        self._run_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._loop_task: asyncio.Task[None] | None = None

    def _notifier(self) -> PatrolNotifier:
        return self._dot_override if self._dot_override is not None else _dot()

    async def status(self) -> dict[str, Any]:
        async with self._state_lock:
            state = await asyncio.to_thread(self.store.load)
            return {**state.model_dump(mode="json"), "running": self._run_lock.locked()}

    async def configure(self, changes: dict[str, Any]) -> PatrolConfig:
        async with self._state_lock:
            state = await asyncio.to_thread(self.store.load)
            merged = state.config.model_dump()
            if "patrol_workspace_id" in changes and "workspace_id" not in changes:
                merged.pop("workspace_id", None)
            merged.update(changes)
            state.config = PatrolConfig.model_validate(merged)
            await asyncio.to_thread(self.store.save, state)
            return state.config

    async def _save_run_state(self, state: PatrolState) -> None:
        async with self._state_lock:
            # Config controls remain responsive during long diagnoses. A run may
            # finish, but its stale snapshot must never undo a concurrent disable.
            current = await asyncio.to_thread(self.store.load)
            state.config = current.config
            await asyncio.to_thread(self.store.save, state)

    async def run(
        self,
        *,
        channel: PatrolChannel = "all",
        dry_run: bool = False,
        notify: bool = True,
    ) -> dict[str, Any]:
        if channel not in {"all", "feishu", "daily"}:
            raise ValueError("channel must be all, feishu or daily")
        async with self._run_lock:
            state = await asyncio.to_thread(self.store.load)
            now = self.clock()
            channels = ["feishu", "daily"] if channel == "all" else [channel]
            records = []
            for selected in channels:
                record = await self._run_channel(
                    state, selected, now=now, dry_run=dry_run, notify=notify
                )
                records.append(record)
            return {
                "runs": [record.model_dump(mode="json") for record in records],
                "markdown": "\n\n".join(record.markdown for record in records if record.markdown),
            }

    async def _run_channel(
        self,
        state: PatrolState,
        channel: str,
        *,
        now: datetime,
        dry_run: bool,
        notify: bool,
        slot_key: str | None = None,
    ) -> PatrolRunRecord:
        candidate = state.model_copy(deep=True)
        selected: Literal["feishu", "daily"] = "feishu" if channel == "feishu" else "daily"
        try:
            if selected == "feishu":
                record = await run_feishu_triage(
                    candidate,
                    now=now,
                    runner=self.runner,
                    dot=self._notifier(),
                    dry_run=dry_run,
                    notify=notify,
                    triage=self.triage,
                )
            else:
                tasks, lifecycles, task_details = self.task_source()
                slot_label = slot_key.split("@", 1)[1] if slot_key else "manual"
                digest = await build_daily_digest(
                    candidate,
                    now=now,
                    runner=self.runner,
                    tasks=tasks,
                    lifecycles=lifecycles,
                    task_details=task_details,
                    decision_model=self.decision_model,
                    slot_label=slot_label,
                    dry_run=dry_run,
                )
                record = digest.record
                if slot_key and not dry_run:
                    # A quiet run consumes its slot too. Claim before a send
                    # so an ambiguous delivery cannot replay this slot.
                    candidate.fired_daily_slots.append(slot_key)
                    if record.markdown:
                        await self._save_run_state(candidate)
                if notify and not dry_run and record.markdown:
                    result = await self._notifier().notify(
                        record.markdown,
                        title=f"📋 研发需求与 Work 巡检 ({slot_label})",
                        tone="info",
                    )
                    record.notified = int(result == "sent")
                    record.details["notification"] = result
                    if result in {"sent", "repeat"}:
                        candidate.daily_decision_receipts.update(digest.receipts)
                    else:
                        record.errors.append(
                            "Dot notification failed; inspect binding before manual retry"
                        )
        except Exception as exc:
            # This is the automation boundary: one broken source must not stop
            # the other channel or the resident loop. Never emit raw CLI data.
            logger.warning("%s patrol failed (%s)", selected, type(exc).__name__)
            record = PatrolRunRecord(
                channel=selected,
                started_at=now.isoformat(),
                dry_run=dry_run,
                errors=[f"{selected} patrol failed: {type(exc).__name__}"],
            )
        if not dry_run:
            candidate.recent_runs.append(record)
            await self._save_run_state(candidate)
            # The next channel in this serialized run sees committed evidence.
            state.config = candidate.config
            state.feishu_watermark_iso = candidate.feishu_watermark_iso
            state.processed_feishu_ids = candidate.processed_feishu_ids
            state.fired_daily_slots = candidate.fired_daily_slots
            state.daily_mr_cursor = candidate.daily_mr_cursor
            state.mr_snapshots = candidate.mr_snapshots
            state.daily_decision_receipts = candidate.daily_decision_receipts
            state.recent_runs = candidate.recent_runs
        return record

    async def tick(self) -> list[PatrolRunRecord]:
        """Execute only due channels; useful for deterministic clock-driven tests."""
        async with self._run_lock:
            state = await asyncio.to_thread(self.store.load)
            now = self.clock()
            if not state.config.enabled or not self._notifier().is_set_up():
                return []
            records = []
            for key in due_daily_slots(state, now):
                records.append(
                    await self._run_channel(
                        state, "daily", now=now, dry_run=False, notify=True, slot_key=key
                    )
                )
            if feishu_due(state, now):
                records.append(
                    await self._run_channel(state, "feishu", now=now, dry_run=False, notify=True)
                )
            return records

    def start(self, *, allow_test_loop: bool = False) -> None:
        if os.environ.get("CLAUDE_HUB_DISABLE_PATROL_LOOP") == "1":
            return
        # Importing the app or running unrelated lifespan tests cannot launch
        # real CLI/model/notification work. Tests explicitly tick injected fakes.
        if "pytest" in sys.modules and not allow_test_loop:
            return
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._loop(), name="demand-patrol")

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:
                logger.warning("Patrol loop unavailable (%s)", type(exc).__name__)
            await asyncio.sleep(60)

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None


demand_patrol_service = DemandPatrolService()
