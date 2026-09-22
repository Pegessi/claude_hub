"""Scheduled-task orchestration: cron / interval / one-off firing.

A ``ScheduledTask`` is a durable schedule that fires an action when its
next-run time arrives. Four kinds are supported:

* ``chat_turn`` — queue a native turn in an existing top-level Chat tab. The
  turn is delivered through the same provider session and structured event
  stream as the composer, so its prompt and answer appear in that conversation.

* ``tab_message`` — type a message into an existing terminal tab's pane and
  submit it. This is the agent self-scheduling primitive: an agent calls the
  ``claude-hub`` CLI (which hits the scheduling API) to register a schedule
  that will re-message its own tab on a cron / interval basis. The agent
  learns its own tab id from the ``CLAUDE_HUB_TAB_ID`` environment variable.
* ``new_session`` — create a new session in a workspace and send it a
  message (manual one-off execution, like Codex's "create a new session to
  execute").
* ``hub_task`` — publish a Hub-native system-internal task on a caller-owned
  ephemeral orchestrator. The task runs through the normal reviewed-task
  flow; when the worker reports completion the task is auto-DONE (skipping
  human review) and the ephemeral session is auto-deleted so no agent /
  reviewer resources are held.

Schedules are persisted to ``STATE_ROOT/scheduled_tasks.json`` (atomic
write) and hydrated at startup after the core workspace state. The tick is
driven by the 5-second background monitor loop.

Crash idempotency: before firing, the task's ``last_run_at`` / ``run_count``
/ ``next_run_at`` are stamped and persisted. Chat turns additionally create a
durable run with a deterministic turn id in that same atomic write.
"""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ._constants import *  # noqa: F401,F403


class _SchedulingMixin:
    """Scheduled-task persistence, CRUD, scheduling, and firing."""

    scheduled_task_runs: dict[str, ScheduledTaskRun]
    _scheduled_chat_recovery_pending: bool
    _scheduled_chat_tab_locks: dict[str, asyncio.Lock]
    _scheduled_chat_dispatch: Any
    _scheduled_chat_liveness: Any
    _sched_reap_checked_at: dict[str, datetime]

    # Minimum gap between fires of the same task. A concurrent fire (tick vs
    # run-now, or two run-now calls) that lands within this window joins the
    # in-flight fire instead of re-firing. This is the guard that stops a
    # recurring task from double-firing on concurrent manual run-now calls,
    # where the ``enabled`` / ``next_run_at`` re-checks don't apply.
    _FIRE_COOLDOWN = timedelta(seconds=1)
    _SCHEDULED_RUN_HISTORY_LIMIT = 100
    _SCHEDULED_CHAT_ACTIVE_RUN_LIMIT = 100
    # A run stuck in DISPATCHING/RUNNING past this age is reconciled against
    # the durable transcript and the live provider turn guard. A legitimately
    # long turn keeps the provider guard raised, so this only reaps dead turns.
    _SCHEDULED_RUN_STALE_GRACE = timedelta(minutes=10)
    # Minimum gap between two reconciliation attempts for the same stuck run;
    # scanning a full transcript on every 5s tick would be wasteful.
    _SCHEDULED_REAP_RECHECK = timedelta(seconds=60)

    # Per-field (min, max) for the 5 cron fields: minute, hour, day-of-month,
    # month, day-of-week (0 = Sunday).
    _CRON_FIELD_RANGES = (
        (0, 59),
        (0, 23),
        (1, 31),
        (1, 12),
        (0, 6),
    )
    _CRON_DAY_FULL = frozenset(range(1, 32))
    _CRON_DOW_FULL = frozenset(range(0, 7))

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_scheduled_tasks(self) -> None:
        if not SCHEDULED_TASKS_FILE.exists():
            return
        try:
            raw = json.loads(SCHEDULED_TASKS_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("Failed to load scheduled tasks from %s: %s", SCHEDULED_TASKS_FILE, exc)
            return
        items = raw.get("scheduled_tasks", [])
        if not isinstance(items, list):
            logger.warning(
                "Invalid scheduled tasks file: scheduled_tasks is not a list in %s",
                SCHEDULED_TASKS_FILE,
            )
            return
        for item in items:
            try:
                task = ScheduledTask(**item)
                self.scheduled_tasks[task.id] = task
            except Exception as exc:
                logger.warning("Skipping invalid scheduled task entry: %s", exc)
        runs = raw.get("scheduled_task_runs", [])
        if isinstance(runs, list):
            for item in runs:
                try:
                    run = ScheduledTaskRun(**item)
                    self.scheduled_task_runs[run.id] = run
                except Exception as exc:
                    logger.warning("Skipping invalid scheduled task run entry: %s", exc)
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
        SCHEDULED_TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "scheduled_tasks": [
                task.model_dump(mode="json") for task in self.scheduled_tasks.values()
            ],
            "scheduled_task_runs": [
                run.model_dump(mode="json") for run in self.scheduled_task_runs.values()
            ],
        }
        self._atomic_write_text(
            SCHEDULED_TASKS_FILE, json.dumps(payload, ensure_ascii=False, indent=2)
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

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_scheduled_task(self, payload: ScheduledTaskCreate) -> ScheduledTask:
        fields = payload.model_dump()
        self._validate_scheduled_task_fields(fields)
        name = payload.name.strip()
        if not name:
            raise ValueError("name must not be empty")
        now = _wm._now()
        agent_type = payload.agent_type
        if payload.kind == ScheduledTaskKind.CHAT_TURN and payload.tab_id:
            tab = ttyd_manager.get_tab(payload.tab_id)
            if tab is not None:
                agent_type = tab.agent_type
        task = ScheduledTask(
            id=str(uuid.uuid4()),
            name=name,
            kind=payload.kind,
            enabled=payload.enabled,
            run_at=payload.run_at,
            cron=payload.cron,
            interval_seconds=payload.interval_seconds,
            tab_id=payload.tab_id,
            workspace_id=payload.workspace_id,
            agent_type=agent_type,
            message=payload.message,
            task_title=payload.task_title,
            run_count=0,
            created_at=now,
            updated_at=now,
        )
        task.next_run_at = self._compute_next_run(task, now)
        self.scheduled_tasks[task.id] = task
        self._save_scheduled_tasks()
        logger.info(
            "Created scheduled task id=%s name=%r kind=%s next_run_at=%s",
            task.id,
            task.name,
            task.kind.value,
            task.next_run_at,
        )
        return task

    def list_scheduled_tasks(self) -> list[ScheduledTask]:
        return list(self.scheduled_tasks.values())

    def scheduled_task_view(self, task: ScheduledTask) -> ScheduledTaskView:
        """Decorate a durable task with live run/backlog counts (API responses)."""
        active = queued = in_flight = 0
        in_flight_runs: list[ScheduledTaskRun] = []
        for run in self.scheduled_task_runs.values():
            if run.scheduled_task_id != task.id or not self._chat_run_active(run):
                continue
            active += 1
            if run.status in {ScheduledTaskRunStatus.QUEUED, ScheduledTaskRunStatus.WAITING}:
                queued += 1
            else:
                in_flight += 1
                in_flight_runs.append(run)
        in_flight_run_id: Optional[str] = None
        in_flight_since: Optional[datetime] = None
        if in_flight_runs:
            oldest = min(in_flight_runs, key=lambda r: (r.scheduled_for, r.queued_at, r.id))
            in_flight_run_id = oldest.id
            in_flight_since = oldest.dispatched_at or oldest.queued_at
        return ScheduledTaskView(
            **task.model_dump(),
            active_run_count=active,
            queued_run_count=queued,
            in_flight_run_count=in_flight,
            in_flight_run_id=in_flight_run_id,
            in_flight_since=in_flight_since,
        )

    def list_scheduled_task_runs(self, task_id: str) -> list[ScheduledTaskRun]:
        if task_id not in self.scheduled_tasks:
            raise KeyError(task_id)
        return sorted(
            (run for run in self.scheduled_task_runs.values() if run.scheduled_task_id == task_id),
            key=lambda run: (run.scheduled_for, run.queued_at),
            reverse=True,
        )

    def configure_scheduled_chat_dispatch(self, callback: Any) -> None:
        """Inject the native Chat transport without importing the API layer."""
        self._scheduled_chat_dispatch = callback

    def get_scheduled_task(self, task_id: str) -> ScheduledTask:
        task = self.scheduled_tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def update_scheduled_task(self, task_id: str, payload: ScheduledTaskUpdate) -> ScheduledTask:
        task = self.scheduled_tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        updates = payload.model_dump(exclude_unset=True)
        if "name" in updates:
            updates["name"] = updates["name"].strip()
            if not updates["name"]:
                raise ValueError("name must not be empty")

        schedule_keys = ("run_at", "cron", "interval_seconds")
        schedule_changed = any(k in updates for k in schedule_keys)
        if schedule_changed:
            # Normalize to exactly one schedule field: clear the others so a
            # switch from cron to run_at (etc.) does not leave two set.
            for key in schedule_keys:
                updates.setdefault(key, None)

        now = _wm._now()
        merged = task.model_copy(update={**updates, "updated_at": now})

        fields = merged.model_dump()
        self._validate_scheduled_task_fields(fields, existing=task)
        if merged.kind == ScheduledTaskKind.CHAT_TURN and merged.tab_id:
            tab = ttyd_manager.get_tab(merged.tab_id)
            if tab is not None:
                # The provider is target-derived, not caller-controlled. An
                # edit/re-enable explicitly confirms the target's current
                # backend after a provider switch disabled the automation.
                merged.agent_type = tab.agent_type

        if schedule_changed:
            merged.next_run_at = self._compute_next_run(merged, now)

        # Disabling an automation is a Stop: cancel its queued / waiting
        # occurrences so they are not replayed in a burst if the task is later
        # re-enabled. A run already executing on the provider is left to finish
        # (its completion edge is terminal and no longer drains anything).
        if updates.get("enabled") is False and task.enabled:
            self._cancel_scheduled_task_runs(
                task.id,
                statuses={ScheduledTaskRunStatus.QUEUED, ScheduledTaskRunStatus.WAITING},
                reason="automation was disabled",
                now=now,
            )

        self.scheduled_tasks[task_id] = merged
        self._save_scheduled_tasks()
        logger.info("Updated scheduled task id=%s name=%r", task_id, merged.name)
        return merged

    def delete_scheduled_task(self, task_id: str) -> bool:
        task = self.scheduled_tasks.pop(task_id, None)
        if task is None:
            return False
        self.scheduled_task_runs = {
            run_id: run
            for run_id, run in self.scheduled_task_runs.items()
            if run.scheduled_task_id != task_id
        }
        self._save_scheduled_tasks()
        logger.info("Deleted scheduled task id=%s name=%r", task_id, task.name)
        return True

    async def run_scheduled_task(self, task_id: str) -> ScheduledTask:
        """Fire a scheduled task immediately (manual run-now).

        Stamps and advances the schedule just like a tick fire (a one-shot is
        disabled after firing). Raises ``RuntimeError`` if the fire itself
        failed so the API / CLI can surface a non-success status.
        """
        task = self.scheduled_tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        chat_run = await self._fire_scheduled_task(task, _wm._now(), manual=True)
        if task.kind == ScheduledTaskKind.CHAT_TURN and chat_run is not None:
            if chat_run.status in {
                ScheduledTaskRunStatus.FAILED,
                ScheduledTaskRunStatus.SKIPPED,
                ScheduledTaskRunStatus.UNCERTAIN,
                ScheduledTaskRunStatus.CANCELLED,
            }:
                raise RuntimeError(
                    chat_run.error
                    or chat_run.waiting_reason
                    or f"scheduled Chat run ended as {chat_run.status.value}"
                )
        elif task.last_status == "error":
            raise RuntimeError(task.last_error or "scheduled task failed to fire")
        return task

    async def cancel_scheduled_task_run(self, run_id: str) -> ScheduledTaskRun:
        """Manually cancel a wedged or queued scheduled Chat run.

        Lets a user unblock a stuck FIFO without a backend restart. Only
        non-terminal runs can be cancelled; a DISPATCHING/RUNNING run is marked
        CANCELLED directly (the provider-side turn is not killed — use the Chat
        Stop control for that — but it no longer blocks the queue: a late
        completion edge simply no-ops against a terminal run). The affected
        tab's queue is drained afterwards so the next occurrence dispatches.
        """
        run = self.scheduled_task_runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        if self._chat_run_active(run):
            now = _wm._now()
            run.status = ScheduledTaskRunStatus.CANCELLED
            run.waiting_reason = None
            run.error = "cancelled manually"
            run.completed_at = now
            self._sync_task_from_run(run)
            self._save_scheduled_tasks()
        await self._drain_scheduled_chat_runs(run.tab_id)
        return run

    async def cancel_pending_scheduled_task_runs(self, task_id: str) -> int:
        """Cancel every queued/waiting occurrence of a task (keep a live run)."""
        if task_id not in self.scheduled_tasks:
            raise KeyError(task_id)
        count = self._cancel_scheduled_task_runs(
            task_id,
            statuses={ScheduledTaskRunStatus.QUEUED, ScheduledTaskRunStatus.WAITING},
            reason="cancelled manually",
            now=_wm._now(),
        )
        if count:
            self._save_scheduled_tasks()
            for tab_id in {
                run.tab_id
                for run in self.scheduled_task_runs.values()
                if run.scheduled_task_id == task_id
            }:
                await self._drain_scheduled_chat_runs(tab_id)
        return count

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_scheduled_task_fields(
        self, fields: dict, existing: Optional[ScheduledTask] = None
    ) -> None:
        """Validate a complete set of scheduled-task fields. Raises ValueError.

        ``existing`` is the stored task being updated (``None`` on create).
        For ``tab_message`` it lets us skip the tab-existence re-check when
        the target tab is unchanged, so a task whose tab was deleted can
        still be disabled or edited (only changing the tab re-validates).
        """
        kind = fields.get("kind")

        schedule_count = sum(
            1 for key in ("run_at", "cron", "interval_seconds") if fields.get(key) is not None
        )
        if schedule_count != 1:
            raise ValueError("Exactly one of run_at, cron, or interval_seconds must be set")

        cron = fields.get("cron")
        if cron is not None:
            self._validate_cron_expression(cron)

        if kind == ScheduledTaskKind.CHAT_TURN:
            if not fields.get("tab_id"):
                raise ValueError("tab_id is required for chat_turn tasks")
            if not fields.get("message"):
                raise ValueError("message is required for chat_turn tasks")
            if existing is None or fields["tab_id"] != existing.tab_id:
                tab = ttyd_manager.get_tab(fields["tab_id"])
                if tab is None:
                    raise ValueError(f"Chat tab '{fields['tab_id']}' not found")
                if tab.session_kind != SessionKind.CHAT or tab.workspace_role is not None:
                    raise ValueError("chat_turn tasks require a top-level Chat tab")
                if tab.agent_type == AgentType.TERMINAL:
                    raise ValueError("chat_turn tasks require an AI Chat backend")
        elif kind == ScheduledTaskKind.TAB_MESSAGE:
            if not fields.get("tab_id"):
                raise ValueError("tab_id is required for tab_message tasks")
            if not fields.get("message"):
                raise ValueError("message is required for tab_message tasks")
            # Only re-check tab existence when the target tab is changing.
            # A task whose tab was deleted can still be disabled / edited.
            if existing is None or fields["tab_id"] != existing.tab_id:
                if ttyd_manager.get_tab(fields["tab_id"]) is None:
                    raise ValueError(f"Terminal tab '{fields['tab_id']}' not found")
        elif kind == ScheduledTaskKind.NEW_SESSION:
            if fields.get("cron") is not None or fields.get("interval_seconds") is not None:
                # A recurring new_session task spawns a fresh ephemeral session on
                # every fire with no completion signal to clean it up, leaking one
                # session per fire. Restrict to one-shot (run_at); recurring
                # "execute on a schedule" needs should use hub_task, which
                # auto-cleans its ephemeral session on completion.
                raise ValueError(
                    "new_session tasks only support one-shot run_at scheduling; "
                    "use hub_task for recurring execution (it auto-cleans)"
                )
            if not fields.get("workspace_id"):
                raise ValueError("workspace_id is required for new_session tasks")
            if not fields.get("message"):
                raise ValueError("message is required for new_session tasks")
            if fields["workspace_id"] not in self.workspaces:
                raise ValueError(f"Workspace '{fields['workspace_id']}' not found")
        elif kind == ScheduledTaskKind.HUB_TASK:
            if not fields.get("workspace_id"):
                raise ValueError("workspace_id is required for hub_task tasks")
            if not fields.get("task_title"):
                raise ValueError("task_title is required for hub_task tasks")
            if not fields.get("message"):
                raise ValueError("message (task prompt) is required for hub_task tasks")
            if fields["workspace_id"] not in self.workspaces:
                raise ValueError(f"Workspace '{fields['workspace_id']}' not found")

    # ------------------------------------------------------------------
    # Cron parsing
    # ------------------------------------------------------------------

    def _parse_cron_field(self, expr: str, min_val: int, max_val: int) -> frozenset[int]:
        """Parse one cron field into a frozenset of valid integers."""
        result: set[int] = set()
        for part in expr.split(","):
            part = part.strip()
            if not part:
                raise ValueError(f"empty list element in cron field {expr!r}")
            step = 1
            if "/" in part:
                base, step_str = part.split("/", 1)
                step_str = step_str.strip()
                if not step_str.isdigit() or int(step_str) < 1:
                    raise ValueError(f"invalid step {step_str!r} in cron field {expr!r}")
                step = int(step_str)
            else:
                base = part

            if base == "*":
                start, end = min_val, max_val
            elif "-" in base:
                start_str, end_str = base.split("-", 1)
                start_str, end_str = start_str.strip(), end_str.strip()
                if not start_str.isdigit() or not end_str.isdigit():
                    raise ValueError(f"invalid range {base!r} in cron field {expr!r}")
                start, end = int(start_str), int(end_str)
            elif base.isdigit():
                start = end = int(base)
                if step > 1:
                    # A bare value with a step (e.g. "5/10") means start at the
                    # value and step to the field max.
                    end = max_val
            else:
                raise ValueError(f"invalid value {base!r} in cron field {expr!r}")

            if start < min_val or end > max_val or start > end:
                raise ValueError(f"cron value {part!r} out of range [{min_val}, {max_val}]")
            result.update(range(start, end + 1, step))
        if not result:
            raise ValueError(f"cron field {expr!r} produced no values")
        return frozenset(result)

    def _parse_cron_expression(self, expr: str) -> tuple[frozenset[int], ...]:
        """Parse a 5-field cron expression into a tuple of value frozensets."""
        field_texts = expr.strip().split()
        if len(field_texts) != 5:
            raise ValueError(f"cron expression must have exactly 5 fields (got {len(field_texts)})")
        return tuple(
            self._parse_cron_field(text, min_val, max_val)
            for text, (min_val, max_val) in zip(field_texts, self._CRON_FIELD_RANGES)
        )

    def _validate_cron_expression(self, expr: str) -> None:
        self._parse_cron_expression(expr)

    def _cron_matches(self, cron_parts: tuple[frozenset[int], ...], dt: datetime) -> bool:
        minute_set, hour_set, dom_set, month_set, dow_set = cron_parts
        if dt.minute not in minute_set:
            return False
        if dt.hour not in hour_set:
            return False
        if dt.month not in month_set:
            return False
        dom_match = dt.day in dom_set
        dow_match = ((dt.weekday() + 1) % 7) in dow_set
        dom_unrestricted = dom_set == self._CRON_DAY_FULL
        dow_unrestricted = dow_set == self._CRON_DOW_FULL
        if dom_unrestricted and dow_unrestricted:
            return True
        if dom_unrestricted:
            return dow_match
        if dow_unrestricted:
            return dom_match
        return dom_match or dow_match

    def _next_cron_run(self, cron_expr: str, after: datetime) -> Optional[datetime]:
        """Find the next datetime matching ``cron_expr`` strictly after ``after``."""
        cron_parts = self._parse_cron_expression(cron_expr)
        minute_set, hour_set, dom_set, month_set, dow_set = cron_parts

        candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        # Search ~4 years out so a cron whose only match is Feb 29 (e.g.
        # "0 0 29 2 *") is not falsely reported as having no future run.
        limit = after + timedelta(days=366 * 4)

        while candidate <= limit:
            if candidate.month not in month_set:
                # Skip to the first day of the next month.
                if candidate.month == 12:
                    candidate = candidate.replace(
                        year=candidate.year + 1, month=1, day=1, hour=0, minute=0
                    )
                else:
                    candidate = candidate.replace(
                        month=candidate.month + 1, day=1, hour=0, minute=0
                    )
                continue

            dom_match = candidate.day in dom_set
            dow_match = ((candidate.weekday() + 1) % 7) in dow_set
            dom_unrestricted = dom_set == self._CRON_DAY_FULL
            dow_unrestricted = dow_set == self._CRON_DOW_FULL
            if dom_unrestricted and dow_unrestricted:
                day_ok = True
            elif dom_unrestricted:
                day_ok = dow_match
            elif dow_unrestricted:
                day_ok = dom_match
            else:
                day_ok = dom_match or dow_match

            if not day_ok:
                candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
                continue

            if candidate.hour not in hour_set:
                candidate = (candidate + timedelta(hours=1)).replace(minute=0)
                continue

            if candidate.minute not in minute_set:
                candidate += timedelta(minutes=1)
                continue

            return candidate

        return None

    # ------------------------------------------------------------------
    # Next-run computation
    # ------------------------------------------------------------------

    def _compute_next_run(self, task: ScheduledTask, after: datetime) -> Optional[datetime]:
        """Compute the next fire time for a task strictly after ``after``."""
        if task.run_at is not None:
            # One-shot: fire at run_at; if it is already in the past, fire now.
            return task.run_at if task.run_at > after else after
        if task.cron is not None:
            nxt = self._next_cron_run(task.cron, after)
            if nxt is None:
                logger.warning(
                    "Scheduled task %s cron %r has no future match within the search "
                    "window; next_run_at left unset (task will not fire)",
                    task.id,
                    task.cron,
                )
            return nxt
        if task.interval_seconds is not None:
            return after + timedelta(seconds=task.interval_seconds)
        return None

    # ------------------------------------------------------------------
    # Tick and fire
    # ------------------------------------------------------------------

    async def _tick_scheduled_tasks(self) -> None:
        if self._scheduled_chat_recovery_pending:
            await self._recover_scheduled_chat_runs()
            self._scheduled_chat_recovery_pending = False
        now = _wm._now()
        self._disable_deleted_chat_targets(now)
        await self._reap_stale_scheduled_chat_runs(now)
        for task_id in list(self.scheduled_tasks.keys()):
            task = self.scheduled_tasks.get(task_id)
            if task is None or not task.enabled:
                continue
            if task.next_run_at is None or task.next_run_at > now:
                continue
            try:
                await self._fire_scheduled_task(task, now)
            except Exception:
                logger.exception("Scheduled task tick failed for task_id=%s", task_id)
        await self._drain_scheduled_chat_runs()

    def _disable_deleted_chat_targets(self, now: datetime) -> None:
        changed = False
        for task in self.scheduled_tasks.values():
            if (
                task.kind != ScheduledTaskKind.CHAT_TURN
                or not task.enabled
                or not task.tab_id
                or ttyd_manager.get_tab(task.tab_id) is not None
            ):
                continue
            task.enabled = False
            task.last_status = ScheduledTaskRunStatus.SKIPPED.value
            task.last_error = "target Chat was deleted"
            task.updated_at = now
            changed = True
        if changed:
            self._save_scheduled_tasks()

    async def _fire_scheduled_task(
        self, task: ScheduledTask, now: datetime, *, manual: bool = False
    ) -> Optional[ScheduledTaskRun]:
        lock = self._sched_fire_locks.setdefault(task.id, asyncio.Lock())
        async with lock:
            # Re-check eligibility under the lock: a concurrent fire (the 5s
            # tick vs a manual run-now, or two run-now clicks) may have already
            # stamped this task. Without this re-check a one-shot could fire
            # twice and run_count could double-increment.
            #
            # The cooldown below is what prevents a *recurring* task from
            # double-firing on concurrent manual run-now calls: such a task
            # stays enabled after firing, so the ``enabled`` re-check does not
            # catch the second call, and the ``next_run_at`` re-check is
            # tick-only (it does not apply to the manual path).
            if task.last_run_at is not None and (now - task.last_run_at) < self._FIRE_COOLDOWN:
                # A concurrent fire advanced this task within the cooldown
                # window while we waited for the lock. Join it: don't re-fire.
                return (
                    self.scheduled_task_runs.get(task.last_run_id)
                    if task.kind == ScheduledTaskKind.CHAT_TURN and task.last_run_id
                    else None
                )
            if not task.enabled:
                if manual:
                    raise ValueError(f"Scheduled task '{task.id}' is disabled")
                return None
            if not manual and (task.next_run_at is None or task.next_run_at > now):
                # Already advanced by a concurrent manual fire.
                return None

            scheduled_for = now if manual else (task.next_run_at or now)
            # Stamp BEFORE the side effect (crash-idempotent, same pattern as
            # resident agents): persist last_run_at / run_count / next_run_at
            # first so a crash does not re-fire or respawn. One-shot tasks are
            # disabled after firing.
            task.last_run_at = now
            task.run_count += 1
            task.updated_at = now
            if task.run_at is not None:
                task.enabled = False
                task.next_run_at = None
            else:
                task.next_run_at = self._compute_next_run(task, now)
            chat_run: Optional[ScheduledTaskRun] = None
            if task.kind == ScheduledTaskKind.CHAT_TURN:
                chat_run = self._queue_scheduled_chat_run(task, scheduled_for, now)
            self._save_scheduled_tasks()

            try:
                if task.kind == ScheduledTaskKind.CHAT_TURN:
                    assert chat_run is not None
                    await self._drain_scheduled_chat_runs(task.tab_id)
                elif task.kind == ScheduledTaskKind.TAB_MESSAGE:
                    await self._send_tab_message(task.tab_id, task.message)
                elif task.kind == ScheduledTaskKind.NEW_SESSION:
                    await self._fire_new_session(task)
                elif task.kind == ScheduledTaskKind.HUB_TASK:
                    await self._fire_hub_task(task)
                if task.kind != ScheduledTaskKind.CHAT_TURN:
                    task.last_status = "ok"
                    task.last_error = None
            except Exception as exc:
                logger.exception("Scheduled task %s failed to fire", task.id)
                task.last_status = "error"
                task.last_error = str(exc)

            task.updated_at = _wm._now()
            self._save_scheduled_tasks()
            return chat_run

    @staticmethod
    def _chat_run_active(run: ScheduledTaskRun) -> bool:
        return run.status in {
            ScheduledTaskRunStatus.QUEUED,
            ScheduledTaskRunStatus.WAITING,
            ScheduledTaskRunStatus.DISPATCHING,
            ScheduledTaskRunStatus.RUNNING,
        }

    def _queue_scheduled_chat_run(
        self, task: ScheduledTask, scheduled_for: datetime, now: datetime
    ) -> ScheduledTaskRun:
        """Create one durable Chat run for one schedule occurrence."""
        assert task.tab_id is not None
        assert task.message is not None
        run_uuid = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"claude-hub:scheduled-chat:{task.id}:{scheduled_for.isoformat()}",
        )
        existing = self.scheduled_task_runs.get(str(run_uuid))
        if existing is not None:
            # A deterministic id makes replaying the exact same occurrence
            # idempotent without collapsing distinct future occurrences.
            task.last_run_id = existing.id
            task.last_status = existing.status.value
            task.last_error = existing.error or existing.waiting_reason
            return existing
        # Supersede blocked occurrences: a target Chat accepts one turn at a
        # time, so while it is busy/archived every interval occurrence just
        # accumulates in the FIFO and would replay as a burst of identical,
        # stale prompts once it frees up. Mark every earlier occurrence of the
        # SAME task that was never dispatched (queued/waiting) as auditable
        # SKIPPED and keep only the newest. A DISPATCHING/RUNNING occurrence
        # (or another task's run on the same tab) is always retained.
        new_key = (scheduled_for, now, str(run_uuid))
        superseded = [
            candidate
            for candidate in self.scheduled_task_runs.values()
            if candidate.scheduled_task_id == task.id
            and candidate.dispatched_at is None
            and candidate.status in {ScheduledTaskRunStatus.QUEUED, ScheduledTaskRunStatus.WAITING}
            and (candidate.scheduled_for, candidate.queued_at, candidate.id) < new_key
        ]
        for older in superseded:
            older.status = ScheduledTaskRunStatus.SKIPPED
            older.completed_at = now
            older.waiting_reason = None
            older.error = "superseded by a newer occurrence of this schedule"
        run = ScheduledTaskRun(
            id=str(run_uuid),
            scheduled_task_id=task.id,
            tab_id=task.tab_id,
            client_turn_id=f"scheduled-{run_uuid}",
            message=task.message,
            scheduled_for=scheduled_for,
            queued_at=now,
        )
        active_for_tab = sum(
            1
            for candidate in self.scheduled_task_runs.values()
            if candidate.tab_id == task.tab_id and self._chat_run_active(candidate)
        )
        if active_for_tab >= self._SCHEDULED_CHAT_ACTIVE_RUN_LIMIT:
            run.status = ScheduledTaskRunStatus.SKIPPED
            run.completed_at = now
            run.error = (
                "target Chat backlog limit reached "
                f"({self._SCHEDULED_CHAT_ACTIVE_RUN_LIMIT} active runs)"
            )
        self.scheduled_task_runs[run.id] = run
        task.last_run_id = run.id
        task.last_status = run.status.value
        task.last_error = run.error
        return run

    async def _scheduled_turn_lifecycle(self, run: ScheduledTaskRun) -> tuple[bool, Optional[str]]:
        """Return (turn_started, completion_status) from the durable transcript."""
        from ..agent_stream.store import AgentStreamStore

        store = AgentStreamStore("terminal-tabs", f"terminal-tab-{run.tab_id}")
        cursor = -1
        started = False
        while True:
            page = await store.read_since(cursor, limit=5000)
            for event in page.events:
                if event.turn_id != run.client_turn_id:
                    continue
                if event.type.value == "turn_started":
                    started = True
                elif event.type.value == "turn_completed":
                    return started, str(event.payload.get("status") or "failed")
                elif event.type.value == "error":
                    # Every persisted ERROR edge is terminal-by-construction
                    # (tailer publishes it immediately before turn_completed).
                    # If a non-terminal error event is ever introduced, this
                    # (and cold-start recovery) would fail the turn early.
                    return started, "failed"
            if not page.has_more:
                return started, None
            cursor = page.next_sequence

    async def _recover_scheduled_chat_runs(self) -> None:
        """Reconcile dispatch markers left by a previous backend process."""
        changed = False
        for run in self.scheduled_task_runs.values():
            if run.status not in {
                ScheduledTaskRunStatus.DISPATCHING,
                ScheduledTaskRunStatus.RUNNING,
            }:
                continue
            started, completion = await self._scheduled_turn_lifecycle(run)
            if completion is not None:
                self._complete_scheduled_chat_run(run, completion)
            elif started:
                run.status = ScheduledTaskRunStatus.UNCERTAIN
                run.waiting_reason = None
                run.error = "backend restarted while this Chat turn was in flight"
                run.completed_at = _wm._now()
                self._sync_task_from_run(run)
            else:
                run.status = ScheduledTaskRunStatus.QUEUED
                run.waiting_reason = "recovered before provider dispatch"
                run.error = None
                self._sync_task_from_run(run)
            changed = True
        if changed:
            self._save_scheduled_tasks()

    async def _reap_stale_scheduled_chat_runs(self, now: datetime) -> None:
        """Live-process safety net for a run whose terminal edge was missed.

        Cold restart reconciles every DISPATCHING/RUNNING marker once, but a
        turn can also die while the backend keeps running (the provider
        process exits and is respawned, an orphan is terminalized, etc.). The
        normal completion observer should always fire in that case; this is the
        backstop when it does not.

        A run older than the grace period is reconciled against the durable
        transcript first (authoritative completion evidence). If no terminal
        edge exists, the injected live liveness probe decides: the provider
        still owns the turn -> leave it alone; otherwise mark it uncertain so
        the FIFO drain can dispatch the next queued occurrence instead of
        blocking forever.
        """
        candidates = [
            run
            for run in self.scheduled_task_runs.values()
            if run.status in {ScheduledTaskRunStatus.DISPATCHING, ScheduledTaskRunStatus.RUNNING}
        ]
        if not candidates:
            return
        # Drop throttle stamps for runs that have since gone terminal.
        self._sched_reap_checked_at = {
            run_id: stamp
            for run_id, stamp in self._sched_reap_checked_at.items()
            if (run := self.scheduled_task_runs.get(run_id)) is not None
            and self._chat_run_active(run)
        }
        changed = False
        affected_tabs: set[str] = set()
        for run in candidates:
            stamp = run.dispatched_at or run.queued_at
            if now - stamp < self._SCHEDULED_RUN_STALE_GRACE:
                continue
            last_check = self._sched_reap_checked_at.get(run.id)
            if last_check is not None and now - last_check < self._SCHEDULED_REAP_RECHECK:
                continue
            self._sched_reap_checked_at[run.id] = now
            # Scan the durable transcript WITHOUT the per-tab lock: a full
            # history can span many 5000-event pages, and the same lock gates
            # completion observers and dispatch for that tab. Re-check run
            # state under the lock before acting on the result.
            try:
                started, completion = await self._scheduled_turn_lifecycle(run)
            except Exception:
                logger.exception("Stale scheduled run %s transcript reconciliation failed", run.id)
                continue
            if completion is not None:
                async with self._scheduled_chat_tab_locks.setdefault(run.tab_id, asyncio.Lock()):
                    if run.status not in {
                        ScheduledTaskRunStatus.DISPATCHING,
                        ScheduledTaskRunStatus.RUNNING,
                    }:
                        continue
                    self._complete_scheduled_chat_run(run, completion)
                    changed = True
                    affected_tabs.add(run.tab_id)
                continue
            # Live probe (tailer send-lock, not the scheduler tab lock).
            liveness = await self._probe_scheduled_turn_liveness(run)
            if liveness in {"active", "terminalized", "unknown"}:
                # active: a turn owns the provider guard (this scheduled turn
                # or a newer unrelated manual turn — both mean "do not reap").
                # terminalized: the orphan was just repaired; its completion
                # observer is in flight. unknown: no tailer / a different
                # orphan open / probe error — fail safe and wait.
                continue
            # Only "dead" reaches here: provider idle, no open orphan.
            async with self._scheduled_chat_tab_locks.setdefault(run.tab_id, asyncio.Lock()):
                # Status may have changed during the scan/probe awaits.
                if run.status not in {
                    ScheduledTaskRunStatus.DISPATCHING,
                    ScheduledTaskRunStatus.RUNNING,
                }:
                    continue
                if started:
                    run.status = ScheduledTaskRunStatus.UNCERTAIN
                    run.error = "Chat turn stopped reporting while it was in flight"
                    run.completed_at = now
                else:
                    # Never started and the provider is idle: requeue so the
                    # drain delivers it again.
                    run.status = ScheduledTaskRunStatus.QUEUED
                    run.waiting_reason = "redelivered after a stalled dispatch"
                    run.error = None
                    run.completed_at = None
                self._sync_task_from_run(run)
                changed = True
                affected_tabs.add(run.tab_id)
        if changed:
            self._save_scheduled_tasks()
            for tab_id in affected_tabs:
                await self._drain_scheduled_chat_runs(tab_id)

    async def _probe_scheduled_turn_liveness(self, run: ScheduledTaskRun) -> str:
        """Ask the live Chat transport whether ``run``'s turn is still owned.

        Returns ``"active"`` (the provider guard is raised — by this turn or a
        newer unrelated one), ``"dead"`` (provider idle, no open orphan),
        ``"terminalized"`` (this run's orphan was just closed), or
        ``"unknown"`` (no probe / different orphan open / error), in which case
        the caller must be conservative and leave the run alone.
        """
        probe = self._scheduled_chat_liveness
        if probe is None:
            return "unknown"
        try:
            result = await probe(run.tab_id, run.client_turn_id)
        except Exception:
            logger.exception("Scheduled run liveness probe failed for tab=%s", run.tab_id)
            return "unknown"
        return result if result in {"active", "dead", "terminalized"} else "unknown"

    def configure_scheduled_chat_liveness(self, callback: Any) -> None:
        """Inject the live Chat turn-liveness probe without importing the API."""
        self._scheduled_chat_liveness = callback

    def _cancel_scheduled_task_runs(
        self,
        task_id: str,
        *,
        statuses: set[ScheduledTaskRunStatus],
        reason: str,
        now: datetime,
    ) -> int:
        """Move matching active runs of one task to CANCELLED. Returns count."""
        count = 0
        for run in self.scheduled_task_runs.values():
            if run.scheduled_task_id == task_id and run.status in statuses:
                run.status = ScheduledTaskRunStatus.CANCELLED
                run.error = reason
                run.waiting_reason = None
                run.completed_at = now
                count += 1
        return count

    def _sync_task_from_run(self, run: ScheduledTaskRun) -> None:
        task = self.scheduled_tasks.get(run.scheduled_task_id)
        if task is None:
            return
        current = (
            self.scheduled_task_runs.get(task.last_run_id) if task.last_run_id is not None else None
        )
        if current is not None and current.id != run.id:
            current_key = (current.scheduled_for, current.queued_at, current.id)
            run_key = (run.scheduled_for, run.queued_at, run.id)
            if current_key > run_key:
                # An older FIFO entry can keep transitioning after a newer
                # occurrence is queued. Keep task-level "last" fields tied
                # to the newest occurrence; its own run record still captures
                # every transition.
                return
        task.last_run_id = run.id
        task.last_status = run.status.value
        task.last_error = run.error or run.waiting_reason
        task.updated_at = _wm._now()

    def _complete_scheduled_chat_run(self, run: ScheduledTaskRun, status: str) -> None:
        if status in {"completed", "success", "ok"}:
            run.status = ScheduledTaskRunStatus.COMPLETED
            run.error = None
        elif status == "cancelled":
            # The turn (or its backend runtime) was stopped/interrupted. This
            # is distinct from a model-side failure: surface it as cancelled
            # rather than a red "failed" fire.
            run.status = ScheduledTaskRunStatus.CANCELLED
            run.error = "Chat turn was cancelled or its backend runtime was lost"
        else:
            run.status = ScheduledTaskRunStatus.FAILED
            run.error = f"Chat turn completed with status: {status}"
        run.completed_at = _wm._now()
        run.waiting_reason = None
        self._sync_task_from_run(run)

    async def on_scheduled_chat_turn_completed(
        self, tab_id: str, turn_id: str, status: str
    ) -> None:
        """Advance a scheduled run after its persisted Chat completion edge."""
        lock = self._scheduled_chat_tab_locks.setdefault(tab_id, asyncio.Lock())
        async with lock:
            run = next(
                (
                    candidate
                    for candidate in self.scheduled_task_runs.values()
                    if candidate.tab_id == tab_id and candidate.client_turn_id == turn_id
                ),
                None,
            )
            if run is None or not self._chat_run_active(run):
                return
            self._complete_scheduled_chat_run(run, status)
            self._save_scheduled_tasks()
        await self._drain_scheduled_chat_runs(tab_id)

    async def _drain_scheduled_chat_runs(self, tab_id: Optional[str] = None) -> None:
        tab_ids = (
            [tab_id]
            if tab_id is not None
            else sorted(
                {
                    run.tab_id
                    for run in self.scheduled_task_runs.values()
                    if self._chat_run_active(run)
                }
            )
        )
        for current_tab_id in tab_ids:
            await self._drain_scheduled_chat_tab(current_tab_id)

    async def _drain_scheduled_chat_tab(self, tab_id: str) -> None:
        lock = self._scheduled_chat_tab_locks.setdefault(tab_id, asyncio.Lock())
        async with lock:
            cancelled_any = False
            while True:
                active = sorted(
                    (
                        run
                        for run in self.scheduled_task_runs.values()
                        if run.tab_id == tab_id and self._chat_run_active(run)
                    ),
                    key=lambda run: (run.scheduled_for, run.queued_at, run.id),
                )
                if not active:
                    if cancelled_any:
                        self._save_scheduled_tasks()
                    return
                if any(
                    run.status
                    in {ScheduledTaskRunStatus.DISPATCHING, ScheduledTaskRunStatus.RUNNING}
                    for run in active
                ):
                    if cancelled_any:
                        self._save_scheduled_tasks()
                    return
                run = active[0]
                task = self.scheduled_tasks.get(run.scheduled_task_id)
                if task is None:
                    run.status = ScheduledTaskRunStatus.CANCELLED
                    run.error = "scheduled task was deleted"
                    run.completed_at = _wm._now()
                    cancelled_any = True
                    continue
                if not task.enabled:
                    # Disabling an automation is a Stop: queued occurrences must
                    # not fire when the queue drains (e.g. once a previously
                    # running turn finally ends). Nothing is DISPATCHING/RUNNING
                    # here (early return above), so all active runs are pending.
                    self._cancel_scheduled_task_runs(
                        task.id,
                        statuses={
                            ScheduledTaskRunStatus.QUEUED,
                            ScheduledTaskRunStatus.WAITING,
                        },
                        reason="automation is disabled",
                        now=_wm._now(),
                    )
                    cancelled_any = True
                    continue
                break
            tab = ttyd_manager.get_tab(tab_id)
            if tab is None:
                reason = "target Chat was deleted"
                run.status = ScheduledTaskRunStatus.SKIPPED
                run.error = reason
                run.completed_at = _wm._now()
                task.enabled = False
                # The target is gone: every queued occurrence is unfulfillable.
                self._cancel_scheduled_task_runs(
                    task.id,
                    statuses={
                        ScheduledTaskRunStatus.QUEUED,
                        ScheduledTaskRunStatus.WAITING,
                        ScheduledTaskRunStatus.DISPATCHING,
                        ScheduledTaskRunStatus.RUNNING,
                    },
                    reason=reason,
                    now=_wm._now(),
                )
                self._sync_task_from_run(run)
                self._save_scheduled_tasks()
                return
            if tab.session_kind != SessionKind.CHAT or tab.workspace_role is not None:
                reason = "target is no longer a top-level Chat session"
                run.status = ScheduledTaskRunStatus.SKIPPED
                run.error = reason
                run.completed_at = _wm._now()
                task.enabled = False
                self._cancel_scheduled_task_runs(
                    task.id,
                    statuses={ScheduledTaskRunStatus.QUEUED, ScheduledTaskRunStatus.WAITING},
                    reason=reason,
                    now=_wm._now(),
                )
                self._sync_task_from_run(run)
                self._save_scheduled_tasks()
                return
            if tab.agent_type != task.agent_type:
                reason = "target Chat backend changed; edit the automation to confirm it"
                run.status = ScheduledTaskRunStatus.SKIPPED
                run.error = reason
                run.completed_at = _wm._now()
                task.enabled = False
                # Re-confirmation required: do not replay the queued backlog
                # against the new backend after the user edits the task.
                self._cancel_scheduled_task_runs(
                    task.id,
                    statuses={ScheduledTaskRunStatus.QUEUED, ScheduledTaskRunStatus.WAITING},
                    reason=reason,
                    now=_wm._now(),
                )
                self._sync_task_from_run(run)
                self._save_scheduled_tasks()
                return
            if tab.archived:
                run.status = ScheduledTaskRunStatus.WAITING
                run.waiting_reason = "target Chat is archived"
                self._sync_task_from_run(run)
                self._save_scheduled_tasks()
                return

            from ..goal_run import get_goal_admission_lock, get_goal_manager

            async with get_goal_admission_lock(tab_id):
                goal = get_goal_manager().current(tab_id)
                if goal is not None and (
                    goal.status.value == "active" or goal.dispatch_state.value != "idle"
                ):
                    run.status = ScheduledTaskRunStatus.WAITING
                    run.waiting_reason = "waiting for the active Goal"
                    self._sync_task_from_run(run)
                    self._save_scheduled_tasks()
                    return

                dispatch = self._scheduled_chat_dispatch
                if dispatch is None:
                    run.status = ScheduledTaskRunStatus.WAITING
                    run.waiting_reason = "Chat dispatcher is starting"
                    self._sync_task_from_run(run)
                    self._save_scheduled_tasks()
                    return
                run.status = ScheduledTaskRunStatus.DISPATCHING
                run.waiting_reason = None
                run.error = None
                self._sync_task_from_run(run)
                self._save_scheduled_tasks()
                try:
                    await dispatch(run, task)
                except RuntimeError as exc:
                    if "turn is already in flight" in str(exc):
                        run.status = ScheduledTaskRunStatus.WAITING
                        run.waiting_reason = "waiting for the current Chat response"
                        run.error = None
                    else:
                        run.status = ScheduledTaskRunStatus.FAILED
                        run.error = str(exc)
                        run.completed_at = _wm._now()
                except Exception as exc:
                    run.status = ScheduledTaskRunStatus.FAILED
                    run.error = str(exc)
                    run.completed_at = _wm._now()
                else:
                    # A very fast provider may complete before the send
                    # acknowledgement returns. Preserve that terminal evidence.
                    if run.status == ScheduledTaskRunStatus.DISPATCHING:
                        run.status = ScheduledTaskRunStatus.RUNNING
                        run.dispatched_at = _wm._now()
                self._sync_task_from_run(run)
                self._save_scheduled_tasks()

    async def _send_tab_message(self, tab_id: Optional[str], message: Optional[str]) -> None:
        """Type ``message`` into a terminal tab's pane and submit it (Enter).

        Used by the ``tab_message`` scheduled-task kind. Every tab (plain
        terminal or managed agent) owns a tmux session named
        ``claude-hub-<tab_id[:8]>``, so the same tmux paste path that
        delivers managed-session messages can target any tab.

        The fields are ``Optional`` because the durable schema allows nulls;
        create/update validation guarantees they are set for this kind, and
        the guards below re-check defensively.

        Delivery is best-effort: the message is pasted and submitted, but
        there is no worker-ACK state machine (unlike
        ``send_session_message``). A tab whose tmux session is gone raises,
        which the fire path records as ``last_status=error``.
        """
        from ..ttyd_manager import _tmux_session_name

        if not tab_id:
            raise ValueError("tab_id is required")
        if not message:
            raise ValueError("message is required")
        if ttyd_manager.get_tab(tab_id) is None:
            raise ValueError(f"Terminal tab '{tab_id}' not found")
        await self._send_tmux_message(_tmux_session_name(tab_id), message)

    async def _best_effort_delete_session(self, session_id: str) -> None:
        """Best-effort teardown of an ephemeral session; never raises."""
        try:
            await self.delete_session(session_id)
        except Exception:
            logger.exception("Best-effort delete of scheduled-task session %s failed", session_id)

    async def _fire_new_session(self, task: ScheduledTask) -> None:
        """Create a new session in the workspace and send it the message."""
        session = await self.ensure_workspace_agent(
            task.workspace_id,
            EnsureWorkspaceAgentRequest(
                agent_type=task.agent_type,
                ephemeral=True,
                caller_owned_ephemeral=True,
                role=WorkspaceSessionRole.ORCHESTRATOR,
                reuse_existing=False,
            ),
        )
        try:
            await self.send_session_message(session.id, task.message)
        except Exception:
            # The send failed; tear down the just-created ephemeral session so
            # a fire failure does not strand an idle orchestrator.
            await self._best_effort_delete_session(session.id)
            raise

    async def _fire_hub_task(self, task: ScheduledTask) -> None:
        """Publish a system-internal task on a caller-owned ephemeral orchestrator.

        The task runs through the normal reviewed-task flow; when the worker
        reports completion, ``_handle_internal_task_report`` marks it DONE
        (skipping human review) and the auto-cleanup hook deletes the ephemeral
        session so no agent / reviewer resources are held.
        """
        now = _wm._now()
        # Validation at create/update time guarantees task_title and message are
        # set for hub_task; assert so the type checker sees non-Optional str.
        assert task.task_title is not None
        assert task.message is not None
        internal_task = self._create_task(
            task.workspace_id,
            WorkspaceTaskCreate(
                title=task.task_title,
                prompt=task.message,
                task_mode=WorkspaceTaskMode.REVIEWED,
                execution_complexity=WorkspaceTaskExecutionComplexity.AUTO,
                agent_type=task.agent_type,
            ),
            system_internal=True,
            internal_kind="scheduled",
        )
        session = await self.ensure_workspace_agent(
            task.workspace_id,
            EnsureWorkspaceAgentRequest(
                agent_type=task.agent_type,
                ephemeral=True,
                caller_owned_ephemeral=True,
                role=WorkspaceSessionRole.ORCHESTRATOR,
                reuse_existing=False,
            ),
        )
        internal_task = internal_task.model_copy(
            update={
                "status": WorkspaceTaskStatus.QUEUED,
                "queued_at": now,
                "dispatch_attempt": internal_task.dispatch_attempt + 1,
                "dispatch_reason": "scheduled",
                "updated_at": now,
            }
        )
        self.tasks[internal_task.id] = internal_task
        try:
            await self._dispatch_task_to_session(internal_task, session)
        except Exception:
            # Mark the internal task failed and tear down the ephemeral
            # orchestrator so a dispatch failure strands neither a task nor a
            # session.
            self.tasks[internal_task.id] = internal_task.model_copy(
                update={"status": WorkspaceTaskStatus.FAILED, "updated_at": _wm._now()}
            )
            await self._best_effort_delete_session(session.id)
            raise
