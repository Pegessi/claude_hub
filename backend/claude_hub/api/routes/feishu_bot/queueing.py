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

# Bot + channel/topic identity, fenced by the resolved configuration version.
DispatchChannelKey = tuple[str, str, str, str, str | None, int, int]


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
    # The stop asked for by that newer message; its result says whether it took.
    stop_task: asyncio.Task[bool] | None = None

    @property
    def channel_key(self) -> DispatchChannelKey:
        # Interruption deliberately excludes the sender: another person in the
        # same topic may supersede an answer, but must never coalesce with it.
        # Pairing/version fences also keep stale work out of a rebound channel.
        # Legacy group fallback returns a DM binding: only the event identifies
        # the actual room, so two groups must not share that binding's chat ID.
        return (
            self.bot_id,
            self.binding.pairing_id,
            self.event.chat_type,
            self.event.chat_id,
            self.event.thread_id,
            self.effective.generation,
            self.effective.revision,
        )


#: Identity of one sender inside one channel: what a follow-up is compared against.
DispatchContextKey = tuple[str, str, str, "str | None", str]


@dataclass
class TabDispatchQueue:
    pending: list[TabDispatchItem]
    active: list[TabDispatchItem]
    processor: TabDispatchItem | None
    changed: asyncio.Event
    dispatched_contexts: OrderedDict[tuple[str, str, str, str | None, str], None] = field(
        default_factory=OrderedDict
    )
    # A stopped batch's text belongs to its channel, not whichever channel is
    # next in the FIFO. Multiple channels can have a stopped answer to resume.
    active_text: str = ""
    interrupted_text: OrderedDict[DispatchChannelKey, str] = field(default_factory=OrderedDict)

    @property
    def waiters(self) -> list[asyncio.Future[None]]:
        """Return compatibility view used by queue lifecycle tests."""
        return [item.start for item in self.pending if item is not self.processor]
