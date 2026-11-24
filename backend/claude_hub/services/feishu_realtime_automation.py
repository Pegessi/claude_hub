"""Detect automated messages before routing personal Feishu events to Dot."""

import asyncio
from collections.abc import Mapping

from .feishu_ai_command_models import DEFAULT_AI_COMMAND_FOOTER
from .feishu_bot_pool import FeishuBotPoolStateError, FeishuBotPoolStore
from .feishu_web_session.rich import CHATTER_USER, WebChatter

BOT_SENDER_PREFIXES = ("cli_", "app_", "bot_")
_AI_FOOTER_TEXT = DEFAULT_AI_COMMAND_FOOTER.removeprefix("> ")


def has_ai_reply_footer(text: str) -> bool:
    return _AI_FOOTER_TEXT in text


def is_pool_bot_message(sender_keys: set[str], chat_keys: set[str], *, is_p2p: bool) -> bool:
    try:
        bots = FeishuBotPoolStore().snapshot(sync_environment=False).bots
    except (FeishuBotPoolStateError, OSError):
        return False
    return any(
        (bot.feishu_open_id or "").casefold() in sender_keys
        or bot.app_id.casefold() in sender_keys
        or (
            is_p2p
            and any(
                binding.chat_type == "p2p" and binding.chat_id.casefold() in chat_keys
                for binding in bot.bindings
            )
        )
        for bot in bots
    )


async def _web_sender_entity(web_transport: object, sender_id: str) -> WebChatter | None:
    if web_transport is None or not sender_id.isdecimal():
        return None
    cache = getattr(web_transport, "_chatter_cache", {})
    cached = cache.get(sender_id) if isinstance(cache, Mapping) else None
    if isinstance(cached, WebChatter) and cached.type:
        return cached
    client = getattr(web_transport, "_web", None)
    if client is None or not hasattr(client, "chatters"):
        return None
    try:
        async with asyncio.timeout(2.5):
            result = await client.chatters([sender_id])
    except Exception:
        return None
    entity = result.get(sender_id) if isinstance(result, Mapping) else None
    if isinstance(entity, WebChatter) and entity.id == sender_id and entity.type:
        remember = getattr(web_transport, "_remember_chatter", None)
        if remember is not None:
            remember(entity)
        return entity
    return None


async def is_automated_web_sender(web_transport: object, sender_id: str, *, is_p2p: bool) -> bool:
    entity = await _web_sender_entity(web_transport, sender_id)
    if entity is not None:
        return entity.type != CHATTER_USER
    # A numeric Web ID alone does not establish that a DM is from a person.
    return is_p2p and sender_id.isdecimal()
