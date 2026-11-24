"""Personal /ai image acquisition into the existing native Chat attachment contract."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from typing import Any, Sequence

from claude_hub.config import settings
from claude_hub.models.chat_image import ChatImageAttachment
from claude_hub.services.agent_stream.attachments import MAX_PREVIEW_EDGE
from claude_hub.services.agent_stream.media.image_headers import (
    _magic_mime,
    _parse_image_dimensions,
)
from claude_hub.services.feishu_bot import FeishuBotError
from claude_hub.services.feishu_user_message import FeishuUserMessage


async def prepare_images(
    client: Any, message: FeishuUserMessage, context: Sequence[FeishuUserMessage]
) -> tuple[list[ChatImageAttachment], list[dict[str, Any]]]:
    """Fetch originals before any model input, fail explicitly rather than silently dropping images."""
    from claude_hub.api.agent_stream import _MAX_ATTACHMENTS, _decode_attachments

    attachments: list[ChatImageAttachment] = []
    provenance: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    # Command first, then the quoted parent, then other bounded conversation context.
    sources = [message, *sorted(context, key=lambda m: m.message_id != message.parent_id)]
    async with asyncio.timeout(90):
        for source in sources:
            if source.has_images and not source.image_refs:
                refreshed = await client.get_message(source.message_id)
                if refreshed is None or not refreshed.image_refs:
                    raise FeishuBotError(
                        "Image resource metadata is unavailable; retry after checking read access"
                    )
                source = refreshed
            for source_id, key in source.image_refs:
                if (source_id, key) in seen:
                    continue
                seen.add((source_id, key))
                if len(seen) > _MAX_ATTACHMENTS:
                    raise FeishuBotError("Too many images for one Agent turn")
                owner = source
                if source_id != source.message_id:
                    owner = await client.get_message(source_id)
                    if owner is None:
                        raise FeishuBotError("Referenced image message is unavailable")
                data = await client.download_message_image(owner, key)
                mime = _magic_mime(data)
                if mime is None:
                    raise FeishuBotError("Downloaded resource is not a supported image")
                width, height = _parse_image_dimensions(data, mime)
                if width <= 0 or height <= 0:
                    raise FeishuBotError("Downloaded image dimensions are invalid")
                attachment = ChatImageAttachment(
                    filename=f"image-{len(attachments)+1}.{mime.split('/')[-1]}",
                    mime_type=mime,
                    data_url=f"data:{mime};base64," + base64.b64encode(data).decode("ascii"),
                    preview_is_original=(
                        len(data) <= settings.attachment_max_preview_bytes
                        and max(width, height) <= MAX_PREVIEW_EDGE
                    ),
                )
                attachments.append(attachment)
                # Validate the shared per-image and aggregate limits incrementally.
                _decode_attachments(attachments)
                provenance.append(
                    dict(
                        message_id=source_id,
                        chat_id=owner.chat_id,
                        resource_key=key,
                        transport="openapi_user" if source_id.startswith("om_") else "web_session",
                        mime_type=mime,
                        width=width,
                        height=height,
                        bytes=len(data),
                        sha256=hashlib.sha256(data).hexdigest(),
                    )
                )
    return attachments, provenance


def message_from_event(event: Any, fetched: FeishuUserMessage | None = None) -> FeishuUserMessage:
    """Keep WS image ownership, including resources belonging to forwarded child messages."""
    refs = tuple(
        dict.fromkeys(
            (
                *event.image_refs,
                *((event.message_id, key) for key in event.image_keys),
                *(fetched.image_refs if fetched else ()),
            )
        )
    )
    return FeishuUserMessage(
        message_id=event.message_id,
        chat_id=event.chat_id or (fetched.chat_id if fetched else ""),
        sender_open_id=fetched.sender_open_id if fetched else event.sender_open_id,
        sender_type="user",
        msg_type=event.message_type or (fetched.msg_type if fetched else "text"),
        text=event.text,
        create_time_ms=event.message_created_at_ms or (fetched.create_time_ms if fetched else 0),
        thread_id=event.thread_id or (fetched.thread_id if fetched else None),
        parent_id=event.parent_id or (fetched.parent_id if fetched else None),
        root_id=event.root_id or (fetched.root_id if fetched else None),
        has_images=bool(refs) or bool(fetched and fetched.has_images),
        image_refs=refs,
    )
