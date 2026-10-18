"""Compatibility mixin for status monitoring and recovery."""

from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403
from claude_hub.services.workspace_manager.monitoring.recovery import _AutomaticRecoveryMixin
from claude_hub.services.workspace_manager.monitoring.refresh import _StatusRefreshMixin
from claude_hub.services.workspace_manager.monitoring.reports import _ReportReconciliationMixin


class _MonitorMixin(
    _StatusRefreshMixin, _AutomaticRecoveryMixin, _ReportReconciliationMixin, _WorkspaceManagerBase
):
    """Preserve the historical aggregate mixin import path."""
