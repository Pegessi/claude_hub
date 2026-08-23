"""State persistence and snapshot writing."""

import os
import tempfile

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ._constants import *  # noqa: F401,F403


class _PersistenceMixin:
    def _atomic_write_text(self, path: Path, text: str) -> None:
        """Atomically write text to ``path`` via a temp file + os.replace.

        This ensures readers never see a partially-written state file even
        if the process crashes mid-write.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            # Clean up the temp file on failure.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _workspace_state_payload(self, workspace_id: str) -> dict[str, Any]:
        """Serialize one workspace's durable mutable state."""

        payload = {
            "tasks": [
                item.model_dump(mode="json")
                for item in self.tasks.values()
                if item.workspace_id == workspace_id
            ],
            "sessions": [
                item.model_dump(mode="json")
                for item in self.sessions.values()
                if item.workspace_id == workspace_id
            ],
            "reports": [
                item.model_dump(mode="json")
                for item in self.reports.values()
                if item.workspace_id == workspace_id
            ],
        }
        payload.update(self.agent_tree.to_dict(workspace_id))
        payload.update(self.task_mailbox.to_dict(workspace_id))
        return payload

    def _save_report_intake_workspace_state(self, workspace_id: str) -> None:
        """Commit one report-intake transaction at its workspace state file.

        Report intake never mutates the workspace index or any other
        workspace.  Its sole durable commit point is therefore the atomic
        replacement of ``<workspace>/state.json``.  The Markdown snapshot is
        derived operator output: a failure writing it after the state commit
        must not turn a committed report into an apparent rollback.
        """

        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)
        payload = self._workspace_state_payload(workspace_id)
        self._atomic_write_text(
            self._workspace_state_file(workspace_id),
            json.dumps(payload, indent=2),
        )
        try:
            self._write_snapshot(workspace_id)
        except Exception:
            logger.exception(
                "Report intake committed state.json but failed to refresh derived snapshot "
                "workspace_id=%s",
                workspace_id,
            )

    def _save_state(self) -> None:
        # ``create_report`` deliberately retains this public/internal save
        # call so existing failure injection hooks still exercise the report
        # transaction.  Task-local routing makes the production operation a
        # one-workspace commit instead of rewriting the index and unrelated
        # workspaces.
        report_workspace_id = self._report_intake_workspace.get()
        if report_workspace_id is not None:
            self._save_report_intake_workspace_state(report_workspace_id)
            return

        _wm.STATE_ROOT.mkdir(parents=True, exist_ok=True)
        index_payload = {
            "workspaces": [item.model_dump(mode="json") for item in self.workspaces.values()]
        }
        self._atomic_write_text(INDEX_FILE, json.dumps(index_payload, indent=2))

        for workspace in self.workspaces.values():
            workspace_dir = self._workspace_dir(workspace.id)
            workspace_dir.mkdir(parents=True, exist_ok=True)
            payload = self._workspace_state_payload(workspace.id)
            self._atomic_write_text(
                self._workspace_state_file(workspace.id),
                json.dumps(payload, indent=2),
            )
            self._write_snapshot(workspace.id)

    def _write_snapshot(self, workspace_id: str) -> None:
        workspace = self.workspaces.get(workspace_id)
        if not workspace:
            return

        sessions = self._sessions_for_workspace_raw(workspace_id)
        tasks = [task for task in self.tasks.values() if task.workspace_id == workspace_id]
        lines = [
            "# Claude Hub Workspace State",
            "",
            f"Generated: {_wm._now().isoformat(timespec='seconds')}",
            f"Workspace: {workspace.name}",
            f"Target: {workspace.target.value}",
            f"Local workspace dir: {workspace.path}",
            f"Remote profile: {workspace.remote_profile_id or 'none'}",
            f"Remote start dir: {self._workspace_remote_cwd(workspace) if workspace.target == ExecutionTarget.REMOTE else 'n/a'}",
            f"Default branch: {workspace.default_branch}",
            "",
            "## Agents",
        ]
        if not sessions:
            lines.append("- No managed agents yet.")
        for session in sorted(sessions, key=lambda item: item.created_at):
            current_task_id = session.current_task_id or session.task_id
            current = current_task_id or "none"
            lines.append(
                "- "
                f"{session.id}: role={session.role.value}, type={session.agent_type.value}, "
                f"target={session.target.value}, runtime={session.runtime_status.value}, current_task={current}, "
                f"queued={self._queued_count(session.id)}, path={session.workspace_path}"
            )

        lines.extend(["", "## Tasks"])
        if not tasks:
            lines.append("- No tasks yet.")
        for task in sorted(tasks, key=lambda item: item.created_at):
            target = task.session_id or "unassigned"
            autonomy = (
                f", autonomous_phase={task.autonomous_run.phase.value}"
                if task.autonomous_run
                else ""
            )
            lines.append(
                "- "
                f"{task.id}: status={task.status.value}, mode={task.task_mode.value}, "
                f"title={task.title}, target_agent={target}, "
                f"pending_dispatch={task.dispatch_pending}{autonomy}"
            )

        lines.extend(
            [
                "",
                "## Coordination Notes",
                "- Only work on the task explicitly assigned to your agent session.",
                "- The workspace is an environment, not necessarily a single repository.",
                "- Use the task instructions to choose the correct local or remote project directory.",
                "- Check for existing file changes before editing; do not overwrite work from another agent.",
                "- If your terminal asks for human input, stop and let the task move to review.",
                "- Reviewer agents are independent quality gates; dispatcher agents are reserved for future smart assignment flows.",
            ]
        )
        self._atomic_write_text(self.snapshot_path(workspace_id), "\n".join(lines) + "\n")
