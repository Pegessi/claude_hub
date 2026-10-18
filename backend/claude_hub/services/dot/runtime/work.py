"""Dot delegation, review learning, and watched-work lifecycle."""

from abc import ABC, abstractmethod

from claude_hub.services.dot.runtime.common import (
    _ATTENTION_REPORT_STATES,
    _QUESTION_REPORT_STATES,
    _STALL_TASK_STATES,
    _TERMINAL_TASK_STATES,
    BRIEF_TEMPLATE,
    DEFAULT_STALL_HOURS,
    REVIEW_CAUSES,
    REVIEW_VERDICTS,
    STATS_WINDOW,
    Any,
    Card,
    DelegateNotStarted,
    LessonNotFound,
    Optional,
    _checklist,
    _clip_text,
    _continue_report,
    _ended_at,
    _first_line,
    _hub_report,
    _parse_time,
    _report_state,
    _worker_report,
    _worker_return,
    asyncio,
    datetime,
    lesson_score,
    lint_brief,
    logger,
    now_iso,
    same_rule,
    session_rules,
    time,
    timedelta,
)
from claude_hub.services.dot.runtime.session import DotSessionService


class DotWorkService(DotSessionService, ABC):
    @abstractmethod
    async def wake(self) -> bool:
        """Wake the Dot chat after tracked work changes."""
        ...

    async def delegate(
        self,
        *,
        workspace_id: str,
        title: str,
        goal: str,
        done_when: list[str],
        output: str = "",
        context: str = "",
        out_of_scope: str = "",
        constraints: str = "",
        agent_type: str = "codex",
        task_mode: Optional[str] = None,
        start: bool = True,
        request_key: Optional[str] = None,
        stall_hours: Optional[float] = None,
        confirm: bool = False,
        actor_key: str,
    ) -> dict[str, Any]:
        """Check the brief, then register (and start) one Hub Task and track it.

        Returns ``held`` (nothing sent), the brief ``warnings`` and the tracked
        ``work`` item. A brief with warnings is held until Dot fixes it or sends
        ``confirm``. A retry with a known ``request_key`` is not checked again
        and leaves the tracked status to the watcher.
        """
        from ....models.schemas import (
            AgentType,
            StartTaskRequest,
            TaskSourceReference,
            WorkspaceTaskCreate,
            WorkspaceTaskMode,
        )
        from ... import workspace_manager
        from ...workspace_manager._task_execution import TaskExecutionConflict

        checks = [" ".join(item.split()) for item in done_when if item.strip()]
        if not title.strip() or not goal.strip() or not checks:
            raise ValueError("title, goal and done_when are required")
        self.store.ensure()
        warnings = lint_brief(done_when=checks, context=context, output=output)
        replay = bool(request_key) and any(
            item.get("request_key") == request_key
            for item in self.store.list_work(include_closed=True)
        )
        if warnings and not (confirm or replay):
            return {"held": True, "warnings": warnings, "work": None}
        brief = BRIEF_TEMPLATE.format(
            goal=goal.strip(),
            context=context.strip() or "(none given)",
            done_when="\n".join(f"{number}. {item}" for number, item in enumerate(checks, 1)),
            output=output.strip() or "(not given; say in the report where the result is)",
            out_of_scope=out_of_scope.strip() or "(none given)",
            constraints=constraints.strip() or "(none)",
        )
        fields: dict[str, Any] = {
            "title": title.strip(),
            "prompt": brief,
            "agent_type": AgentType(agent_type),
            "request_key": request_key,
        }
        tab_id = self.tab_id()
        if tab_id:
            fields["source"] = TaskSourceReference(kind="chat", tab_id=tab_id)
        if task_mode:
            fields["task_mode"] = WorkspaceTaskMode(task_mode)
        task, _replayed = await workspace_manager.register_task(
            workspace_id, WorkspaceTaskCreate(**fields), actor_key
        )
        # Tracked before it starts: a task that fails to start is still Dot's.
        item = self.store.upsert_work(
            task.id,
            workspace_id=workspace_id,
            title=task.title,
            goal=goal.strip(),
            done_when=checks,
            output=output.strip() or None,
            agent_type=agent_type,
            # A replayed task may have moved on since the last poll; the watcher must
            # still see that change, or its result event is lost.
            status=None if replay else task.status.value,
            stall_hours=stall_hours,
            request_key=request_key,
            # Kept so a later review can see which warnings Dot sent past.
            brief_warnings=warnings or None,
        )
        try:
            if start and task.status.value == "todo":
                try:
                    task = await workspace_manager.start_task(
                        task.id, StartTaskRequest(agent_type=AgentType(agent_type))
                    )
                except TaskExecutionConflict:
                    raise
                except Exception as exc:
                    raise DelegateNotStarted(
                        f"Task {task.id} was created but did not start: {exc}"
                    ) from exc
                # Outside the "did not start" net: it did start, whatever this write does.
                if not replay:
                    item = self.store.upsert_work(task.id, status=task.status.value)
        finally:
            await asyncio.to_thread(self.store.commit, f"dot: delegate {task.id}")
        return {"held": False, "warnings": warnings, "work": item}

    async def review(
        self,
        task_id: str,
        *,
        verdict: str,
        cause: Optional[str] = None,
        note: str = "",
        lesson: str = "",
        lesson_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Record Dot's verdict on a returned task; a brief miss adds or votes a lesson.

        A new rule that says nearly the same as a live lesson votes that lesson
        instead (``lesson_reused``). A redo not blamed on the brief needs a note,
        so blaming the environment, the worker or a changed request is never
        free. Raises KeyError for an unknown task, LessonNotFound for an unknown
        lesson id, ValueError for a bad combination.
        """
        item = self.store.get_work(task_id)
        if item is None:
            raise KeyError(task_id)
        if item.get("watched") or item.get("stopped"):
            raise ValueError("only tasks Dot delegated and still tracks are reviewed")
        if verdict not in REVIEW_VERDICTS:
            raise ValueError("verdict must be ok, redo or skip")
        if cause is not None and cause not in REVIEW_CAUSES:
            raise ValueError("cause must be brief, env, worker or change")
        if verdict == "redo" and cause is None:
            raise ValueError("a redo needs a cause: brief, env, worker or change")
        if verdict == "skip" and cause is not None:
            raise ValueError("a skip has no cause")
        if (verdict == "skip" or (verdict == "redo" and cause != "brief")) and not note.strip():
            raise ValueError(
                "say why in --note"
                + (": what you could not judge" if verdict == "skip" else ": what went wrong")
            )
        if not item.get("result_card"):
            raise ValueError("the task has not come back yet")
        if item.get("review") and not item.get("review_due"):
            raise ValueError("already reviewed; nothing new came back since")
        rule = lesson.strip()
        if (rule or lesson_id) and cause != "brief":
            raise ValueError("lessons are only for brief misses (cause brief)")
        if rule and lesson_id:
            raise ValueError("give a new lesson or a lesson id, not both")
        if cause == "brief" and not (rule or lesson_id):
            raise ValueError(
                "a brief miss needs a lesson: a new rule, or the id of an old one it repeats"
            )
        reused = False
        if rule and (same := same_rule(self.store.list_lessons(), rule)):
            lesson_id, reused = str(same["id"]), True
        saved = None
        if lesson_id:
            saved = self.store.vote_lesson(lesson_id, task_id)
            if saved is None:
                raise LessonNotFound(lesson_id)
        elif rule:
            saved = self.store.add_lesson(rule, task_id)
        record = {
            "verdict": verdict,
            "cause": cause,
            "note": " ".join(note.split()) or None,
            "lesson_id": saved["id"] if saved else None,
            "at": now_iso(),
        }
        changes: dict[str, Any] = {
            "review": {key: value for key, value in record.items() if value is not None},
            "review_due": False,
        }
        # The first verdict decides first-pass success. A skip is no verdict yet.
        if not item.get("first_verdict") and verdict != "skip":
            changes["first_verdict"] = verdict
            changes["first_cause"] = cause or ""
        elif item.get("first_verdict") and verdict == "redo" and cause != "change":
            # An ok that turned out wrong in a later round was not a first pass.
            changes["late_redo"] = True
        updated = self.store.upsert_work(task_id, **changes)
        await asyncio.to_thread(self.store.commit, f"dot: review {task_id} {verdict}")
        return {
            "work": updated,
            "lesson": saved,
            "lesson_reused": reused,
        }

    def lessons(self, *, include_dropped: bool = False) -> list[dict[str, Any]]:
        """Lessons with their current score and reach, strongest first.

        reach: ``always`` (in every session), ``hidden`` (too weak or outside the
        top few; kept as history), ``dropped`` or ``merged``.
        """
        now = self._utc_now()
        in_session = {lesson.get("id") for lesson in session_rules(self.store.list_lessons(), now)}
        rows = []
        for lesson in self.store.list_lessons(include_dropped=include_dropped):
            score = lesson_score(lesson, now)
            if lesson.get("merged_into"):
                reach = "merged"
            elif lesson.get("dropped"):
                reach = "dropped"
            else:
                reach = "always" if lesson.get("id") in in_session else "hidden"
            rows.append(
                {
                    **lesson,
                    "score": round(score, 2),
                    "in_session": reach == "always",
                    "reach": reach,
                }
            )
        return sorted(rows, key=lambda row: (bool(row.get("dropped")), -row["score"]))

    def review_stats(self) -> dict[str, Any]:
        """First-pass rate over the latest reviews: ok at the first verdict, no question asked,
        and no later redo.

        Tasks whose first review said the user changed what they want, and
        skipped ones, are left out. ``causes`` counts the first-round cause, so
        brief misses stand apart from env and worker ones. ``judged`` /
        ``returned`` show how many delegated tasks that came back got a verdict
        at all. ``confirmed`` / ``confirmed_first_pass`` cover briefs sent past
        the brief check's warnings: if they pass as often as the rest, the check
        is not worth its holds.
        """
        delegated = [
            item
            for item in self.store.list_work(include_closed=True)
            if not item.get("watched") and not item.get("stopped") and item.get("result_card")
        ]
        reviewed = [item for item in delegated if isinstance(item.get("review"), dict)]
        reviewed.sort(key=lambda item: str(item["review"].get("at") or ""))

        def first_cause(item: dict[str, Any]) -> str:
            return str(item.get("first_cause", item["review"].get("cause")) or "")

        def first_verdict(item: dict[str, Any]) -> str:
            return str(item.get("first_verdict", item["review"].get("verdict")) or "")

        window = [
            item
            for item in reviewed
            if first_cause(item) != "change" and first_verdict(item) in ("ok", "redo")
        ][-STATS_WINDOW:]

        def passed(item: dict[str, Any]) -> bool:
            return (
                first_verdict(item) == "ok" and not item.get("asked") and not item.get("late_redo")
            )

        first_pass = sum(1 for item in window if passed(item))
        confirmed = [item for item in window if item.get("brief_warnings")]
        causes: dict[str, int] = {}
        for item in window:
            cause = first_cause(item)
            if cause:
                causes[cause] = causes.get(cause, 0) + 1
        return {
            "reviewed": len(window),
            "first_pass": first_pass,
            "rate": round(first_pass / len(window), 2) if window else None,
            "causes": causes,
            "pending": len(self.store.pending_reviews()),
            "judged": len(reviewed),
            "returned": len(delegated),
            "confirmed": len(confirmed),
            "confirmed_first_pass": sum(1 for item in confirmed if passed(item)),
            "window": STATS_WINDOW,
        }

    async def watch(self, task_id: str, *, stall_hours: Optional[float] = None) -> dict[str, Any]:
        """Track an existing Hub task Dot did not start. Raises KeyError / ValueError."""
        from ... import workspace_manager

        task = workspace_manager.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        status = task.status.value
        if status in _TERMINAL_TASK_STATES:
            raise ValueError(f"task is already {status}")
        self.store.ensure()
        existing = self.store.get_work(task.id)
        # Dot's own delegation stays one: it keeps its review and unseen events.
        delegated = existing is not None and not existing.get("watched")
        # A live watch keeps its place: watching it again must not skip unseen reports.
        if (
            existing is not None
            and not existing.get("stopped")
            and (delegated or not existing.get("closed"))
        ):
            return self.store.upsert_work(task.id, stall_hours=stall_hours)
        # Old reports are history; only reports after this point raise events.
        reports = self._reports(task.workspace_id, task.id)
        report = reports[-1] if reports else None
        result = next((old for old in reversed(reports) if _worker_report(old)), None)
        item = self.store.upsert_work(
            task.id,
            workspace_id=task.workspace_id,
            title=task.title,
            agent_type=task.agent_type.value,
            status=status,
            watched=None if delegated else True,
            watched_at=None if delegated else now_iso(),
            closed=False,
            stopped=False,
            last_report_id=getattr(report, "id", None),
            # ... and so is the result: the Hub reviewer going over it is not news.
            announced_report_id=getattr(result, "id", None),
            announced_status=status,
            stall_hours=stall_hours,
        )
        await asyncio.to_thread(self.store.commit, f"dot: watch {task.id}")
        return item

    async def unwatch(self, task_id: str) -> Optional[dict[str, Any]]:
        """Stop tracking a task (watched or delegated). Returns None when unknown."""
        if self.store.get_work(task_id) is None:
            return None
        item = self.store.upsert_work(task_id, closed=True, stopped=True, review_due=False)
        await asyncio.to_thread(self.store.commit, f"dot: unwatch {task_id}")
        return item

    def start(self) -> None:
        tab_id = self.tab_id()
        if tab_id:
            # Bring standing schedules up to date (e.g. drop an old daily wake-up).
            try:
                self.ensure_schedules(tab_id)
            except Exception:
                logger.exception("dot schedule refresh failed")
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(self._watch_loop(), name="dot-watch")

    async def stop(self) -> None:
        task, self._watch_task = self._watch_task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _watch_loop(self) -> None:
        while True:
            try:
                if self.is_set_up():
                    await self._wake_if_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("dot work watcher failed")
            await asyncio.sleep(self._watch_interval_seconds())

    async def _wake_if_due(self) -> None:
        """Wake while events are unread: new ones, or ones a stopped, failed or
        skipped wake-up gave back (a tidy that gave way waits for the next night).
        """
        # Pending: new events, or a wake-up gave way. They go as soon as the pause
        # and any running wake-up allow.
        if await self.check_work():
            self._wake_pending = True
        # The user may still be talking, or the provider is failing: wait a while.
        if time.monotonic() - self._yielded_at < self._yield_pause_seconds():
            return
        if self._wake_in_flight():
            return
        if not self.store.list_inbox():
            # Nothing unread (e.g. a tidy gave way): no wake-up, and none pending.
            self._wake_pending = False
            return
        # Nothing pending since the last try (it failed, was skipped or stopped by
        # hand): try again, but not every minute.
        if (
            not self._wake_pending
            and time.monotonic() - self._woke_at < self._yield_pause_seconds()
        ):
            return
        self._woke_at = time.monotonic()
        self._wake_pending = False
        await self.wake()

    def _wake_in_flight(self) -> bool:
        """A wake-up run is queued or running: it reads the unread events."""
        from ... import workspace_manager

        schedule_id = self._peek_state().get("wake_schedule_id")
        active = {"queued", "waiting", "dispatching", "running"}
        return bool(schedule_id) and any(
            run.scheduled_task_id == schedule_id
            and str(getattr(run.status, "value", run.status)) in active
            for run in list(workspace_manager.scheduled_task_runs.values())
        )

    def _reports(self, workspace_id: str, task_id: str) -> list[Any]:
        from ... import workspace_manager

        try:
            return list(workspace_manager.reports_for_task(workspace_id, task_id))
        except KeyError:
            return []

    def _latest_report(self, workspace_id: str, task_id: str) -> Any:
        reports = self._reports(workspace_id, task_id)
        return reports[-1] if reports else None

    @staticmethod
    def _last_activity(item: dict[str, Any], task: Any, report: Any) -> Optional[datetime]:
        """Newest sign of life: task update, progress, runtime change, report, or watch start."""
        moments = (
            getattr(task, "updated_at", None),
            getattr(getattr(task, "latest_progress", None), "reported_at", None),
            getattr(getattr(task, "runtime_observation", None), "observed_at", None),
            getattr(report, "created_at", None),
            item.get("watched_at") or item.get("created"),
        )
        parsed = [moment for moment in map(_parse_time, moments) if moment is not None]
        return max(parsed, default=None)

    def _result_card(
        self, item: dict[str, Any], status: str, report: Any, failure: str = ""
    ) -> Card:
        message = _clip_text(str(getattr(report, "message", "") or "(no report)"))
        lines = [f"Task `{item['task_id']}` in workspace `{item.get('workspace_id')}`: {status}."]
        if item.get("goal"):
            lines += ["", f"Goal: {item['goal']}"]
        if item.get("output"):
            lines.append(f"Output: {item['output']}")
        checks = _checklist(item.get("done_when"))
        if checks:
            # Next to the report, so each check can be held against the worker's answer.
            lines += [
                "",
                "## Done when",
                *[f"{number}. {check}" for number, check in enumerate(checks, 1)],
            ]
        # The worker's own failure report says why; else the Hub's reason does.
        headline = failure if failure and _report_state(report) != "failed" else message
        if failure:
            lines += ["", "## Failure", _clip_text(failure)]
        lines += ["", "## Report", message]
        for label, attr in (("Validation", "validation"), ("Risks", "risks")):
            value = getattr(report, attr, None)
            if value:
                lines += ["", f"## {label}", _clip_text(str(value))]
        changed = list(getattr(report, "changed_files", []) or [])
        artifacts = list(getattr(report, "artifact_refs", []) or [])
        if changed:
            lines += ["", "## Changed files", *[f"- {path}" for path in changed[:50]]]
        if artifacts:
            lines += ["", "## Artifacts", *[f"- {ref}" for ref in artifacts[:50]]]
        card = Card(
            id=str(item.get("result_card") or ""),
            kind="work",
            title=f"Result: {item.get('title') or item['task_id']}",
            summary=f"[{status}] {_first_line(headline)}",
            body="\n".join(lines),
            tags=["result", status],
            importance=3,
            source=f"task:{item['task_id']}",
        )
        return self.store.save_card(card)

    async def check_work(self) -> int:
        """Turn tracked task transitions into inbox events and result cards."""
        from ... import workspace_manager

        events = 0
        for item in self.store.list_work(include_closed=True):
            try:
                events += self._check_item(workspace_manager, item)
            except Exception:
                # One bad task or report must not stop the others from being seen.
                logger.exception("dot: checking task %s failed", item.get("task_id"))
        if events:
            await asyncio.to_thread(self.store.commit, f"dot: {events} task event(s)")
        return events

    def _check_item(self, workspace_manager: Any, item: dict[str, Any]) -> int:
        """Look at one tracked task; returns how many inbox events it raised."""
        task_id = str(item.get("task_id") or "")
        task = workspace_manager.tasks.get(task_id)
        # A re-run that ends the same way between two polls only shows as a new end time.
        ended = _ended_at(task)
        rerun = (
            task is not None
            and ended is not None
            and task.status.value == item.get("status")
            and "ended_at" in item
            and ended != item["ended_at"]
        )
        if item.get("closed"):
            if ended and "ended_at" not in item and not item.get("stopped"):
                # Closed before end times were kept: remember it, no event.
                self.store.upsert_work(task_id, ended_at=ended)
            # A closed task is looked at again only when it was restarted or re-run.
            if (
                item.get("stopped")
                or task is None
                or (task.status.value == item.get("status") and not rerun)
            ):
                return 0
        if task is None:
            self.store.add_inbox(
                "task_missing",
                f"{item.get('title') or task_id} — task no longer exists",
                task_id=task_id,
                key=f"task_missing:{task_id}",
            )
            self.store.upsert_work(task_id, status="missing", closed=True)
            return 1
        status = task.status.value
        previous = item.get("status")
        reports = self._reports(str(item.get("workspace_id") or ""), task_id)
        report = reports[-1] if reports else None
        report_id = getattr(report, "id", None)
        seen_ids = [getattr(old, "id", None) for old in reports]
        last_seen = item.get("last_report_id")
        new_reports = reports[seen_ids.index(last_seen) + 1 :] if last_seen in seen_ids else reports
        # The Hub's own review requests carry no result, question or round.
        fresh = [new for new in new_reports if not _hub_report(new)]
        latest = fresh[-1] if fresh else None
        # The Hub reviewer's reports (review_*) are not a round's result either.
        returned = [new for new in fresh if _worker_report(new)]
        changes: dict[str, Any] = {}
        notices: list[tuple[str, str, Optional[str]]] = []
        if status != previous:
            changes["status"] = status
        # Any question since the last check, even one answered before this poll, counts
        # against first pass (only until the first verdict).
        if not item.get("first_verdict") and any(
            _report_state(new) in _QUESTION_REPORT_STATES for new in fresh
        ):
            changes["asked"] = True
        # Dot judges what it delegated; a watched task's brief was not Dot's.
        review_due = None if item.get("watched") else True
        # A new final report in review/failed is a new round, even when the poll never
        # saw the task working again; before a verdict it refreshes the result card.
        new_returns = [new for new in returned if _worker_return(new)]
        new_result = bool(new_returns)
        again = status == previous and new_result
        title = item.get("title") or task_id
        # A new round shows its return even when progress came after it; otherwise
        # the worker's newest word (a send-back that came back without a new result).
        result = new_returns[-1] if new_returns else None
        for old in [] if new_returns else reversed(reports):
            # The person's continue message is not the worker's word, and before it
            # is the round that was sent back.
            if _continue_report(old, task):
                break
            if _worker_report(old):
                result = old
                break
        result_id = getattr(result, "id", None)
        # The Hub reviewer moves a task to working and back to review (or a person then
        # accepts it): that is the result already announced, not a new one.
        announced = item.get("announced_report_id")
        at = seen_ids.index(announced) if announced in seen_ids else None
        since = reports[at + 1 :] if at is not None else None
        announced_return = at is not None and _worker_return(reports[at])
        same_result = (
            bool(announced)
            and since is not None
            and not any(_worker_return(old) for old in since)
            # Back in review without a reviewer step, someone sent the task back: ask
            # again. Accepting (again) a result that was not a failure is not news (a
            # task watched while working only had a result if it had returned);
            # failing again with no new report (a re-run that crashed) is.
            and (
                any(_report_state(new).startswith("review_") for new in fresh)
                if status == "review"
                else (
                    item.get("announced_status") in ("review", "done")
                    or (item.get("announced_status") != "failed" and announced_return)
                    if status == "done"
                    else ended is not None and ended == item.get("ended_at")
                )
            )
        )
        # Why the Hub failed it, when the worker did not say (session died, timeout).
        failure = str(getattr(task, "failure_reason", None) or "") if status == "failed" else ""
        if (
            status in ("review", "failed")
            and (status != previous or again or rerun)
            and not same_result
        ):
            card = self._result_card(item, status, result, failure)
            changes.update(result_card=card.id, review_due=review_due)
            changes.update(announced_report_id=result_id, announced_status=status)
            said = str(getattr(result, "message", "") or "")
            if failure and _report_state(result) != "failed":
                said = failure
            summary = _first_line(said, 300)
            kind = "task_ready" if status == "review" else "task_failed"
            notices.append((kind, f"{title} — {summary or status}", card.id))
        elif (
            status == "done"
            and (previous not in ("review", "done") or rerun or new_result)
            and not same_result
        ):
            card = self._result_card(item, status, result)
            changes.update(result_card=card.id, review_due=review_due)
            changes.update(announced_report_id=result_id, announced_status=status)
            notices.append(("task_done", f"{title} — done", card.id))
        asking = latest if _report_state(latest) in _ATTENTION_REPORT_STATES else None
        if asking is None and status == "review":
            # Worker progress after it does not answer the Hub reviewer's question.
            step = next(
                (
                    new
                    for new in reversed(fresh)
                    if _report_state(new).startswith("review_") or _worker_return(new)
                ),
                None,
            )
            asking = step if _report_state(step) == "review_needs_input" else None
        if (
            asking is not None
            and status not in _TERMINAL_TASK_STATES
            # The Hub reviewer's question can come with the result it is about.
            and (not notices or _report_state(asking) == "review_needs_input")
        ):
            question = _first_line(str(getattr(asking, "message", "") or ""), 300)
            notices.append(("task_needs_input", f"{title} — {question}", None))
        elif (
            not notices
            and status in _STALL_TASK_STATES
            and (activity := self._last_activity(item, task, report))
        ):
            idle = self._utc_now() - activity
            limit = float(item.get("stall_hours") or DEFAULT_STALL_HOURS)
            # One alarm per stall: a new sign of life re-arms it.
            if idle > timedelta(hours=limit) and item.get("stall_alarm_for") != (
                marker := activity.isoformat()
            ):
                notices.append(
                    (
                        "task_stalled",
                        f"{title} — no progress for "
                        f"{idle.total_seconds() / 3600:.1f}h (status {status})",
                        None,
                    )
                )
                changes["stall_alarm_for"] = marker
        if report_id and report_id != last_seen:
            changes["last_report_id"] = report_id
        if status in _TERMINAL_TASK_STATES:
            changes["closed"] = True
            if ended and ended != item.get("ended_at"):
                changes["ended_at"] = ended
        elif item.get("closed"):
            changes["closed"] = False
        # Events go first, so a failed write cannot lose one. The next tick sees the same
        # change again; its key makes that the same unread event, not a second one.
        key = ":".join(
            str(part or "")
            for part in (task_id, status, report_id, ended, changes.get("stall_alarm_for"))
        )
        for kind, text, card_id in notices:
            extra = {"card_id": card_id} if card_id else {}
            self.store.add_inbox(kind, text, task_id=task_id, key=f"{kind}:{key}", **extra)
        if changes:
            self.store.upsert_work(task_id, **changes)
        return len(notices)
