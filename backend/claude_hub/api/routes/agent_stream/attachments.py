"""Agent-stream attachment decoding and preview routes."""

from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Response

from claude_hub.api.routes.agent_stream.bindings import AgentStreamRouteBindings
from claude_hub.auth.dependencies import get_current_user
from claude_hub.models import User
from claude_hub.models.chat_image import ChatImageAttachment as AgentStreamAttachment
from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore

ATTACHMENT_CACHE_CONTROL = "private, max-age=300"
ATTACHMENT_NO_STORE = "no-store"
TAB_STREAM_WORKSPACE_ID = "terminal-tabs"


def parse_data_url(data_url: str) -> Tuple[str, bytes]:
    """Parse a ``data:`` URL into ``(mime_type, bytes)``.

    Only ``data:<mime>;base64,<payload>`` form is accepted. The comma and
    ``;base64`` marker are required; malformed URLs (e.g. ``data:image/png``
    with no payload) are rejected with 400 rather than silently returning
    empty bytes.
    """
    import base64

    if not data_url.startswith("data:"):
        raise HTTPException(status_code=400, detail="attachment is not a data URL")
    rest = data_url[len("data:") :]
    comma_idx = rest.find(",")
    if comma_idx < 0:
        raise HTTPException(
            status_code=400,
            detail="attachment data URL is missing the payload comma",
        )
    header = rest[:comma_idx]
    b64 = rest[comma_idx + 1 :]
    # header is "<mime>[;base64]"
    parts = header.split(";")
    mime = parts[0] or "application/octet-stream"
    if "base64" not in parts[1:]:
        raise HTTPException(
            status_code=400,
            detail="attachment data URL must use base64 encoding",
        )
    try:
        data = base64.b64decode(b64, validate=True)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"attachment is not valid base64: {exc}",
        )
    return mime, data


def decode_attachments(
    attachments: List[AgentStreamAttachment],
    *,
    max_attachments: int,
    max_attachment_bytes: int,
    max_total_attachment_bytes: int,
) -> Tuple[List[bytes], Optional[List[Optional[bytes]]]]:
    """Decode data-URL attachments into ``(originals, previews)``.

    Returns a list of original bytes and either a list of optional preview
    bytes (one slot per original) or ``None`` if no previews were supplied.

    Preview presence may be mixed so an oversized image does not suppress
    safe previews for the other images in the turn. Setting both preview
    forms on one attachment is rejected with 400.
    When previews are absent, ``previews`` is ``None`` so the tailer emits
    placeholder metadata and never persists original bytes.

    For originals, the declared ``mime_type``, the data URL media type, and
    the magic bytes must agree; unsupported MIME types (non-PNG/JPEG/GIF/WebP)
    are rejected.

    For previews, the data URL media type and the preview's own magic bytes
    must agree, and the preview MIME must be in the whitelist. The preview
    MIME is *not* required to match the original MIME — browsers commonly
    transcode PNG/GIF originals to bounded WebP/JPEG previews to meet the
    size cap. The store persists the preview under its detected MIME.

    Fail-closed: any malformed data URL, oversized file, MIME mismatch, or
    unsupported type raises ``HTTPException(400)``.
    """
    from claude_hub.services.agent_stream.attachments import (
        _ALLOWED_MIME_TYPES,
        _magic_mime,
    )

    if len(attachments) > max_attachments:
        raise HTTPException(
            status_code=400,
            detail=f"too many attachments: {len(attachments)} > {max_attachments}",
        )

    originals: List[bytes] = []
    previews: List[Optional[bytes]] = []
    for att in attachments:
        if att.preview_is_original and att.preview_data_url is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"attachment {att.filename!r} sets both preview_data_url "
                    "and preview_is_original"
                ),
            )
    has_previews = [a.preview_data_url is not None or a.preview_is_original for a in attachments]
    any_preview = any(has_previews)

    total_bytes = 0
    for att in attachments:
        # Decode and validate the original.
        orig_mime, original = parse_data_url(att.data_url)
        if len(original) > max_attachment_bytes:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"attachment {att.filename!r} exceeds size limit: "
                    f"{len(original)} > {max_attachment_bytes} bytes"
                ),
            )
        total_bytes += len(original)
        if total_bytes > max_total_attachment_bytes:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"total attachment size exceeds limit: "
                    f"{total_bytes} > {max_total_attachment_bytes} bytes"
                ),
            )
        if att.mime_type not in _ALLOWED_MIME_TYPES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"attachment {att.filename!r} has unsupported mime type " f"{att.mime_type!r}"
                ),
            )
        if orig_mime != att.mime_type:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"attachment {att.filename!r}: data URL media type "
                    f"{orig_mime!r} does not match declared mime_type "
                    f"{att.mime_type!r}"
                ),
            )
        detected = _magic_mime(original)
        if detected != att.mime_type:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"attachment {att.filename!r}: magic bytes indicate "
                    f"{detected!r} but declared mime_type is {att.mime_type!r}"
                ),
            )
        originals.append(original)

        # Decode and validate the preview (if supplied). The preview MIME is
        # independent of the original MIME — browsers may transcode to a
        # different format to meet the size cap.
        if att.preview_is_original:
            previews.append(original)
        elif att.preview_data_url is not None:
            prev_mime, preview = parse_data_url(att.preview_data_url)  # type: ignore[arg-type]
            if len(preview) > max_attachment_bytes:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"attachment {att.filename!r} preview exceeds size limit: "
                        f"{len(preview)} > {max_attachment_bytes} bytes"
                    ),
                )
            total_bytes += len(preview)
            if total_bytes > max_total_attachment_bytes:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"total attachment size exceeds limit: "
                        f"{total_bytes} > {max_total_attachment_bytes} bytes"
                    ),
                )
            if prev_mime not in _ALLOWED_MIME_TYPES:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"attachment {att.filename!r}: preview has unsupported "
                        f"mime type {prev_mime!r}"
                    ),
                )
            detected_prev = _magic_mime(preview)
            if detected_prev != prev_mime:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"attachment {att.filename!r}: preview magic bytes "
                        f"indicate {detected_prev!r} but data URL media type "
                        f"is {prev_mime!r}"
                    ),
                )
            previews.append(preview)
        elif any_preview:
            previews.append(None)

    return originals, (previews if any_preview else None)


async def read_attachment_or_404(
    store: "AgentStreamAttachmentStore", attachment_id: str
) -> Tuple[bytes, str]:
    """Return ``(preview_bytes, mime_type)`` for an existing attachment.

    Raises ``HTTPException(404)`` for an invalid attachment id format and
    ``HTTPException(410)`` for a valid id whose preview has been evicted or
    never existed. Both responses carry ``X-Content-Type-Options: nosniff``
    and ``Cache-Control: no-store`` so the frontend can render a visible
    "Preview expired" placeholder instead of a silently broken image.
    """
    import uuid

    # Distinguish invalid id format (404) from valid-but-evicted (410).
    try:
        uuid.UUID(attachment_id)
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=404,
            detail="invalid attachment id",
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": ATTACHMENT_NO_STORE,
            },
        )
    try:
        return await store.read(attachment_id)
    except KeyError:
        raise HTTPException(
            status_code=410,
            detail="attachment preview not available",
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": ATTACHMENT_NO_STORE,
            },
        )


@dataclass(frozen=True)
class AgentStreamAttachmentEndpoints:
    get_session_attachment: Callable[..., Any]
    get_tab_attachment: Callable[..., Any]


def register_attachment_routes(
    router: APIRouter, bindings: AgentStreamRouteBindings
) -> AgentStreamAttachmentEndpoints:
    """Register attachment routes in their historical order."""

    @router.get("/sessions/{managed_session_id}/stream/attachments/{attachment_id}")
    async def get_session_attachment(
        managed_session_id: str,
        attachment_id: str,
        current_user: User = Depends(get_current_user),
    ) -> Response:
        """Serve a bounded preview image for a managed session turn.

        Successful responses carry the whitelisted ``Content-Type``,
        ``X-Content-Type-Options: nosniff``, and a private cache policy. Missing
        or evicted previews return ``410 Gone`` (or ``404`` for an invalid id)
        with ``no-store`` so the frontend can render a visible "Preview expired"
        placeholder instead of a silently blank image.
        """
        session = bindings.session_or_404(managed_session_id)
        store = AgentStreamAttachmentStore(session.workspace_id, session.id)
        data, mime = await bindings.read_attachment_or_404(store, attachment_id)
        return Response(
            content=data,
            media_type=mime,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": ATTACHMENT_CACHE_CONTROL,
            },
        )

    @router.get("/tabs/{tab_id}/stream/attachments/{attachment_id}")
    async def get_tab_attachment(
        tab_id: str,
        attachment_id: str,
        current_user: User = Depends(get_current_user),
    ) -> Response:
        """Serve a bounded preview image for a terminal AI tab turn."""
        session = bindings.terminal_tab_session_or_404(tab_id)
        store = AgentStreamAttachmentStore(TAB_STREAM_WORKSPACE_ID, session.id)
        data, mime = await bindings.read_attachment_or_404(store, attachment_id)
        return Response(
            content=data,
            media_type=mime,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": ATTACHMENT_CACHE_CONTROL,
            },
        )

    return AgentStreamAttachmentEndpoints(
        get_session_attachment=get_session_attachment,
        get_tab_attachment=get_tab_attachment,
    )
