"""Compatibility mixin for managed-session lifecycle."""

from claude_hub.models import TaskCleanupResult as TaskCleanupResult
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403
from claude_hub.services.workspace_manager.sessions.cleanup import _SessionCleanupMixin
from claude_hub.services.workspace_manager.sessions.lifecycle import _SessionLifecycleMixin
from claude_hub.services.workspace_manager.sessions.provisioning import _SessionProvisioningMixin


class _SessionsMixin(
    _SessionLifecycleMixin, _SessionProvisioningMixin, _SessionCleanupMixin, _WorkspaceManagerBase
):
    """Preserve the historical aggregate mixin import path."""
