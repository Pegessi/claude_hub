"""Bounded reaction operations shared by /ai execution and recovery."""

import asyncio
import logging

from claude_hub.services.feishu_ai_command_models import _REACTION_TIMEOUT_SECONDS
from claude_hub.services.feishu_user_client import FeishuUserClient

logger = logging.getLogger(__name__)


async def add_reaction(client: FeishuUserClient, message_id: str, emoji: str) -> str | None:
    try:
        async with asyncio.timeout(_REACTION_TIMEOUT_SECONDS):
            return await client.add_reaction_as_user(message_id, emoji)
    except Exception:
        logger.warning("Failed to add %s reaction on %s", emoji, message_id)
        return None


async def remove_ack_reaction(client: FeishuUserClient, message_id: str, reaction_id: str) -> bool:
    try:
        async with asyncio.timeout(_REACTION_TIMEOUT_SECONDS):
            await client.delete_reaction_as_user(message_id, reaction_id)
        return True
    except Exception:
        logger.debug("Could not remove ack reaction %s on %s", reaction_id, message_id)
        return False
