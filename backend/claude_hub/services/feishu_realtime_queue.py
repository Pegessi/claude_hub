"""Quiet-window selection for the journal-backed realtime poller queue."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .feishu_realtime_delivery import realtime_message_state
from .feishu_realtime_events import FeishuRealtimeEventRouter
from .feishu_user_client import FeishuUserMessage
from .feishu_web_journal import WebPushJournal
from .feishu_web_replay import WebPositionReplay


@dataclass
class RealtimeQueued:
    pushed: Any
    user_msg: FeishuUserMessage
    transport: Any
    client: Any
    owner_ids: set[str]
    is_owner: bool
    journal: WebPushJournal
    generation: int | None
    replay: WebPositionReplay
    received_at: float
    p2p_key: str


async def next_realtime_batch(
    queue: OrderedDict[str, RealtimeQueued],
    changed: asyncio.Event,
    router: FeishuRealtimeEventRouter,
    *,
    now: Callable[[], float],
) -> list[RealtimeQueued]:
    while queue:
        changed.clear()
        cfg = router.resolve_config()
        remaining = cfg.p2p_debounce_seconds
        queued = list(queue.items())
        owners: dict[str, str] = {}
        terminal: set[str] = set()
        for mid, item in queued:
            state, receipted = await realtime_message_state(mid)
            if state.event is not None or state.decision is not None or state.followup_due:
                owners[mid] = state.event.dedupe_key if state.event else mid
            elif receipted or router.is_message_handled(mid):
                terminal.add(mid)
        if changed.is_set():
            continue
        for mid, item in queued:
            batch = [(mid, item)]
            if mid in owners:
                # A frozen batch is resumed as-is, even when only one member has
                # arrived from replay. Delivery restores its persisted evidence.
                batch = [(key, row) for key, row in queued if owners.get(key) == owners[mid]]
            elif item.p2p_key and mid not in terminal and cfg.p2p_debounce_seconds > 0:
                batch = [
                    (key, row)
                    for key, row in queued
                    if row.p2p_key == item.p2p_key and key not in owners and key not in terminal
                ]
                wait = max(row.received_at for _, row in batch) + cfg.p2p_debounce_seconds - now()
                replied = router.has_owner_replied(
                    chat_id=item.user_msg.chat_id,
                    message_id=item.user_msg.message_id,
                    created_at_ms=item.user_msg.create_time_ms,
                    web_transport=item.transport,
                    owner_ids=item.owner_ids,
                )
                active = router.owner_active_in_p2p(
                    item.user_msg.chat_id,
                    cfg.p2p_active_seconds,
                    web_transport=item.transport,
                    owner_ids=item.owner_ids,
                )
                if wait > 0 and not replied and not active:
                    remaining = min(remaining, wait)
                    continue
            for key, _ in batch:
                queue.pop(key, None)
            return [
                row
                for _, row in sorted(
                    batch,
                    key=lambda pair: (
                        pair[1].user_msg.create_time_ms,
                        pair[1].pushed.message.position,
                    ),
                )
            ]
        try:
            await asyncio.wait_for(changed.wait(), max(0.001, remaining))
        except TimeoutError:
            pass
    return []
