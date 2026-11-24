"""Resolve whether the owner participated in a Web-native topic."""

from __future__ import annotations

import asyncio
from collections import namedtuple
from typing import Any

from claude_hub.services.feishu_user_client import FeishuUserMessage


async def owner_participated(
    user_client: Any,
    topic_id: str,
    owner_ids: set[str],
    before_ms: int,
    *,
    web_transport: Any,
    topic_position: int | None,
) -> bool:
    """Find an earlier owner reply, or fail closed if topic history is incomplete."""
    if not owner_ids or not topic_id or before_ms <= 0:
        raise ValueError("topic participation cannot be verified")
    token: str | None = None
    seen: set[str] = set()
    for _ in range(100):
        async with asyncio.timeout(10):
            page = await user_client.list_messages_page(
                container_id_type="thread",
                container_id=topic_id,
                sort_type="ByCreateTimeDesc",
                page_size=50,
                page_token=token,
            )
        for item in page.items:
            if not isinstance(item, FeishuUserMessage) or item.sender_open_id not in owner_ids:
                continue
            if item.create_time_ms <= 0:
                raise ValueError("topic participation order is ambiguous")
            if 0 < item.create_time_ms < before_ms:
                return True
            if item.create_time_ms == before_ms and before_ms > 0:
                if item.message_id == topic_id and topic_position is not None:
                    # The root necessarily exists before its topic replies, even
                    # when both timestamps have only one-second precision.
                    return True
                cached = getattr(web_transport, "_msg_cache", {}).get(item.message_id)
                other_pos = getattr(cached, "topic_position", None)
                if other_pos is None or topic_position is None:
                    raise ValueError("topic participation order is ambiguous")
                if other_pos < topic_position:
                    return True
        if not page.has_more:
            return False
        token = page.page_token
        if not token or token in seen:
            raise ValueError("topic history pagination did not advance")
        seen.add(token)
    raise ValueError("topic history exceeds recovery limit")


_OwnerReplyMark = namedtuple(
    "_OwnerReplyMark", ("create_time_ms", "position", "topic_position", "message_id")
)


def _reply_mark_is_newer_or_equal(candidate: _OwnerReplyMark, existing: _OwnerReplyMark) -> bool:
    if candidate.create_time_ms != existing.create_time_ms:
        return bool(candidate.create_time_ms > existing.create_time_ms)
    if existing.topic_position > 0 and candidate.topic_position <= 0:
        return False
    if candidate.position != existing.position and candidate.position > 0 and existing.position > 0:
        return bool(candidate.position >= existing.position)
    if existing.position > 0 and candidate.position <= 0:
        return bool(
            existing.topic_position > 0 and candidate.topic_position > existing.topic_position
        )
    return bool(candidate.topic_position >= existing.topic_position)


def _is_newer_reply(
    cand_ms: int,
    cand_pos: int,
    cand_tpos: int,
    target_ms: int,
    target_pos: int = 0,
    target_tpos: int = 0,
    direct_anchor: bool = False,
) -> bool:
    if (direct_anchor and cand_ms > 0 and target_ms <= 0) or (cand_ms > target_ms > 0):
        return True
    if cand_ms != target_ms or target_ms <= 0:
        return False
    if target_pos > 0 and cand_pos > 0 and cand_pos != target_pos:
        return cand_pos > target_pos
    if target_tpos > 0 and cand_tpos > 0 and cand_tpos != target_tpos:
        return cand_tpos > target_tpos
    return direct_anchor
