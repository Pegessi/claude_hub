"""Workspace orchestration manager (package split from the former single module).

Public surface is unchanged: ``WorkspaceManager``, the ``workspace_manager``
singleton, and all module-level constants/helpers are re-exported here so that
``claude_hub.services.workspace_manager`` behaves exactly as before, including
monkeypatch targets such as ``STATE_ROOT`` and ``_now``.
"""

from ._artifacts import _ArtifactsMixin
from ._attachments import _AttachmentsMixin
from ._constants import *  # noqa: F401,F403
from ._constants import (  # noqa: F401  (ensure underscore-prefixed names are package attrs)
    _format_duration,
    _now,
    _safe_attachment_filename,
    _slug,
    _sort_time,
    logger,
)
from ._dispatch import _DispatchMixin
from ._feedback import _FeedbackMixin
from ._messaging import _MessagingMixin
from ._monitor import _MonitorMixin
from ._normalize import _NormalizeMixin
from ._persistence import _PersistenceMixin
from ._prompts import _PromptsMixin
from ._reports import _ReportsMixin
from ._review import _ReviewMixin
from ._sessions import _SessionsMixin
from ._state import _StateMixin
from ._task_updates import _TaskUpdatesMixin
from ._tasks import _TasksMixin
from ._tmux_queries import _TmuxQueriesMixin
from ._workspaces import _WorkspacesMixin, build_resident_agent_prompt  # noqa: F401


class WorkspaceManager(
    _StateMixin,
    _NormalizeMixin,
    _PersistenceMixin,
    _WorkspacesMixin,
    _TasksMixin,
    _AttachmentsMixin,
    _ArtifactsMixin,
    _TaskUpdatesMixin,
    _FeedbackMixin,
    _SessionsMixin,
    _DispatchMixin,
    _PromptsMixin,
    _MessagingMixin,
    _ReportsMixin,
    _ReviewMixin,
    _TmuxQueriesMixin,
    _MonitorMixin,
):
    """Human-orchestrated workspace/task/session layer above TTYDManager."""


workspace_manager = WorkspaceManager()
