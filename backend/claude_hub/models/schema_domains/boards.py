"""Workspace board response schemas."""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from claude_hub.models.schema_domains.lifecycle import TaskLifecycle
from claude_hub.models.schema_domains.reports import (
    AgentReport,
    WorkspaceMarkdownDocument,
)
from claude_hub.models.schema_domains.sessions import (
    ManagedSession,
    redact_managed_sessions_for_public,
)
from claude_hub.models.schema_domains.tasks import WorkspaceTask
from claude_hub.models.schema_domains.workspaces import Workspace


class BoardTasksPagination(BaseModel):
    """Pagination metadata for board task history (optional query params)."""

    total_count: int
    has_more: bool
    next_cursor: Optional[str] = None
    limit: Optional[int] = None
    status_counts: Dict[str, int] = Field(default_factory=dict)


class WorkspaceBoard(BaseModel):
    """Workspace board response for Agent Workspace mode."""

    workspace: Workspace
    tasks: List[WorkspaceTask]
    sessions: List[ManagedSession]
    reports: List[AgentReport]
    markdown_documents: List[WorkspaceMarkdownDocument] = Field(default_factory=list)
    snapshot_path: Optional[str] = None
    tasks_pagination: Optional[BoardTasksPagination] = None
    # Derived per-Task lifecycle keyed by Task id; never persisted.
    task_lifecycles: Dict[str, TaskLifecycle] = Field(default_factory=dict)


def redact_workspace_board_for_public(board: WorkspaceBoard) -> WorkspaceBoard:
    """Return a board copy safe for API responses (session env values redacted)."""
    return board.model_copy(update={"sessions": redact_managed_sessions_for_public(board.sessions)})
