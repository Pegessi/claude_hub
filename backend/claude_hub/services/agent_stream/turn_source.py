"""Stable provider text formats for server-authored Chat turn sources."""

from __future__ import annotations

import json
from typing import Any, Mapping

FEISHU_PROVIDER_TEXT_FORMAT_V1 = "feishu-v1"
FEISHU_PROVIDER_TEXT_FORMAT_V2 = "feishu-v2"
_FEISHU_REQUIRED_FIELDS = ("app_id", "chat_id", "message_id", "sender_open_id")
_FEISHU_PROVIDER_INTRO_V1 = "Claude Hub verified message source (feishu-v1): "
_FEISHU_PROVIDER_INTRO_V2 = "Claude Hub verified message source (feishu-v2): "
_FEISHU_OUTPUT_GUIDANCE_V2 = (
    "Your final response will be delivered to Feishu. Write it as lightweight Markdown that "
    "can be converted reliably: use headings, lists, links, blockquotes, inline code, and "
    "fenced code blocks as needed. Avoid raw HTML, Markdown tables, or interactive cards. "
    "Do not write mentions or raw Feishu tags such as <at user_id=...>; those require a "
    "separate controlled capability."
)


def format_feishu_provider_text_v1(
    text: str,
    *,
    app_id: str,
    chat_id: str,
    message_id: str,
    sender_open_id: str,
) -> str:
    """Build the immutable v1 provider text for one verified Feishu message.

    This exact format is part of the edit-resend contract. Historical turns
    record its version in metadata, so future wording changes must add another
    formatter instead of changing v1 in place.
    """

    source = json.dumps(
        {
            "app_id": app_id,
            "chat_id": chat_id,
            "message_id": message_id,
            "sender_open_id": sender_open_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{_FEISHU_PROVIDER_INTRO_V1}{source}\n\n{text}"


def format_feishu_provider_text_v2(
    text: str,
    *,
    app_id: str,
    chat_id: str,
    message_id: str,
    sender_open_id: str,
) -> str:
    """Build v2 provider text with Feishu output-format guidance."""

    source = json.dumps(
        {
            "app_id": app_id,
            "chat_id": chat_id,
            "message_id": message_id,
            "sender_open_id": sender_open_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{_FEISHU_PROVIDER_INTRO_V2}{source}\n{_FEISHU_OUTPUT_GUIDANCE_V2}\n\n{text}"


def provider_text_for_stored_turn(
    visible_text: str,
    metadata: Mapping[str, Any],
) -> str:
    """Rebuild provider text from immutable metadata on the historical turn."""

    if metadata.get("origin") != "feishu":
        return visible_text
    provider_format = metadata.get("provider_text_format")
    if provider_format not in {FEISHU_PROVIDER_TEXT_FORMAT_V1, FEISHU_PROVIDER_TEXT_FORMAT_V2}:
        raise ValueError("stored Feishu turn has an unsupported provider text format")
    feishu = metadata.get("feishu")
    if not isinstance(feishu, Mapping) or not all(
        isinstance(feishu.get(field), str) and feishu.get(field)
        for field in _FEISHU_REQUIRED_FIELDS
    ):
        raise ValueError("stored Feishu turn metadata is incomplete")
    formatter = (
        format_feishu_provider_text_v1
        if provider_format == FEISHU_PROVIDER_TEXT_FORMAT_V1
        else format_feishu_provider_text_v2
    )
    return formatter(
        visible_text,
        app_id=str(feishu["app_id"]),
        chat_id=str(feishu["chat_id"]),
        message_id=str(feishu["message_id"]),
        sender_open_id=str(feishu["sender_open_id"]),
    )


__all__ = [
    "FEISHU_PROVIDER_TEXT_FORMAT_V1",
    "FEISHU_PROVIDER_TEXT_FORMAT_V2",
    "format_feishu_provider_text_v1",
    "format_feishu_provider_text_v2",
    "provider_text_for_stored_turn",
]
