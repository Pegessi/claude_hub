"""Scheduled-task implementation split from the compatibility mixin."""

from .._constants import *  # noqa: F401,F403
from ._contract import _SchedulingHost


class _ScheduleRegistryMixin(_SchedulingHost):
    _SCHEDULED_CHAT_COLD_REDRAIN_DELAY = timedelta(seconds=5)
    _CRON_FIELD_RANGES = (
        (0, 59),
        (0, 23),
        (1, 31),
        (1, 12),
        (0, 6),
    )
    _CRON_DAY_FULL = frozenset(range(1, 32))
    _CRON_DOW_FULL = frozenset(range(0, 7))

    def create_scheduled_task(self, payload: ScheduledTaskCreate) -> ScheduledTask:
        fields = payload.model_dump()
        self._validate_scheduled_task_fields(fields)
        name = payload.name.strip()
        if not name:
            raise ValueError("name must not be empty")
        env_preset = payload.env_preset.strip() if payload.env_preset else None
        now = self._runtime_now()
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
            env_preset=env_preset,
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

    def configure_scheduled_chat_dispatch(self, callback: ScheduledChatDispatch) -> None:
        """Inject the native Chat transport without importing the API layer."""
        self._scheduled_chat_dispatch = callback

    def configure_scheduled_chat_readiness(self, callback: ScheduledChatReadiness) -> None:
        """Inject the native Chat runtime readiness probe without importing API.

        The callback is ``async (tab_id) -> bool``: it ensures the target Chat's
        native tailer/provider exists (spawning a cold one and waiting until it
        is ready) and returns ``True`` when a turn can be delivered, ``False``
        when it is still starting.
        """
        self._scheduled_chat_readiness = callback

    def _schedule_chat_redrain(self, tab_id: str) -> None:
        """Re-run one tab's FIFO drain after the cold-start backoff.

        Self-scheduling and de-duplicated per tab: only one delayed redrain is
        ever pending for a given tab, so a backlog of cold runs cannot enqueue
        an unbounded number of wakeups.
        """
        existing = self._sched_chat_redrain_tasks.get(tab_id)
        if existing is not None and not existing.done():
            return

        async def _redrain() -> None:
            try:
                await asyncio.sleep(self._SCHEDULED_CHAT_COLD_REDRAIN_DELAY.total_seconds())
                await self._drain_scheduled_chat_runs(tab_id)
            except asyncio.CancelledError:  # pragma: no cover - shutdown cancellation
                raise
            except Exception:
                logger.exception("Scheduled Chat cold-start redrain failed for tab=%s", tab_id)
            finally:
                self._sched_chat_redrain_tasks.pop(tab_id, None)

        self._sched_chat_redrain_tasks[tab_id] = asyncio.create_task(
            _redrain(), name=f"sched-chat-cold-redrain-{tab_id[:8]}"
        )

    async def _ensure_scheduled_chat_runtime_ready(self, tab_id: str) -> bool:
        """Ensure the target Chat's native runtime is up before delivery.

        Returns ``True`` when a turn can be delivered now. When no readiness
        callback is wired we cannot verify, so we optimistically allow delivery
        (preserves the pre-callback behavior used by tests).
        """
        readiness = self._scheduled_chat_readiness
        if readiness is None:
            return True
        try:
            return bool(await readiness(tab_id))
        except Exception:
            logger.exception("Scheduled Chat runtime readiness probe failed for tab=%s", tab_id)
            return False

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
        if "env_preset" in updates and isinstance(updates["env_preset"], str):
            updates["env_preset"] = updates["env_preset"].strip() or None

        schedule_keys = ("run_at", "cron", "interval_seconds")
        schedule_changed = any(k in updates for k in schedule_keys)
        if schedule_changed:
            # Normalize to exactly one schedule field: clear the others so a
            # switch from cron to run_at (etc.) does not leave two set.
            for key in schedule_keys:
                updates.setdefault(key, None)

        now = self._runtime_now()
        merged = task.model_copy(update={**updates, "updated_at": now})

        if (
            "env_preset" not in updates
            and merged.env_preset
            and merged.kind not in (ScheduledTaskKind.NEW_SESSION, ScheduledTaskKind.HUB_TASK)
        ):
            # A preset on a non-session kind can only come from a hand-edited
            # persistence file (the API rejects it). Drop the stale value here
            # so it cannot lock out every future edit/disable of the schedule,
            # which all re-run validation against the merged record.
            merged = merged.model_copy(update={"env_preset": None})

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
        chat_run = await self._fire_scheduled_task(task, self._runtime_now(), manual=True)
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
            now = self._runtime_now()
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
            now=self._runtime_now(),
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

        env_preset = fields.get("env_preset")
        if env_preset:
            if kind not in (ScheduledTaskKind.NEW_SESSION, ScheduledTaskKind.HUB_TASK):
                raise ValueError("env_preset is only valid for new_session / hub_task tasks")
            # Resolve at create/update time so an unknown preset fails the
            # request instead of silently failing on every fire.
            from ...env_preset_resolver import (
                EnvPresetNotFoundError,
                EnvPresetParseError,
                resolve_env_preset,
            )

            try:
                resolve_env_preset(env_preset)
            except (EnvPresetNotFoundError, EnvPresetParseError) as exc:
                raise ValueError(str(exc)) from None

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
