"""State persistence and snapshot writing."""

import os
import tempfile

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ..workspace_snapshot import render_workspace_snapshot
from ._constants import *  # noqa: F401,F403


class _PersistenceMixin:
    def _task_dump_for_state(self, task: WorkspaceTask) -> dict[str, Any]:
        """Serialize a task for durable state without null optional metadata keys."""

        payload = task.model_dump(mode="json")
        if task.reporter_key_hash is not None:
            payload["reporter_key_hash"] = task.reporter_key_hash
        if task.execution_call_fingerprints:
            payload["execution_call_fingerprints"] = dict(task.execution_call_fingerprints)
        for name in ("creation_request_key", "creation_actor_key", "creation_fingerprint"):
            value = getattr(task, name)
            if value is not None:
                payload[name] = value
        if payload.get("agent_tag") is None:
            payload.pop("agent_tag", None)
        return payload

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
                self._task_dump_for_state(item)
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
        state_text = json.dumps(payload, indent=2)
        self._atomic_write_text(self._workspace_state_file(workspace_id), state_text)
        self._refresh_snapshot_best_effort(workspace_id, state_text=state_text)

    def _refresh_snapshot_best_effort(
        self, workspace_id: str, *, state_text: str | None = None
    ) -> None:
        try:
            self._write_snapshot(workspace_id, state_text=state_text)
        except Exception:
            logger.exception(
                "Committed state.json is authoritative; failed to refresh derived snapshot "
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
            "workspaces": [self._workspace_index_item(item) for item in self.workspaces.values()]
        }
        self._atomic_write_text(INDEX_FILE, json.dumps(index_payload, indent=2))

        for workspace in self.workspaces.values():
            workspace_dir = self._workspace_dir(workspace.id)
            workspace_dir.mkdir(parents=True, exist_ok=True)
            payload = self._workspace_state_payload(workspace.id)
            state_text = json.dumps(payload, indent=2)
            self._atomic_write_text(self._workspace_state_file(workspace.id), state_text)
            self._refresh_snapshot_best_effort(workspace.id, state_text=state_text)

    def _write_snapshot(self, workspace_id: str, *, state_text: str | None = None) -> None:
        """Render a committed payload; direct/cold-start calls read the authoritative file."""
        workspace = self.workspaces.get(workspace_id)
        if not workspace:
            return
        if state_text is None:
            state_text = self._workspace_state_file(workspace_id).read_text(encoding="utf-8")
        snapshot = render_workspace_snapshot(
            workspace.model_dump(mode="json"), state_text, _wm._now()
        )
        self._atomic_write_text(self.snapshot_path(workspace_id), snapshot)
