"""Assignment envelope delivery and public dispatch adapters."""

from claude_hub.services.task_dependencies import require_task_dependencies
from claude_hub.services.task_transitions import apply_task_update
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _DispatchDeliveryMixin(_WorkspaceManagerBase):
    async def _send_dispatch_message(
        self,
        session_id: str,
        dispatch_call_id: str,
        prompt: str,
    ) -> None:
        """Send a task dispatch prompt.

        The dispatch call_id is ``f"dispatch:{task.id}:{dispatch_attempt}"``.

        Recovery contract: the assignment prompt passed here may be rebuilt
        from current state (lesson context can drift across cycles). We MUST
        NOT overwrite or re-fingerprint a persisted envelope — the original
        stored payload is the source of truth until the worker ACKs it.

        Therefore we first call :meth:`resume_existing_call`, which operates
        on the persisted envelope only:

        * ``pending``    — pump the stored payload to tmux.
        * ``processing`` — no-op (already in flight).
        * ``delivered``  — no-op (already ACKed).
        * ``uncertain``  — raise :class:`DeliveryUncertain` (fail-closed;
          system never auto-retries an ambiguous delivery).
        * ``absent``     — return ``False``; we then persist the newly built
          prompt via :meth:`send_session_message`.

        Only when the call_id is absent do we commit the rebuilt prompt as a
        fresh delivery.
        """
        resumed = await self.resume_existing_call(session_id, dispatch_call_id)
        if resumed:
            return
        # call_id absent: persist the newly built prompt as a fresh delivery.
        await self.send_session_message(session_id, prompt, call_id=dispatch_call_id)

    async def _dispatch_task_to_session_workspace(
        self,
        task: WorkspaceTask,
        session: ManagedSession,
    ) -> None:
        workspace = self.workspaces.get(task.workspace_id)
        if not workspace:
            raise KeyError(task.workspace_id)

        task = self.tasks[task.id]
        require_task_dependencies(self.tasks, task)
        if task.session_kind != session.session_kind:
            raise ValueError("The assigned agent does not match the Task execution surface")
        session = await self._rename_session_for_task(session, task)
        task = self.tasks[task.id]
        require_task_dependencies(self.tasks, task)

        await self._resume_chat_session(session.id)
        session = self.sessions[session.id]
        dispatch_call_id = f"dispatch:{task.id}:{task.dispatch_attempt}"
        if task.clear_context and (
            session.session_kind != SessionKind.CHAT
            or dispatch_call_id not in session.call_payload_fingerprints
        ):
            await self._reset_session_context(session)
            session = self.sessions[session.id]

        logger.info(
            "Dispatching workspace task id=%s title=%r to session_id=%s session_title=%r "
            "related_task_id=%s dispatch_reason=%r",
            task.id,
            task.title,
            session.id,
            session.title,
            task.related_task_id,
            task.dispatch_reason,
        )
        now = self._runtime_now()

        # ------------------------------------------------------------------
        # Crash-idempotent dispatch: claim the task for the session BEFORE
        # sending the prompt.
        #
        # The dispatch call_id is f"dispatch:{task.id}:{task.dispatch_attempt}".
        # We persist session.task_id = task.id (and save) before the send so
        # that a crash between the send side-effect and the WORKING persist
        # does NOT let the monitor re-dispatch the task to a different
        # session. On recovery, _recover_queued_task_ownership detects that
        # the session holds a QUEUED task and re-sends the assignment prompt.
        # The call_id ensures sender-side dedup: if the call_id is already
        # in processing_call_ids (sent to tmux) or delivered_call_ids
        # (ACKed by the worker via a report), send_session_message skips
        # the re-send. If it is still in pending_call_ids, the pump sends
        # it to the tmux inbox exactly once.
        # ------------------------------------------------------------------
        task = self.tasks[task.id]
        require_task_dependencies(self.tasks, task)
        dispatch_call_id = f"dispatch:{task.id}:{task.dispatch_attempt}"
        self.sessions[session.id] = session.model_copy(
            update={
                "task_id": task.id,
                "current_task_id": task.id,
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "auto_continue_task_id": task.id,
                "auto_continue_attempts": 0,
                "last_auto_continue_at": None,
                "hard_recovery_task_id": None,
                "hard_recovery_attempts": 0,
                "last_hard_recovery_at": None,
                "prompt_retry_task_id": None,
                "prompt_retry_attempted_at": None,
                "last_activity_at": now,
                "updated_at": now,
            }
        )
        # Establish the canonical task -> session binding in the SAME pre-send
        # claim transaction as session.task_id. The normal start_task path
        # already persists task.session_id before the dispatch pass, but direct
        # callers (the scheduled hub_task fire path) reach this method without
        # it. Without it a worker's first report is rejected by report intake
        # ("Task has no assigned worker session") and abort/release/auto-DONE
        # cleanup — all keyed on task.session_id — cannot find the session.
        # Persisting before the send makes the binding crash-idempotent. For
        # queued-continuation callers task.session_id already equals session.id,
        # so this write is a no-op there (idempotent, never a re-bind).
        if task.session_id != session.id:
            logger.info(
                "Binding workspace task id=%s to session_id=%s before dispatch",
                task.id,
                session.id,
            )
        self.tasks[task.id] = task.model_copy(update={"session_id": session.id, "updated_at": now})
        self._save_state()
        session = self.sessions[session.id]
        # Re-read so the post-send model_copy below starts from the persisted
        # binding rather than the stale parameter object (whose session_id may
        # still be None), which would otherwise clobber the binding.
        task = self.tasks[task.id]

        lesson_context = self._lesson_context_payload(
            workspace,
            f"{task.title}\n{task.prompt}",
        )
        dispatch_call_id = f"dispatch:{task.id}:{task.dispatch_attempt}"
        await self._send_dispatch_message(
            session.id,
            dispatch_call_id,
            self._build_task_assignment_prompt(
                workspace,
                task,
                session,
                lesson_context=lesson_context,
            ),
        )

        current = self.tasks.get(task.id)
        if current is None or current.status != task.status or current.session_id != session.id:
            return
        task = current
        self.tasks[task.id] = apply_task_update(
            task,
            {
                "status": WorkspaceTaskStatus.WORKING,
                "session_id": session.id,
                "started_at": now,
                "updated_at": now,
            },
            cause="dispatch",
        )
        self._save_state()
        logger.info(
            "Workspace task id=%s dispatched to session_id=%s and marked working",
            task.id,
            session.id,
        )

    async def start_task(
        self, task_id: str, payload: StartTaskRequest | None = None
    ) -> WorkspaceTask:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        async with self._workspace_execution_operation(task):
            return await self._start_task_workspace(task_id, payload)

    async def apply_dispatch_decision(
        self, task_id: str, payload: DispatchDecisionRequest
    ) -> WorkspaceTask:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        async with self._workspace_execution_operation(task):
            return await self._apply_dispatch_decision_workspace(task_id, payload)

    async def continue_task(
        self,
        task_id: str,
        payload: ContinueTaskRequest | None = None,
        call_id: str | None = None,
    ) -> WorkspaceTask:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        async with self._workspace_execution_operation(task):
            return await self._continue_task_workspace(task_id, payload, call_id)

    async def request_task_review(
        self, task_id: str, payload: RequestTaskReviewRequest | None = None
    ) -> WorkspaceTask:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        async with self._workspace_execution_operation(task):
            return await self._request_task_review_workspace(task_id, payload)

    async def _dispatch_task_to_session(self, task: WorkspaceTask, session: ManagedSession) -> None:
        async with self._workspace_execution_operation(task) as live:
            await self._dispatch_task_to_session_workspace(live, session)
