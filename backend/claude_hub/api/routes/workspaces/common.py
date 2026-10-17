"""Shared workspace HTTP behavior."""

import hashlib
import json
from typing import Any, Callable, Coroutine

from fastapi import HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from claude_hub.models import WorkspaceBoard
from claude_hub.services.task_graph import TaskHasDescendantsError
from claude_hub.services.task_mailbox import TaskCallIdConflict
from claude_hub.services.workspace_identity import AmbiguousWorkspaceError, DuplicateWorkspaceError
from claude_hub.services.workspace_manager._task_execution import (
    TaskExecutionConflict,
    TaskReporterForbidden,
)

_VOLATILE_SESSION_FIELDS = ("updated_at", "last_activity_at")
_VOLATILE_MARKDOWN_FIELDS = ("updated_at",)


class TaskSecretSafeRoute(APIRoute):
    """Prevent task reporter secrets from leaking through validation errors."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()
        protected = self.endpoint.__name__ in {
            "create_task",
            "task_progress",
            "manual_task_progress",
            "handoff_task_execution",
        }

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                body = exc.body
                contains_secret = isinstance(body, dict) and bool(
                    {"reporter_key", "new_reporter_key"}.intersection(body)
                )
                if protected or contains_secret:
                    raise HTTPException(status_code=422, detail="invalid_task_request") from None
                raise

        return handle


def board_etag(board: WorkspaceBoard) -> str:
    """Compute a stable, order-independent ETag over rendered board content."""
    payload: dict[str, Any] = json.loads(board.model_dump_json())

    for session in payload.get("sessions") or []:
        for field in _VOLATILE_SESSION_FIELDS:
            session.pop(field, None)

    for document in payload.get("markdown_documents") or []:
        for field in _VOLATILE_MARKDOWN_FIELDS:
            document.pop(field, None)

    for key in ("tasks", "sessions", "reports", "markdown_documents"):
        items = payload.get(key)
        if isinstance(items, list):
            items.sort(key=lambda item: item.get("id") or "")

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f'"{digest}"'


def task_public_http(exc: Exception) -> HTTPException:
    """Map internal task failures to the stable public HTTP contract."""
    if isinstance(exc, TaskReporterForbidden):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, TaskExecutionConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc) or "Not found")
    if isinstance(exc, TaskCallIdConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, TaskHasDescendantsError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (DuplicateWorkspaceError, AmbiguousWorkspaceError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (ValueError, RuntimeError)):
        return HTTPException(status_code=400, detail=str(exc))
    raise exc
