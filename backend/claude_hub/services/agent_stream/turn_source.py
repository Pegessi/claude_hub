"""Stable provider text formats for server-authored Chat turn sources."""

from __future__ import annotations

import json
from typing import Any, Mapping

FEISHU_PROVIDER_TEXT_FORMAT_V1 = "feishu-v1"
_FEISHU_REQUIRED_FIELDS = ("app_id", "chat_id", "message_id", "sender_open_id")
_FEISHU_PROVIDER_INTRO_V1 = "Claude Hub verified message source (feishu-v1): "


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


def provider_text_for_stored_turn(
    visible_text: str,
    metadata: Mapping[str, Any],
) -> str:
    """Rebuild provider text from immutable metadata on the historical turn."""

    if metadata.get("origin") != "feishu":
        return visible_text
    if metadata.get("provider_text_format") != FEISHU_PROVIDER_TEXT_FORMAT_V1:
        raise ValueError("stored Feishu turn has an unsupported provider text format")
    feishu = metadata.get("feishu")
    if not isinstance(feishu, Mapping) or not all(
        isinstance(feishu.get(field), str) and feishu.get(field)
        for field in _FEISHU_REQUIRED_FIELDS
    ):
        raise ValueError("stored Feishu turn metadata is incomplete")
    return format_feishu_provider_text_v1(
        visible_text,
        app_id=str(feishu["app_id"]),
        chat_id=str(feishu["chat_id"]),
        message_id=str(feishu["message_id"]),
        sender_open_id=str(feishu["sender_open_id"]),
    )


__all__ = [
    "FEISHU_PROVIDER_TEXT_FORMAT_V1",
    "format_feishu_provider_text_v1",
    "provider_text_for_stored_turn",
]
