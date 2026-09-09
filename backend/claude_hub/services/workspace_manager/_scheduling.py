"""Scheduled-task orchestration: cron / interval / one-off firing.

A ``ScheduledTask`` is a durable schedule that fires an action when its
next-run time arrives. Three kinds are supported:

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
/ ``next_run_at`` are stamped and persisted (the same stamp-before-side-effect
pattern used by resident agents), so a crash between the persist and the
send side-effect does not re-fire or respawn.
"""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ._constants import *  # noqa: F401,F403


class _SchedulingMixin:
    """Scheduled-task persistence, CRUD, scheduling, and firing."""

    # Minimum gap between fires of the same task. A concurrent fire (tick vs
    # run-now, or two run-now calls) that lands within this window joins the
    # in-flight fire instead of re-firing. This is the guard that stops a
    # recurring task from double-firing on concurrent manual run-now calls,
    # where the ``enabled`` / ``next_run_at`` re-checks don't apply.
    _FIRE_COOLDOWN = timedelta(seconds=1)

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
        logger.info("Loaded %d scheduled task(s)", len(self.scheduled_tasks))

    def _save_scheduled_tasks(self) -> None:
        SCHEDULED_TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "scheduled_tasks": [
                task.model_dump(mode="json") for task in self.scheduled_tasks.values()
            ]
        }
        self._atomic_write_text(
            SCHEDULED_TASKS_FILE, json.dumps(payload, ensure_ascii=False, indent=2)
        )

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
            agent_type=payload.agent_type,
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

        if schedule_changed:
            merged.next_run_at = self._compute_next_run(merged, now)

        self.scheduled_tasks[task_id] = merged
        self._save_scheduled_tasks()
        logger.info("Updated scheduled task id=%s name=%r", task_id, merged.name)
        return merged

    def delete_scheduled_task(self, task_id: str) -> bool:
        task = self.scheduled_tasks.pop(task_id, None)
        if task is None:
            return False
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
        await self._fire_scheduled_task(task, _wm._now(), manual=True)
        if task.last_status == "error":
            raise RuntimeError(task.last_error or "scheduled task failed to fire")
        return task

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

        if kind == ScheduledTaskKind.TAB_MESSAGE:
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
        if not self.scheduled_tasks:
            return
        now = _wm._now()
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

    async def _fire_scheduled_task(
        self, task: ScheduledTask, now: datetime, *, manual: bool = False
    ) -> None:
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
                return
            if not task.enabled:
                if manual:
                    raise ValueError(f"Scheduled task '{task.id}' is disabled")
                return
            if not manual and (task.next_run_at is None or task.next_run_at > now):
                # Already advanced by a concurrent manual fire.
                return

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
            self._save_scheduled_tasks()

            try:
                if task.kind == ScheduledTaskKind.TAB_MESSAGE:
                    await self._send_tab_message(task.tab_id, task.message)
                elif task.kind == ScheduledTaskKind.NEW_SESSION:
                    await self._fire_new_session(task)
                elif task.kind == ScheduledTaskKind.HUB_TASK:
                    await self._fire_hub_task(task)
                task.last_status = "ok"
                task.last_error = None
            except Exception as exc:
                logger.exception("Scheduled task %s failed to fire", task.id)
                task.last_status = "error"
                task.last_error = str(exc)

            task.updated_at = _wm._now()
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
