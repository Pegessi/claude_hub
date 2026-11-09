"""Bounded record of the messages this process sent through each Feishu Bot.

A group topic becomes a conversation the moment the Bot answers inside it: the
next messages in that thread usually do not repeat the @mention, yet they are
still addressed to the Bot. Recognising our own message ids is what lets that
happen without calling Feishu's thread-history API on every inbound message.

Memory is bounded per Bot and the oldest ids are forgotten first, exactly like
the transport's dedupe is bounded. A forgotten id only costs one @mention, never
a wrong reply.
"""

from __future__ import annotations

from collections import OrderedDict

_MAX_PER_BOT = 500


class FeishuSentMessageRegistry:
    def __init__(self, per_bot_limit: int = _MAX_PER_BOT) -> None:
        self._limit = per_bot_limit
        self._sent: OrderedDict[str, OrderedDict[str, None]] = OrderedDict()
        self._threads: OrderedDict[str, OrderedDict[str, None]] = OrderedDict()

    def record(self, bot_id: str, message_id: str | None) -> None:
        """Remember one message id this Bot sent. Empty ids are ignored."""

        if not bot_id or not message_id:
            return
        bucket = self._sent.get(bot_id)
        if bucket is None:
            bucket = OrderedDict()
            self._sent[bot_id] = bucket
        bucket[message_id] = None
        bucket.move_to_end(message_id)
        while len(bucket) > self._limit:
            bucket.popitem(last=False)

    def record_thread(self, bot_id: str, thread_id: str | None) -> None:
        """Remember a topic this Bot answered in.

        In a topic-mode group every message belongs to a topic, and a follow-up
        there usually carries the human's root id rather than one of our
        messages, so the thread itself is the ownership proof.
        """

        if not bot_id or not thread_id:
            return
        bucket = self._threads.get(bot_id)
        if bucket is None:
            bucket = OrderedDict()
            self._threads[bot_id] = bucket
        bucket[thread_id] = None
        bucket.move_to_end(thread_id)
        while len(bucket) > self._limit:
            bucket.popitem(last=False)

    def owns(self, bot_id: str, message_id: str | None) -> bool:
        if not bot_id or not message_id:
            return False
        bucket = self._sent.get(bot_id)
        return bucket is not None and message_id in bucket

    def owns_thread(self, bot_id: str, thread_id: str | None) -> bool:
        if not bot_id or not thread_id:
            return False
        bucket = self._threads.get(bot_id)
        return bucket is not None and thread_id in bucket

    def clear(self, bot_id: str | None = None) -> None:
        if bot_id is None:
            self._sent.clear()
            self._threads.clear()
            return
        self._sent.pop(bot_id, None)
        self._threads.pop(bot_id, None)


sent_messages = FeishuSentMessageRegistry()
