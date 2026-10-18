"""Task domain implementation split from the compatibility mixin."""

from ....models.task_mailbox import TaskEvent
from ...task_dependencies import validate_task_dependencies
from ...task_graph import (
    make_task_consumer_key,
    resolve_task_tree_fields,
    tasks_in_subtree,
    top_level_tasks,
)
from .._base import _WorkspaceManagerBase
from .._constants import *  # noqa: F401,F403


class _TaskCoreMixin(_WorkspaceManagerBase):
    def create_task(self, workspace_id: str, payload: WorkspaceTaskCreate) -> WorkspaceTask:
        return self._create_task(workspace_id, payload)

    def _create_task(
        self,
        workspace_id: str,
        payload: WorkspaceTaskCreate,
        *,
        system_internal: bool = False,
        internal_kind: str | None = None,
        creation_request_key: str | None = None,
        creation_actor_key: str | None = None,
        creation_fingerprint: str | None = None,
    ) -> WorkspaceTask:
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)
        key_hash = self._validate_task_execution_create(
            payload,
            system_internal=system_internal,
            internal_kind=internal_kind,
        )
        initiator_owned = payload.execution_control.value == "initiator"
        if initiator_owned and not creation_actor_key:
            raise ValueError("initiator_task_requires_authenticated_registration")
        if payload.related_task_id and payload.related_task_id not in self.tasks:
            raise KeyError(payload.related_task_id)
        session_kind = payload.session_kind
        if payload.session_id:
            session = self.sessions.get(payload.session_id)
            if not session or session.workspace_id != workspace_id:
                raise KeyError(payload.session_id)
            if session.role != WorkspaceSessionRole.ORCHESTRATOR:
                raise ValueError("Tasks can only be assigned to workspace agents")
            if "session_kind" not in payload.model_fields_set:
                session_kind = session.session_kind
            elif session.session_kind != session_kind:
                raise ValueError("The selected agent does not match the Task execution surface")
        title = payload.title.strip()
        prompt = payload.prompt.strip()
        if not title:
            raise ValueError("Task title is required")
        if not prompt and not payload.attachments:
            raise ValueError("Task description is required")

        # Allocate the durable task id before parent validation so path,
        # self-parent, and cycle checks use the real id, not a placeholder.
        task_id = str(uuid.uuid4())
        parent_task_id, root_task_id, task_path = resolve_task_tree_fields(
            self.tasks, workspace_id, task_id, payload.parent_task_id or None
        )
        dependencies = validate_task_dependencies(
            self.tasks, workspace_id, task_id, payload.depends_on_task_ids
        )
        now = self._runtime_now()
        attachments = self._persist_attachments(workspace_id, task_id, payload.attachments)
        autonomy_policy = (
            payload.autonomy_policy or AutonomyPolicy()
            if payload.task_mode == WorkspaceTaskMode.AUTONOMOUS
            else None
        )
        task = WorkspaceTask(
            execution_control=payload.execution_control,
            source=payload.source,
            execution_ref=payload.execution_ref,
            reporter_key_hash=key_hash,
            creation_request_key=creation_request_key,
            creation_actor_key=creation_actor_key,
            creation_fingerprint=creation_fingerprint,
            id=task_id,
            workspace_id=workspace_id,
            title=title,
            prompt=prompt,
            attachments=attachments,
            goal_packet=payload.goal_packet,
            review_profiles=payload.review_profiles,
            agent_type=payload.agent_type,
            session_kind=session_kind,
            task_mode=payload.task_mode,
            execution_complexity=payload.execution_complexity,
            origin=payload.origin,
            agent_tag=payload.agent_tag,
            autonomy_policy=autonomy_policy,
            autonomous_run=(
                self._default_autonomous_run(task_id, autonomy_policy.max_iterations)
                if autonomy_policy and not initiator_owned
                else None
            ),
            status=WorkspaceTaskStatus.TODO,
            related_task_id=payload.related_task_id,
            session_id=payload.session_id or None,
            clear_context=payload.clear_context,
            timeout_seconds=payload.timeout_seconds,
            parent_task_id=parent_task_id,
            depends_on_task_ids=dependencies,
            root_task_id=root_task_id,
            path=task_path,
            consumer_ack_sequence=0,
            system_internal=system_internal,
            internal_kind=internal_kind,
            created_at=now,
            updated_at=now,
        )
        self.tasks[task_id] = task
        self._save_workspace_state(workspace_id)
        logger.info(
            "Created workspace task id=%s workspace_id=%s title=%r related_task_id=%s agent_type=%s",
            task.id,
            workspace_id,
            task.title,
            task.related_task_id,
            task.agent_type,
        )
        return task

    def list_top_level_tasks(self, workspace_id: str) -> list[WorkspaceTask]:
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)
        return top_level_tasks(self.tasks.values(), workspace_id)

    def list_task_subtree(self, workspace_id: str, root_task_id: str) -> list[WorkspaceTask]:
        root = self.require_workspace_task(workspace_id, root_task_id)
        return tasks_in_subtree(self.tasks.values(), workspace_id, root)

    def require_workspace_task(self, workspace_id: str, task_id: str) -> WorkspaceTask:
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)
        task = self.tasks.get(task_id)
        if task is None or task.workspace_id != workspace_id:
            raise KeyError(task_id)
        return task

    def task_mailbox_consumer_key(
        self,
        workspace_id: str,
        task_id: str,
    ) -> str:
        return make_task_consumer_key(self.require_workspace_task(workspace_id, task_id).id)

    def list_task_mailbox_events(
        self,
        workspace_id: str,
        task_id: str,
        *,
        since_sequence: int = 0,
        subtree: bool = False,
    ) -> list[TaskEvent]:
        return self.task_mailbox.wait(
            workspace_id,
            self.task_mailbox_consumer_key(workspace_id, task_id),
            since_sequence=since_sequence,
            subtree=subtree,
        )

    async def wait_task_mailbox_events(
        self,
        workspace_id: str,
        task_id: str,
        *,
        since_sequence: int = 0,
        subtree: bool = False,
        timeout_seconds: float = 30.0,
    ) -> list[TaskEvent]:
        return await self.task_mailbox.wait_for(
            workspace_id,
            self.task_mailbox_consumer_key(workspace_id, task_id),
            since_sequence=since_sequence,
            subtree=subtree,
            timeout_seconds=timeout_seconds,
        )

    def ack_task_mailbox(
        self,
        workspace_id: str,
        sequence: int,
        task_id: str,
    ) -> WorkspaceTask:
        consumer_key = self.task_mailbox_consumer_key(workspace_id, task_id)
        self.task_mailbox.ack(workspace_id, consumer_key, sequence)
        return self.tasks[task_id]
