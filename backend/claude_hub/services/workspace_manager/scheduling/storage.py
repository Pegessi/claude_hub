"""Scheduled-task implementation split from the compatibility mixin."""

from pydantic import ValidationError

from .._constants import (
    Any,
    ScheduledTask,
    ScheduledTaskRun,
    ScheduledTaskRunStatus,
    json,
    logger,
)
from ._contract import _SchedulingHost


class _ScheduleStorageMixin(_SchedulingHost):
    _SCHEDULED_RUN_HISTORY_LIMIT = 100

    @staticmethod
    def _validate_scheduled_tasks_payload(raw: Any) -> dict[str, Any]:
        """Structural validation for scheduled_tasks.json (primary and backup)."""

        if not isinstance(raw, dict):
            raise ValueError("scheduled tasks payload must be a JSON object")
        items = raw.get("scheduled_tasks", [])
        if not isinstance(items, list):
            raise ValueError("scheduled tasks payload 'scheduled_tasks' must be a list")
        runs = raw.get("scheduled_task_runs", [])
        if not isinstance(runs, list):
            raise ValueError("scheduled tasks payload 'scheduled_task_runs' must be a list")
        for item in items:
            try:
                ScheduledTask(**item)
            except TypeError as exc:
                raise ValueError("scheduled task entries must be objects") from exc
            except ValidationError as exc:
                if any(error["type"] == "extra_forbidden" for error in exc.errors()):
                    raise
        for item in runs:
            try:
                ScheduledTaskRun(**item)
            except TypeError as exc:
                raise ValueError("scheduled task run entries must be objects") from exc
            except ValidationError as exc:
                if any(error["type"] == "extra_forbidden" for error in exc.errors()):
                    raise
        return raw

    def _load_scheduled_tasks(self) -> None:
        path = self._runtime_scheduled_tasks_file
        result = self._load_durable_json(
            path,
            label="scheduled tasks",
            validator=self._validate_scheduled_tasks_payload,
        )
        raw = result.data
        if not isinstance(raw, dict):
            # Missing file (normal empty) or quarantined without a good backup.
            return
        items = raw.get("scheduled_tasks", [])
        for item in items:
            try:
                task = ScheduledTask(**item)
                self.scheduled_tasks[task.id] = task
            except Exception as exc:
                if isinstance(exc, ValidationError) and any(
                    error["type"] == "extra_forbidden" for error in exc.errors()
                ):
                    raise
                logger.warning("Skipping invalid scheduled task entry: %s", exc)
        runs = raw.get("scheduled_task_runs", [])
        for item in runs:
            try:
                run = ScheduledTaskRun(**item)
                self.scheduled_task_runs[run.id] = run
            except Exception as exc:
                logger.warning("Skipping invalid scheduled task run entry: %s", exc)
        self._scheduled_legacy_run_ids = {
            run.id for run in self.scheduled_task_runs.values() if self._chat_run_active(run)
        }
        # A dispatch/running marker owned by a prior process must be reconciled
        # against the durable stream before another turn can be sent.
        self._scheduled_chat_recovery_pending = any(
            run.status in {ScheduledTaskRunStatus.DISPATCHING, ScheduledTaskRunStatus.RUNNING}
            for run in self.scheduled_task_runs.values()
        )
        logger.info(
            "Loaded %d scheduled task(s) and %d scheduled run(s)",
            len(self.scheduled_tasks),
            len(self.scheduled_task_runs),
        )

    def _save_scheduled_tasks(self) -> None:
        self._prune_scheduled_task_runs()
        self._runtime_scheduled_tasks_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "scheduled_tasks": [
                task.model_dump(mode="json") for task in self.scheduled_tasks.values()
            ],
            "scheduled_task_runs": [
                run.model_dump(mode="json") for run in self.scheduled_task_runs.values()
            ],
        }
        self._atomic_save_state_text(
            self._runtime_scheduled_tasks_file, json.dumps(payload, ensure_ascii=False, indent=2)
        )

    def _prune_scheduled_task_runs(self) -> None:
        """Bound completed history while always retaining active runs."""
        keep: set[str] = set()
        for task_id in self.scheduled_tasks:
            runs = sorted(
                (
                    run
                    for run in self.scheduled_task_runs.values()
                    if run.scheduled_task_id == task_id
                ),
                key=lambda run: (run.scheduled_for, run.queued_at),
                reverse=True,
            )
            terminal_kept = 0
            for run in runs:
                if self._chat_run_active(run):
                    keep.add(run.id)
                elif terminal_kept < self._SCHEDULED_RUN_HISTORY_LIMIT:
                    keep.add(run.id)
                    terminal_kept += 1
        self.scheduled_task_runs = {
            run_id: run for run_id, run in self.scheduled_task_runs.items() if run_id in keep
        }
