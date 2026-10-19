"""Cached agent and quoted-image stream routes."""

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse, RedirectResponse

from claude_hub.api.routes.agent_stream.bindings import AgentStreamRouteBindings
from claude_hub.auth.dependencies import get_current_user
from claude_hub.models import (
    ManagedSession,
    SessionKind,
    StreamAttachmentPreview,
    User,
    WorkspaceArtifactPreview,
)
from claude_hub.services.agent_stream import quoted_images
from claude_hub.services.agent_stream.local_attachments import read_attachment_preview
from claude_hub.services.agent_stream.local_images import (
    AgentImageUnavailable,
    read_cached_agent_image,
)
from claude_hub.services.agent_stream.local_markdown import (
    MarkdownEncodingError,
    MarkdownUnavailable,
    read_markdown_preview,
)

ATTACHMENT_NO_STORE = "no-store"
AGENT_IMAGE_CACHE_CONTROL = "private, max-age=86400, immutable"


def agent_image_not_available() -> HTTPException:
    """Uniform opaque 404 for every denied agent-image request."""
    return HTTPException(
        status_code=404,
        detail="image not available",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": ATTACHMENT_NO_STORE,
        },
    )


def same_session_incarnation(expected: ManagedSession, live: Optional[ManagedSession]) -> bool:
    return bool(
        live is not None
        and live.workspace_id == expected.workspace_id
        and live.tab_id == expected.tab_id
        and live.created_at == expected.created_at
    )


def managed_image_owner_is_current(
    expected: ManagedSession, bindings: AgentStreamRouteBindings
) -> bool:
    return same_session_incarnation(
        expected, bindings.workspace_manager().sessions.get(expected.id)
    )


def tab_image_owner_is_current(
    expected: ManagedSession, bindings: AgentStreamRouteBindings
) -> bool:
    tab = bindings.ttyd_manager().get_tab(expected.tab_id)
    if tab is None or (
        tab.session_kind == SessionKind.CHAT
        and (tab.managed_session_id or tab.workspace_role is not None)
    ):
        return False
    live = bindings.terminal_tab_stream_session(expected.tab_id)
    return same_session_incarnation(expected, live)


async def serve_agent_image(
    session: ManagedSession,
    path: str,
    *,
    owner_is_current: Callable[[], bool],
    cache_control: str,
) -> Response:
    try:
        data, mime = await read_cached_agent_image(
            session,
            path,
            owner_is_current=owner_is_current,
        )
    except AgentImageUnavailable:
        raise agent_image_not_available() from None
    if not owner_is_current():
        raise agent_image_not_available()
    return Response(
        content=data,
        media_type=mime,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": cache_control,
        },
    )


def quoted_image_not_available() -> HTTPException:
    """Uniform opaque 404 for every denied quoted-image request."""
    return HTTPException(
        status_code=404,
        detail="image not available",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": ATTACHMENT_NO_STORE,
        },
    )


def serve_quoted_image(key: str) -> Response:
    try:
        result = quoted_images.resolve_quoted_image(key)
    except quoted_images.QuotedImageUnavailable:
        raise quoted_image_not_available() from None

    if isinstance(result, quoted_images.LocalImage):
        return Response(
            content=result.data,
            media_type=result.media_type,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": AGENT_IMAGE_CACHE_CONTROL,
            },
        )
    if isinstance(result, quoted_images.DataImage):
        return Response(
            content=result.data,
            media_type=result.media_type,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": ATTACHMENT_NO_STORE,
            },
        )
    if isinstance(result, quoted_images.RedirectImage):
        return RedirectResponse(
            url=result.location,
            status_code=307,
            headers={"X-Content-Type-Options": "nosniff"},
        )
    raise quoted_image_not_available()


async def serve_markdown(
    session: ManagedSession, path: str, *, owner_is_current: Callable[[], bool]
) -> Response:
    return await _serve_text_preview(
        session, path, owner_is_current=owner_is_current, reader=read_markdown_preview
    )


async def serve_attachment(
    session: ManagedSession, path: str, *, owner_is_current: Callable[[], bool]
) -> Response:
    return await _serve_text_preview(
        session, path, owner_is_current=owner_is_current, reader=read_attachment_preview
    )


async def _serve_text_preview(
    session: ManagedSession,
    path: str,
    *,
    owner_is_current: Callable[[], bool],
    reader: Callable[[ManagedSession, str], WorkspaceArtifactPreview],
) -> Response:
    headers = {"Cache-Control": ATTACHMENT_NO_STORE, "X-Content-Type-Options": "nosniff"}
    try:
        if not owner_is_current():
            raise MarkdownUnavailable()
        preview = await asyncio.to_thread(reader, session, path)
        if not owner_is_current():
            raise MarkdownUnavailable()
    except MarkdownUnavailable:
        raise HTTPException(404, "document not available", headers=headers) from None
    except MarkdownEncodingError:
        raise HTTPException(415, "document is not UTF-8 text", headers=headers) from None
    return JSONResponse(preview.model_dump(mode="json"), headers=headers)


@dataclass(frozen=True)
class AgentStreamMediaEndpoints:
    get_session_agent_image: Callable[..., Any]
    get_tab_agent_image: Callable[..., Any]
    get_session_quoted_image: Callable[..., Any]
    get_tab_quoted_image: Callable[..., Any]
    get_session_markdown: Callable[..., Any]
    get_tab_markdown: Callable[..., Any]
    get_session_attachment_preview: Callable[..., Any]
    get_tab_attachment_preview: Callable[..., Any]


def register_media_routes(
    router: APIRouter, bindings: AgentStreamRouteBindings
) -> AgentStreamMediaEndpoints:
    """Register media routes in their historical order."""

    @router.get("/sessions/{managed_session_id}/stream/agent-image")
    async def get_session_agent_image(
        managed_session_id: str,
        path: str = Query(..., max_length=4096, description="Absolute or cwd-relative image path"),
        current_user: User = Depends(get_current_user),
    ) -> Response:
        """Serve an agent image scoped to a managed session's working directory."""
        session = bindings.session_or_404(managed_session_id)
        return await serve_agent_image(
            session,
            path,
            owner_is_current=lambda: managed_image_owner_is_current(session, bindings),
            cache_control=ATTACHMENT_NO_STORE,
        )

    @router.get("/tabs/{tab_id}/stream/agent-image")
    async def get_tab_agent_image(
        tab_id: str,
        path: str = Query(..., max_length=4096, description="Absolute or cwd-relative image path"),
        current_user: User = Depends(get_current_user),
    ) -> Response:
        """Serve an image inside cwd or an assistant-quoted temporary image.

        Restricted reader for ``view_image``-style tool output — see
        :func:`read_cached_agent_image` for containment, retention, and content validation.
        """
        session = bindings.terminal_tab_session_or_404(tab_id)
        return await serve_agent_image(
            session,
            path,
            owner_is_current=lambda: tab_image_owner_is_current(session, bindings),
            cache_control=AGENT_IMAGE_CACHE_CONTROL,
        )

    @router.get("/sessions/{managed_session_id}/stream/quoted-image")
    async def get_session_quoted_image(
        managed_session_id: str,
        key: str = Query(
            ...,
            max_length=quoted_images.QUOTED_IMAGE_KEY_MAX_LEN,
            description="Bare provider image token, e.g. img_v3_0215v_…",
        ),
        current_user: User = Depends(get_current_user),
    ) -> Response:
        """Resolve an agent-quoted image for an existing managed session."""
        bindings.session_or_404(managed_session_id)
        return serve_quoted_image(key)

    @router.get("/tabs/{tab_id}/stream/quoted-image")
    async def get_tab_quoted_image(
        tab_id: str,
        key: str = Query(
            ...,
            max_length=quoted_images.QUOTED_IMAGE_KEY_MAX_LEN,
            description="Bare provider image token, e.g. img_v3_0215v_…",
        ),
        current_user: User = Depends(get_current_user),
    ) -> Response:
        """Resolve and serve an agent-quoted bare-token image.

        Provider-neutral entry point; dispatch, containment and content checks
        live in :mod:`services.agent_stream.quoted_images`.
        """
        bindings.terminal_tab_session_or_404(tab_id)
        return serve_quoted_image(key)

    @router.get("/sessions/{managed_session_id}/stream/markdown")
    async def get_session_markdown(
        managed_session_id: str,
        path: str = Query(..., min_length=1, max_length=4096),
        current_user: User = Depends(get_current_user),
    ) -> Response:
        session = bindings.session_or_404(managed_session_id)
        return await serve_markdown(
            session,
            path,
            owner_is_current=lambda: managed_image_owner_is_current(session, bindings),
        )

    @router.get("/tabs/{tab_id}/stream/markdown")
    async def get_tab_markdown(
        tab_id: str,
        path: str = Query(..., min_length=1, max_length=4096),
        current_user: User = Depends(get_current_user),
    ) -> Response:
        session = bindings.terminal_tab_session_or_404(tab_id)
        return await serve_markdown(
            session,
            path,
            owner_is_current=lambda: tab_image_owner_is_current(session, bindings),
        )

    @router.get(
        "/sessions/{managed_session_id}/stream/attachment", response_model=StreamAttachmentPreview
    )
    async def get_session_attachment_preview(
        managed_session_id: str,
        path: str = Query(..., min_length=1, max_length=4096),
        current_user: User = Depends(get_current_user),
    ) -> Response:
        session = bindings.session_or_404(managed_session_id)
        return await serve_attachment(
            session,
            path,
            owner_is_current=lambda: managed_image_owner_is_current(session, bindings),
        )

    @router.get("/tabs/{tab_id}/stream/attachment", response_model=StreamAttachmentPreview)
    async def get_tab_attachment_preview(
        tab_id: str,
        path: str = Query(..., min_length=1, max_length=4096),
        current_user: User = Depends(get_current_user),
    ) -> Response:
        session = bindings.terminal_tab_session_or_404(tab_id)
        return await serve_attachment(
            session,
            path,
            owner_is_current=lambda: tab_image_owner_is_current(session, bindings),
        )

    return AgentStreamMediaEndpoints(
        get_session_attachment_preview=get_session_attachment_preview,
        get_tab_attachment_preview=get_tab_attachment_preview,
        get_session_markdown=get_session_markdown,
        get_tab_markdown=get_tab_markdown,
        get_session_agent_image=get_session_agent_image,
        get_tab_agent_image=get_tab_agent_image,
        get_session_quoted_image=get_session_quoted_image,
        get_tab_quoted_image=get_tab_quoted_image,
    )
