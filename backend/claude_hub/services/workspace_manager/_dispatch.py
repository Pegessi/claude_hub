"""Compatibility mixin for task start, queueing, continuation, and delivery."""

from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403
from claude_hub.services.workspace_manager.dispatching.continuation import _TaskContinuationMixin
from claude_hub.services.workspace_manager.dispatching.delivery import _DispatchDeliveryMixin
from claude_hub.services.workspace_manager.dispatching.queue import (
    _NON_MIGRATABLE_DISPATCH_REASONS as _NON_MIGRATABLE_DISPATCH_REASONS,
)
from claude_hub.services.workspace_manager.dispatching.queue import (
    _DispatchQueueMixin,
)
from claude_hub.services.workspace_manager.dispatching.start import _TaskStartMixin


class _DispatchMixin(
    _TaskStartMixin,
    _TaskContinuationMixin,
    _DispatchQueueMixin,
    _DispatchDeliveryMixin,
    _WorkspaceManagerBase,
):
    """Preserve the historical aggregate mixin import path."""
