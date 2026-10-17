"""Compatibility mixin for tmux transport and workspace read models."""

from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403
from claude_hub.services.workspace_manager.tmux.board import _WorkspaceBoardMixin
from claude_hub.services.workspace_manager.tmux.inventory import _SessionInventoryMixin
from claude_hub.services.workspace_manager.tmux.reports import _ReportQueriesMixin
from claude_hub.services.workspace_manager.tmux.review_reaper import _ReviewReaperMixin
from claude_hub.services.workspace_manager.tmux.transport import _TmuxTransportMixin


class _TmuxQueriesMixin(
    _ReportQueriesMixin,
    _TmuxTransportMixin,
    _SessionInventoryMixin,
    _ReviewReaperMixin,
    _WorkspaceBoardMixin,
    _WorkspaceManagerBase,
):
    """Preserve the historical aggregate mixin import path."""
