"""Feishu-only bounded dispatch queue value types."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field

from claude_hub.services.feishu_bot import (
    FeishuBotClient,
    FeishuMessageEvent,
    FeishuMessageTimeStatus,
)
from claude_hub.services.feishu_bot_pool import BotBinding, EffectiveBot


class TabDispatchQueueFull(RuntimeError):
    """The bounded Feishu-only queue cannot retain another message."""


@dataclass
class TabDispatchItem:
    bot_id: str
    event: FeishuMessageEvent
    effective: EffectiveBot
    binding: BotBinding
    client: FeishuBotClient
    arrived_at: float
    start: asyncio.Future[None]
    outcome: asyncio.Future[str]
    reaction_dispatch_started: asyncio.Event = field(default_factory=asyncio.Event)
    reaction_finished: asyncio.Event = field(default_factory=asyncio.Event)
    reaction_task: asyncio.Task[None] | None = None
    queued_reaction_id: str | None = None
    processing_reaction_id: str | None = None
    dispatch_started: bool = False
    native_bridge_started: bool = False
    admission_time_rejection: FeishuMessageTimeStatus | None = None
    cancelled: bool = False
    # Stopped by a newer message (Dot Chat): answered together with it, no reply.
    superseded: bool = False


@dataclass
class TabDispatchQueue:
    pending: list[TabDispatchItem]
    active: list[TabDispatchItem]
    processor: TabDispatchItem | None
    changed: asyncio.Event
    dispatched_contexts: OrderedDict[tuple[str, str, str, str | None], None] = field(
        default_factory=OrderedDict
    )
    # What the active batch sent, and what a stopped batch sent: the next batch
    # answers that again together with the newer messages.
    active_text: str = ""
    interrupted_text: str = ""

    @property
    def waiters(self) -> list[asyncio.Future[None]]:
        """Return compatibility view used by queue lifecycle tests."""
        return [item.start for item in self.pending if item is not self.processor]
