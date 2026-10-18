"""Read-only report queries used by workspace boards."""

from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _ReportQueriesMixin(_WorkspaceManagerBase):
    def reports_for_workspace(self, workspace_id: str) -> list[AgentReport]:
        return sorted(
            [report for report in self.reports.values() if report.workspace_id == workspace_id],
            key=lambda report: report.created_at,
        )

    def latest_reports_per_task_for_workspace(self, workspace_id: str) -> list[AgentReport]:
        """Latest report per ``task_id`` for the board.

        The board only renders the most recent report per task card; the full
        per-task history is fetched on demand by the detail panel. Trimming here
        keeps the board payload an order of magnitude smaller (the full history
        can be thousands of reports) without changing what any card shows.
        """
        latest: dict[Optional[str], AgentReport] = {}
        for report in self.reports_for_workspace(workspace_id):  # asc by created_at
            latest[report.task_id] = report  # later (newer) overwrites
        return sorted(latest.values(), key=lambda report: report.created_at)

    def reports_for_task(self, workspace_id: str, task_id: str) -> list[AgentReport]:
        """Full report history for a single task, sorted ascending by created_at."""
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)
        return [
            report
            for report in self.reports_for_workspace(workspace_id)
            if report.task_id == task_id
        ]
