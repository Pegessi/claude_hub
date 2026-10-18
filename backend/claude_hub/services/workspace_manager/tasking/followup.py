"""Task domain implementation split from the compatibility mixin."""

from ....models.task_mailbox import TaskActorRole, TaskEvent, TaskEventType
from ...request_fingerprint import request_fingerprint
from ...task_graph import task_inbox_consumer_key
from .._constants import *  # noqa: F401,F403
from .feedback import _TaskFeedbackSummaryMixin

_FOLLOWUP_ACTOR_ROLES = frozenset({TaskActorRole.SUPERVISOR, TaskActorRole.HUMAN})


class _TaskFollowupMixin(_TaskFeedbackSummaryMixin):
    async def _followup_existing_task(
        self,
        task_id: str,
        message: str,
        call_id: Optional[str] = None,
    ) -> None:
        """Deliver an already-committed followup. No Task/mailbox writes.

        ``followup_task`` owns the single outer persist (mailbox + pending +
        TODO/QUEUED prompt). This helper only routes that committed intent.
        """
        task = self.tasks.get(task_id)
        if task is None:
            return
        if call_id and (
            call_id in task.processing_call_ids
            or call_id in task.delivered_call_ids
            or call_id in task.uncertain_call_ids
        ):
            logger.debug(
                "followup call_id=%s already claimed on task %s; skipping delivery",
                call_id,
                task_id,
            )
            return

        if task.status == WorkspaceTaskStatus.TODO:
            await self.start_task(task_id)
        elif task.status == WorkspaceTaskStatus.QUEUED:
            return
        elif task.status == WorkspaceTaskStatus.WORKING:
            if task.session_id:
                await self._resume_chat_session(task.session_id)
                await self.send_session_message(task.session_id, message, call_id=call_id)
        elif task.status == WorkspaceTaskStatus.REVIEW:
            await self.continue_task(
                task_id,
                ContinueTaskRequest(message=message),
                call_id=call_id,
            )

    def _followup_prompt_block(self, message: str, call_id: Optional[str]) -> str:
        followup_text = message
        if call_id:
            followup_text = f"[call_id:{call_id}]\n{message}"
        return f"[followup] {followup_text}"

    def _stage_task_followup_pending(self, task_id: str, call_id: Optional[str]) -> bool:
        """Stage ``call_id`` on the Task outbox. No persist.

        Returns True only when pending was newly appended. Existing
        pending / processing / delivered / uncertain claims are left
        unchanged so a retry does not invent a second outbox row.
        """
        if not call_id:
            return False
        current = self.tasks.get(task_id)
        if current is None:
            return False
        if (
            call_id in current.pending_call_ids
            or call_id in current.processing_call_ids
            or call_id in current.delivered_call_ids
            or call_id in current.uncertain_call_ids
        ):
            return False
        self.tasks[task_id] = current.model_copy(
            update={"pending_call_ids": current.pending_call_ids + [call_id]}
        )
        return True

    def _stage_task_followup_prompt(
        self,
        task_id: str,
        message: str,
        call_id: Optional[str],
    ) -> bool:
        """Stage the TODO/QUEUED followup marker. No persist."""
        current = self.tasks.get(task_id)
        if current is None:
            return False
        if current.status not in (
            WorkspaceTaskStatus.TODO,
            WorkspaceTaskStatus.QUEUED,
        ):
            return False
        block = self._followup_prompt_block(message, call_id)
        if block in current.prompt:
            return False
        self.tasks[task_id] = current.model_copy(update={"prompt": f"{current.prompt}\n\n{block}"})
        return True

    def _resolve_actor_session_from_author_ref(
        self,
        workspace_id: str,
        author_ref: Optional[str],
    ) -> Optional[str]:
        """Resolve a Session id from a supervisor task id or legacy author ref."""

        if not author_ref:
            return None
        direct = self.tasks.get(author_ref)
        if direct is not None and direct.workspace_id == workspace_id and direct.session_id:
            return direct.session_id
        return None

    def _canonical_followup_payload(
        self,
        *,
        task_id: str,
        message: str,
        actor_role: TaskActorRole,
        actor_session_id: Optional[str],
        review_cycle: Optional[int],
        compat_author_run_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return {
            "message": message,
            "followup": True,
            "task_id": task_id,
            "actor_role": actor_role.value,
            "actor_session_id": actor_session_id,
            "review_cycle": review_cycle,
            "compat_author_run_id": compat_author_run_id,
            "correlation_id": correlation_id,
        }

    async def followup_task(
        self,
        workspace_id: str,
        task_id: str,
        message: str,
        call_id: str,
        *,
        actor_session_id: Optional[str] = None,
        actor_role: TaskActorRole = TaskActorRole.SUPERVISOR,
        compat_author_run_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> TaskEvent:
        """Write the TaskMailbox followup intent, then deliver after commit."""
        if actor_role not in _FOLLOWUP_ACTOR_ROLES:
            raise ValueError(
                f"followup_task actor_role must be supervisor or human, got {actor_role}"
            )
        async with self.workspace_mutation_lock(workspace_id):
            if workspace_id not in self.workspaces:
                raise KeyError(workspace_id)
            task = self.tasks.get(task_id)
            if task is None or task.workspace_id != workspace_id:
                raise KeyError(task_id)
            task = self._require_workspace_execution(task)
            if task.status == WorkspaceTaskStatus.DONE:
                raise RuntimeError("Done tasks cannot receive followup")

            resolved_actor_session_id = actor_session_id
            if resolved_actor_session_id is None and compat_author_run_id:
                resolved_actor_session_id = self._resolve_actor_session_from_author_ref(
                    workspace_id, compat_author_run_id
                )
            existing = self.task_mailbox._call_record(workspace_id, call_id)
            existing_event = existing["event"] if existing is not None else None
            review_cycle = (
                existing_event.review_cycle if existing_event is not None else task.review_cycle
            )

            snapshot = self._snapshot_report_intake_workspace(workspace_id)
            event, _created = self.task_mailbox.append_event(
                workspace_id=workspace_id,
                task_id=task.id,
                actor_role=actor_role,
                event_type=TaskEventType.FOLLOWUP,
                call_id=call_id,
                action="followup",
                consumer_key=task_inbox_consumer_key(task),
                actor_session_id=resolved_actor_session_id,
                review_cycle=review_cycle,
                target=task.id,
                payload=self._canonical_followup_payload(
                    task_id=task.id,
                    message=message,
                    actor_role=actor_role,
                    actor_session_id=resolved_actor_session_id,
                    review_cycle=review_cycle,
                    compat_author_run_id=compat_author_run_id,
                    correlation_id=correlation_id,
                ),
                persist=False,
            )
            self._stage_task_followup_pending(task.id, call_id)
            self._stage_task_followup_prompt(task.id, message, call_id)
            try:
                self._save_workspace_state(workspace_id)
            except Exception:
                self._restore_report_intake_workspace(workspace_id, snapshot)
                raise
            self.task_mailbox._wake_compat_waiters(event)
            await self._followup_existing_task(task.id, message, call_id)
            return event
