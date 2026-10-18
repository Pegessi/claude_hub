"""Typed runtime bindings for workspace routes."""

from typing import Protocol

from claude_hub.services import WorkspaceManager


class WorkspaceRouteBindings(Protocol):
    """Resolve compatibility-owned workspace runtime dependencies."""

    def workspace_manager(self) -> WorkspaceManager:
        """Return the currently bound workspace manager."""
