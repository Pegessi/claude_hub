"""Fetch bounded Feishu context and resolve the referenced parent."""

from __future__ import annotations

import logging
from typing import Sequence

from claude_hub.services.feishu_ai_command_models import (
    AiCommandContextRefs,
    FeishuAiCommandConfig,
    extract_context_references,
)
from claude_hub.services.feishu_user_client import FeishuUserClient, FeishuUserMessage

logger = logging.getLogger(__name__)


async def fetch_context_and_refs(
    client: FeishuUserClient,
    message: FeishuUserMessage,
    cfg: FeishuAiCommandConfig,
    *,
    seeded_context: Sequence[FeishuUserMessage] | None = None,
) -> tuple[list[FeishuUserMessage], AiCommandContextRefs]:
    context_msgs: list[FeishuUserMessage] = list(seeded_context or [])
    if not context_msgs:
        if message.thread_id:
            try:
                context_msgs = await client.list_messages(
                    container_id_type="thread",
                    container_id=message.thread_id,
                    sort_type="ByCreateTimeAsc",
                    page_size=cfg.max_context_messages,
                )
            except Exception:
                logger.warning(
                    "Failed to list Feishu thread messages for %s",
                    message.thread_id,
                )
        if not context_msgs and message.chat_id:
            try:
                desc_msgs = await client.list_messages(
                    container_id_type="chat",
                    container_id=message.chat_id,
                    sort_type="ByCreateTimeDesc",
                    page_size=cfg.max_context_messages,
                )
                context_msgs = list(reversed(desc_msgs))
            except Exception:
                logger.warning("Failed to list Feishu chat messages for %s", message.chat_id)
    if message.parent_id and all(m.message_id != message.parent_id for m in context_msgs):
        try:
            parent_msg = await client.get_message(message.parent_id)
        except Exception as exc:
            raise RuntimeError(
                "无法读取引用消息，可能遗漏图片；请重试或重新发送引用内容。"
            ) from exc
        if parent_msg is None:
            raise RuntimeError("找不到引用消息，可能遗漏图片；请重试或重新发送引用内容。")
        context_msgs.insert(0, parent_msg)

    texts = [m.text for m in context_msgs if m.text]
    if message.text and message.text not in texts:
        texts.append(message.text)
    return context_msgs, extract_context_references(texts)
