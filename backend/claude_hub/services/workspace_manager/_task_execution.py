"""Task execution ownership and passive progress intake."""

import hashlib
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

import claude_hub.services.workspace_manager as _wm

from ...models.agent_stream import AgentStreamEvent, AgentStreamEventType
from ...models.schemas import (
    TaskExecutionControl,
    TaskExecutionHandoffRequest,
    TaskExecutionMutationResult,
    TaskExecutionReference,
    TaskManualProgressRequest,
    TaskProgressRequest,
    TaskProgressSnapshot,
    TaskProgressState,
    TaskRuntimeObservation,
    WorkspaceTask,
    WorkspaceTaskCreate,
    WorkspaceTaskStatus,
)
from ...models.task_mailbox import TaskActorRole, TaskEventType
from ..request_fingerprint import request_fingerprint


class TaskExecutionConflict(RuntimeError):
    pass


class TaskReporterForbidden(PermissionError):
    pass


def reporter_key_digest(value: str | None) -> str:
    if (
        not isinstance(value, str)
        or not 32 <= len(value) <= 256
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        raise ValueError("invalid_task_reporter_key")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _TaskExecutionMixin:
    def _workspace_owns_task(self, task: WorkspaceTask) -> bool:
        return task.execution_control == TaskExecutionControl.WORKSPACE

    def _require_workspace_execution(self, task: WorkspaceTask) -> WorkspaceTask:
        live = self.tasks.get(task.id)
        if live is None:
            raise KeyError(task.id)
        if not self._workspace_owns_task(live):
            raise TaskExecutionConflict("task_is_initiator_controlled")
        return live

    @asynccontextmanager
    async def _workspace_execution_operation(
        self, task: WorkspaceTask
    ) -> AsyncIterator[WorkspaceTask]:
        async with self.workspace_mutation_lock(task.workspace_id):
            live = self._require_workspace_execution(task)
            self._task_execution_operations[task.id] = (
                self._task_execution_operations.get(task.id, 0) + 1
            )
        try:
            yield live
        finally:
            # No await here: cancellation cannot strand a reservation.
            remaining = self._task_execution_operations.get(task.id, 1) - 1
            if remaining:
                self._task_execution_operations[task.id] = remaining
            else:
                self._task_execution_operations.pop(task.id, None)

    def _validate_task_execution_create(
        self,
        payload: WorkspaceTaskCreate,
        *,
        system_internal: bool,
        internal_kind: str | None,
    ) -> str | None:
        if payload.execution_control == TaskExecutionControl.WORKSPACE:
            if payload.reporter_key is not None:
                raise ValueError("workspace_task_cannot_have_reporter_key")
            return None
        if payload.session_id or system_internal or internal_kind:
            raise ValueError("initiator_task_cannot_have_managed_assignment")
        if not payload.request_key:
            raise ValueError("initiator_task_requires_request_key")
        return reporter_key_digest(payload.reporter_key)

    async def register_task(
        self, workspace_id: str, payload: WorkspaceTaskCreate, actor_key: str
    ) -> tuple[WorkspaceTask, bool]:
        if not actor_key:
            raise ValueError("authenticated_task_creator_required")
        key_hash = self._validate_task_execution_create(
            payload, system_internal=False, internal_kind=None
        )
        canonical = payload.model_dump(mode="json")
        canonical.update(title=payload.title.strip(), prompt=payload.prompt.strip())
        fingerprint = request_fingerprint(
            "task_create", {**canonical, "reporter_key_hash": key_hash}
        )
        async with self.workspace_mutation_lock(workspace_id):
            if workspace_id not in self.workspaces:
                raise KeyError(workspace_id)
            if payload.request_key:
                for task in self.tasks.values():
                    if (
                        task.workspace_id == workspace_id
                        and task.creation_actor_key == actor_key
                        and task.creation_request_key == payload.request_key
                    ):
                        if task.creation_fingerprint != fingerprint:
                            raise TaskExecutionConflict("task_create_request_key_conflict")
                        return task, True
            snapshot = self._snapshot_report_intake_workspace(workspace_id)
            token = self._report_intake_workspace.set(workspace_id)
            try:
                task = self._create_task(
                    workspace_id,
                    payload,
                    creation_request_key=payload.request_key,
                    creation_actor_key=actor_key if payload.request_key else None,
                    creation_fingerprint=fingerprint if payload.request_key else None,
                )
            except Exception:
                self._restore_report_intake_workspace(workspace_id, snapshot)
                raise
            finally:
                self._report_intake_workspace.reset(token)
            return task, False

    def _execution_task(self, workspace_id: str, task_id: str) -> WorkspaceTask:
        task = self.require_workspace_task(workspace_id, task_id)
        if task.execution_control != TaskExecutionControl.INITIATOR:
            raise TaskExecutionConflict("task_is_workspace_controlled")
        return task

    def _check_reporter(self, task: WorkspaceTask, key: str | None) -> None:
        try:
            supplied = reporter_key_digest(key)
        except ValueError:
            raise TaskReporterForbidden("task_reporter_key_required") from None
        if not task.reporter_key_hash or not hmac.compare_digest(task.reporter_key_hash, supplied):
            raise TaskReporterForbidden("task_reporter_key_rejected")

    def _check_execution_version(
        self, task: WorkspaceTask, epoch: int, revision: int | None = None
    ) -> None:
        if task.execution_epoch != epoch:
            raise TaskExecutionConflict("task_execution_epoch_conflict")
        if revision is not None and task.progress_revision != revision:
            raise TaskExecutionConflict("task_progress_revision_conflict")

    def _execution_call_id(self, task_id: str, call_id: str) -> str:
        return f"task-execution:{task_id}:{call_id}"

    def _execution_replay(
        self, task: WorkspaceTask, call_id: str, fingerprint: str
    ) -> TaskExecutionMutationResult | None:
        existing = self.task_mailbox._call_record(task.workspace_id, call_id)
        if existing is None:
            return None
        if task.execution_call_fingerprints.get(call_id) != fingerprint:
            raise TaskExecutionConflict("task_execution_call_id_conflict")
        return TaskExecutionMutationResult(task=task, event=existing["event"], replayed=True)

    def _commit_execution_change(
        self,
        task: WorkspaceTask,
        *,
        changes: dict[str, Any],
        call_id: str,
        fingerprint: str,
        action: str,
        actor_role: TaskActorRole,
        event_type: TaskEventType,
        event_payload: dict[str, Any],
    ) -> TaskExecutionMutationResult:
        snapshot = self._snapshot_report_intake_workspace(task.workspace_id)
        fingerprints = dict(task.execution_call_fingerprints)
        fingerprints[call_id] = fingerprint
        while len(fingerprints) > 128:
            fingerprints.pop(next(iter(fingerprints)))
        changes = {**changes, "execution_call_fingerprints": fingerprints}
        self.tasks[task.id] = task.model_copy(update=changes)
        token = self._report_intake_workspace.set(task.workspace_id)
        try:
            event, _created = self.task_mailbox.append_event(
                workspace_id=task.workspace_id,
                task_id=task.id,
                actor_role=actor_role,
                event_type=event_type,
                call_id=call_id,
                action=action,
                consumer_key=f"task:{task.id}",
                target=task.id,
                payload=event_payload,
                persist=False,
                wake=False,
            )
            self._save_state()
        except Exception:
            self._restore_report_intake_workspace(task.workspace_id, snapshot)
            raise
        finally:
            self._report_intake_workspace.reset(token)
        # No managed report bridge, prompt, reviewer, or session cleanup here.
        return TaskExecutionMutationResult(task=self.tasks[task.id], event=event, replayed=False)

    async def record_task_progress(
        self,
        workspace_id: str,
        task_id: str,
        payload: TaskProgressRequest,
        reporter_key: str | None,
    ) -> TaskExecutionMutationResult:
        return await self._record_task_progress(
            workspace_id, task_id, payload, reporter_key=reporter_key, manual=False
        )

    async def record_manual_task_progress(
        self,
        workspace_id: str,
        task_id: str,
        payload: TaskManualProgressRequest,
    ) -> TaskExecutionMutationResult:
        return await self._record_task_progress(
            workspace_id, task_id, payload, reporter_key=None, manual=True
        )

    async def _record_task_progress(
        self,
        workspace_id: str,
        task_id: str,
        payload: TaskProgressRequest,
        *,
        reporter_key: str | None,
        manual: bool,
    ) -> TaskExecutionMutationResult:
        async with self.workspace_mutation_lock(workspace_id):
            task = self._execution_task(workspace_id, task_id)
            if manual:
                if (
                    payload.state == TaskProgressState.RELEASED
                    or "execution_ref" in payload.model_fields_set
                ):
                    raise ValueError("manual_progress_cannot_release_or_bind_executor")
            else:
                self._check_reporter(task, reporter_key)
            self._check_execution_version(task, payload.expected_execution_epoch)
            action = "task_manual_progress" if manual else "task_progress"
            body = payload.model_dump(mode="json")
            fingerprint = request_fingerprint(action, body)
            call_id = self._execution_call_id(task.id, payload.call_id)
            replay = self._execution_replay(task, call_id, fingerprint)
            if replay is not None:
                return replay
            self._check_execution_version(
                task, payload.expected_execution_epoch, payload.expected_progress_revision
            )
            if task.execution_released:
                raise TaskExecutionConflict("task_execution_already_released")
            terminal = task.status in {WorkspaceTaskStatus.DONE, WorkspaceTaskStatus.FAILED}
            if terminal and payload.state != TaskProgressState.RELEASED:
                raise TaskExecutionConflict("task_record_is_terminal")
            now = _wm._now()
            progress = TaskProgressSnapshot(
                state=payload.state,
                summary=payload.summary,
                validation=payload.validation,
                risks=payload.risks,
                artifact_refs=payload.artifact_refs,
                call_id=payload.call_id,
                execution_epoch=task.execution_epoch,
                reported_at=now,
            )
            changes: dict[str, Any] = {
                "latest_progress": progress,
                "progress_revision": task.progress_revision + 1,
                "updated_at": now,
            }
            if not manual and payload.execution_ref is not None:
                changes["execution_ref"] = payload.execution_ref
                if payload.execution_ref != task.execution_ref:
                    changes["runtime_observation"] = None
            event_type = TaskEventType.PROGRESS
            if payload.state == TaskProgressState.RELEASED:
                changes["execution_released"] = True
            elif payload.state == TaskProgressState.COMPLETED:
                changes.update(status=WorkspaceTaskStatus.DONE, completed_at=now)
                event_type = TaskEventType.COMPLETED
            elif payload.state == TaskProgressState.FAILED:
                changes.update(
                    status=WorkspaceTaskStatus.FAILED,
                    failed_at=now,
                    failure_reason=payload.summary,
                )
                event_type = TaskEventType.FAILED
            else:
                changes.update(
                    status=WorkspaceTaskStatus.WORKING,
                    started_at=task.started_at or now,
                )
            return self._commit_execution_change(
                task,
                changes=changes,
                call_id=call_id,
                fingerprint=fingerprint,
                action=action,
                actor_role=TaskActorRole.HUMAN if manual else TaskActorRole.INITIATOR,
                event_type=event_type,
                event_payload=body,
            )

    def _require_quiescent_workspace_task(self, task: WorkspaceTask) -> None:
        if (
            self._task_execution_operations.get(task.id, 0)
            or task.status
            in {
                WorkspaceTaskStatus.QUEUED,
                WorkspaceTaskStatus.WORKING,
                WorkspaceTaskStatus.REVIEW,
            }
            or task.dispatch_pending
            or task.pending_call_ids
            or task.processing_call_ids
            or task.uncertain_call_ids
            or (task.review_requested_at and not task.review_completed_at)
        ):
            raise TaskExecutionConflict("workspace_task_execution_not_released")
        for session in self.sessions.values():
            if task.id in {session.task_id, session.current_task_id}:
                raise TaskExecutionConflict("workspace_task_execution_not_released")

    async def handoff_task_execution(
        self,
        workspace_id: str,
        task_id: str,
        payload: TaskExecutionHandoffRequest,
    ) -> TaskExecutionMutationResult:
        new_hash = None
        if payload.execution_control == TaskExecutionControl.INITIATOR:
            new_hash = reporter_key_digest(payload.new_reporter_key)
        elif payload.new_reporter_key is not None or payload.execution_ref is not None:
            raise ValueError("workspace_handoff_cannot_bind_external_executor")
        body = payload.model_dump(mode="json")
        fingerprint = request_fingerprint(
            "task_execution_handoff", {**body, "new_reporter_key_hash": new_hash}
        )
        async with self.workspace_mutation_lock(workspace_id):
            task = self.require_workspace_task(workspace_id, task_id)
            call_id = self._execution_call_id(task.id, payload.call_id)
            replay = self._execution_replay(task, call_id, fingerprint)
            if replay is not None:
                return replay
            self._check_execution_version(
                task, payload.expected_execution_epoch, payload.expected_progress_revision
            )
            if task.system_internal or task.internal_kind:
                raise TaskExecutionConflict("internal_task_cannot_be_handed_off")
            if task.execution_control == TaskExecutionControl.INITIATOR:
                if not task.execution_released:
                    raise TaskExecutionConflict("initiator_execution_not_released")
            else:
                self._require_quiescent_workspace_task(task)
            if (
                task.execution_control == TaskExecutionControl.WORKSPACE
                and payload.execution_control == TaskExecutionControl.WORKSPACE
            ):
                raise TaskExecutionConflict("task_already_workspace_controlled")
            now = _wm._now()
            changes: dict[str, Any] = {
                "execution_control": payload.execution_control,
                "execution_epoch": task.execution_epoch + 1,
                "progress_revision": task.progress_revision + 1,
                "execution_released": False,
                "reporter_key_hash": new_hash,
                "execution_ref": payload.execution_ref,
                "latest_progress": None,
                "runtime_observation": None,
                "status": WorkspaceTaskStatus.TODO,
                "session_id": None,
                "review_session_id": None,
                "related_task_id": None,
                "clear_context": None,
                "dispatch_pending": False,
                "dispatch_reason": None,
                "autonomous_run": None,
                "review_cycle": task.review_cycle + 1,
                "reviewed_cycle": 0,
                "updated_at": now,
            }
            for field in (
                "review_requested_at",
                "review_completed_at",
                "review_skipped_at",
                "review_skip_reason",
                "manual_aborted_at",
                "manual_abort_reason",
                "failure_reason",
                "failed_at",
                "human_acceptance_requested_at",
                "human_accepted_at",
                "queued_at",
                "started_at",
                "reviewed_at",
                "completed_at",
            ):
                changes[field] = None
            return self._commit_execution_change(
                task,
                changes=changes,
                call_id=call_id,
                fingerprint=fingerprint,
                action="task_execution_handoff",
                actor_role=TaskActorRole.HUMAN,
                event_type=TaskEventType.MESSAGE,
                event_payload={
                    **body,
                    "previous_execution_control": task.execution_control.value,
                    "new_execution_epoch": task.execution_epoch + 1,
                },
            )

    async def record_task_activity(
        self,
        workspace_id: str,
        task_id: str,
        execution_ref: TaskExecutionReference,
        observation: TaskRuntimeObservation,
    ) -> bool:
        """Store changed activity only; no business status or managed side effects."""
        async with self.workspace_mutation_lock(workspace_id):
            task = self.require_workspace_task(workspace_id, task_id)
            if (
                task.execution_control != TaskExecutionControl.INITIATOR
                or task.execution_released
                or observation.execution_epoch != task.execution_epoch
                or not execution_ref.session_id
                or execution_ref != task.execution_ref
            ):
                return False
            previous_cursor = self._task_activity_cursors.get(task_id)
            previous = task.runtime_observation
            epoch, cursor = self._task_activity_cursors.get(task_id, (task.execution_epoch, -1))
            if previous is None or epoch != task.execution_epoch:
                cursor = -1
            elif previous.stream_sequence > cursor:
                cursor = previous.stream_sequence
            if observation.stream_sequence <= cursor:
                return False
            self._task_activity_cursors[task_id] = (
                task.execution_epoch,
                observation.stream_sequence,
            )
            if previous is not None and (previous.status, previous.turn_id, previous.detail) == (
                observation.status,
                observation.turn_id,
                observation.detail,
            ):
                return False
            snapshot = self._snapshot_report_intake_workspace(workspace_id)
            self.tasks[task.id] = task.model_copy(update={"runtime_observation": observation})
            token = self._report_intake_workspace.set(workspace_id)
            try:
                self._save_state()
            except Exception:
                self._restore_report_intake_workspace(workspace_id, snapshot)
                if previous_cursor is None:
                    self._task_activity_cursors.pop(task_id, None)
                else:
                    self._task_activity_cursors[task_id] = previous_cursor
                raise
            finally:
                self._report_intake_workspace.reset(token)
            return True

    async def record_task_stream_event(self, event: AgentStreamEvent) -> None:
        statuses: dict[AgentStreamEventType, tuple[Literal["active", "idle", "error"], str]] = {
            AgentStreamEventType.TURN_STARTED: ("active", "turn active"),
            AgentStreamEventType.TOOL_CALL_STARTED: ("active", "turn active"),
            AgentStreamEventType.TOOL_CALL_COMPLETED: ("active", "turn active"),
            AgentStreamEventType.APPROVAL_REQUIRED: ("idle", "waiting for input"),
            AgentStreamEventType.APPROVAL_RESOLVED: ("active", "turn active"),
            AgentStreamEventType.ERROR: ("error", "runtime error"),
            AgentStreamEventType.TURN_COMPLETED: ("idle", "turn ended"),
        }
        state = statuses.get(event.type)
        if state is None or event.run_epoch is None or not event.turn_id:
            return
        if (
            event.type == AgentStreamEventType.TURN_COMPLETED
            and event.payload.get("status") == "failed"
        ):
            state = ("error", "turn failed")
        child_id = event.payload.get("subagent_thread")
        child_id = child_id if isinstance(child_id, str) and child_id else None
        main_id = event.payload.get("_hub_main_thread_id")
        for task in list(self.tasks.values()):
            expected = task.execution_ref
            if (
                task.execution_control != TaskExecutionControl.INITIATOR
                or task.execution_released
                or expected is None
                or not expected.session_id
                or expected.run_epoch is None
                or not expected.turn_id
                or expected.session_id != event.session_id
                or expected.run_epoch != event.run_epoch
                or expected.turn_id != event.turn_id
                or (expected.provider is not None and expected.provider != event.agent_type.value)
            ):
                continue
            # An omitted thread selects the main stream, never every child.
            if expected.thread_id is None:
                if child_id is not None:
                    continue
            elif expected.thread_id != (child_id or main_id):
                continue
            observation = TaskRuntimeObservation(
                execution_epoch=task.execution_epoch,
                stream_sequence=event.stream_sequence,
                status=state[0],
                observed_at=event.created_at,
                turn_id=event.turn_id,
                detail=state[1],
            )
            try:
                await self.record_task_activity(task.workspace_id, task.id, expected, observation)
            except KeyError:
                # Deletion is allowed while a stream observation is queued.
                continue
