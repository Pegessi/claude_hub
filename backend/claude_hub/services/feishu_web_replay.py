"""Durable, single-namespace position checkpoints for Feishu Web recovery."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from claude_hub.services.feishu_web_session.adapter import is_in_topic
from claude_hub.services.feishu_web_session.client import PushedMessage, is_web_entity_id
from claude_hub.services.feishu_web_session.rich import CARD_BOX, CARD_CHAT, FEED_INBOX
from claude_hub.services.local_storage import read_document, write_document

logger = logging.getLogger(__name__)


def _visible_positions(
    messages: list[Any], positions: list[int], chat_id: str, *, topic: bool, require_end: bool
) -> dict[int, Any]:
    key = "topic_position" if topic else "position"
    by_position = {getattr(m, key): m for m in messages}
    if (
        len(by_position) != len(messages)
        or any(p not in positions for p in by_position)
        or any(not is_web_entity_id(m.id) or m.chat_id != chat_id for m in messages)
        or (require_end and positions[-1] not in by_position)
    ):
        raise ValueError("Feishu Web replay returned an unsettled position range")
    for pos in positions:
        if pos not in by_position:
            logger.debug("Feishu Web replay skipped invisible position %s", pos)
    return by_position


class WebPositionReplay:
    """Replay complete chat position ranges, advancing only after processing succeeds.

    A new store snapshots the initial feed. Later newly discovered chats are read
    from position zero so messages created while disconnected are not silently lost.
    """

    def __init__(self, path: Path, *, guard: Callable[[], Any] | None = None) -> None:
        self.path = path
        self._guard = guard or nullcontext

    def _load(self) -> dict[str, Any]:
        try:
            value = read_document(self.path)
        except FileNotFoundError:
            return {"version": 1, "initialized": False, "chats": {}, "topics": {}}
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("invalid Feishu Web replay state")
        if not isinstance(value.get("chats"), dict):
            raise ValueError("invalid Feishu Web chat checkpoints")
        if not isinstance(value.get("topics", {}), dict):
            raise ValueError("invalid Feishu Web topic checkpoints")
        value.setdefault("topics", {})
        return value

    def remember_topic(
        self, topic_id: str, chat_id: str, position: int, chat_type: int = 0
    ) -> bool:
        """Register an observed topic without skipping older pending positions."""
        if not topic_id or not chat_id or position < -1:
            return False
        with self._guard():
            state = self._load()
            topics = state["topics"]
            if topic_id not in topics:
                topics[topic_id] = {
                    "chat_id": chat_id,
                    "position": position,
                    "chat_type": chat_type,
                }
                write_document(self.path, state)
                return True
            return False

    def mark_handled(self, message: Any) -> None:
        """Advance only the next contiguous live position after successful intake."""
        with self._guard():
            state = self._load()
            if not state["initialized"]:
                return
            changed = False
            cid = message.chat_id
            previous = state["chats"].get(cid)
            if (
                not is_in_topic(message)
                and isinstance(previous, int)
                and message.position == previous + 1
            ):
                state["chats"][cid] = message.position
                changed = True
            tid = message.thread_id or message.root_id
            topic = state["topics"].get(tid)
            pos = message.topic_position
            if isinstance(topic, dict) and pos is not None and pos == topic["position"] + 1:
                topic["position"] = pos
                changed = True
            if changed:
                write_document(self.path, state)

    async def catch_up(
        self,
        web: Any,
        deliver: Callable[[PushedMessage], Awaitable[None]],
        watch_chat_ids: frozenset[str] = frozenset(),
    ) -> None:
        state = self._load()
        checkpoints: dict[str, int] = state["chats"]
        initial = not state["initialized"]
        chats: dict[str, Any] = {}
        parents = [(FEED_INBOX, "")]
        visited_parents: set[tuple[int, str]] = set()
        while parents:
            feed_type, parent = parents.pop()
            if (feed_type, parent) in visited_parents:
                raise ValueError("Feishu feed box cycle")
            visited_parents.add((feed_type, parent))
            cursor = 0
            seen_cursors: set[int] = set()
            while True:
                page = await web.feed_cards(
                    feed_type=feed_type, parent=parent, cursor=cursor, count=100
                )
                for card in page.cards:
                    if card.type == CARD_BOX and card.id:
                        parents.append((feed_type, card.id))
                    elif card.type == CARD_CHAT and card.id:
                        if card.id not in page.chats:
                            raise ValueError("Feishu feed omitted chat metadata")
                        chats[card.id] = page.chats[card.id]
                next_cursor = page.next_cursor
                if not next_cursor:
                    break
                if next_cursor in seen_cursors:
                    raise ValueError("Feishu feed pagination did not advance")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
        known = (
            set(checkpoints)
            | {topic["chat_id"] for topic in state["topics"].values() if topic.get("chat_id")}
            | {cid for cid in watch_chat_ids if is_web_entity_id(cid)}
        )
        missing = sorted(known - chats.keys())
        for start in range(0, len(missing), 50):
            ids = missing[start : start + 50]
            recovered = await web.chats(ids)
            if any(cid not in recovered for cid in ids):
                raise ValueError("Feishu Web replay omitted known chat metadata")
            chats.update(recovered)
        if initial:
            # Discover recent topic roots without delivering historical messages.
            for cid, chat in chats.items():
                if not chat.has_last and (not chat.thread or chat.last_position <= 0):
                    continue
                for start in range(max(0, chat.last_position - 49), chat.last_position + 1, 50):
                    positions = list(range(start, min(chat.last_position + 1, start + 50)))
                    messages = await web.messages_at(cid, positions)
                    ids = [m.id for m in messages if m.id]
                    topics = await web.topics(ids) if ids else {}
                    for tid, topic in topics.items():
                        state["topics"][tid] = {
                            "chat_id": topic.chat or cid,
                            "position": topic.last,
                            "chat_type": chat.type,
                        }
            with self._guard():
                latest = self._load()
                for tid, topic in state["topics"].items():
                    latest["topics"][tid] = topic
                latest["chats"].update(
                    {
                        cid: (
                            -1
                            if not chat.has_last and chat.last_position <= 0
                            else chat.last_position
                        )
                        for cid, chat in chats.items()
                    }
                )
                latest["initialized"] = True
                write_document(self.path, latest)
            return
        for cid, chat in chats.items():
            old = checkpoints.get(cid, -1)
            last = chat.last_position
            if not chat.has_last and last <= 0:
                if not isinstance(old, int) or old >= 0:
                    raise ValueError("Feishu Web chat position regressed")
                continue
            if not isinstance(old, int) or last < old:
                raise ValueError("Feishu Web chat position regressed")
            chunks = []
            for start in reversed(range(old + 1, last + 1, 50)):
                positions = list(range(start, min(last + 1, start + 50)))
                messages = await web.messages_at(cid, positions)
                by_position = _visible_positions(
                    messages, positions, cid, topic=False, require_end=positions[-1] == last
                )
                chunks.append((positions, messages, by_position))
            for positions, messages, by_position in reversed(chunks):
                ids = [m.id for m in messages if m.id]
                topics = await web.topics(ids) if ids else {}
                with self._guard():
                    state = self._load()
                    for tid, topic in topics.items():
                        state["topics"].setdefault(
                            tid,
                            {
                                "chat_id": topic.chat or cid,
                                "position": topic.start - 1,
                                "chat_type": chat.type,
                            },
                        )
                    write_document(self.path, state)
                for pos in positions:
                    if pos in by_position:
                        await deliver(PushedMessage(message=by_position[pos], chat_type=chat.type))
                    with self._guard():
                        state = self._load()
                        state["chats"][cid] = max(state["chats"].get(cid, -1), pos)
                        write_document(self.path, state)

    async def catch_up_topics(
        self, web: Any, deliver: Callable[[PushedMessage], Awaitable[None]]
    ) -> None:
        state = self._load()
        checkpoints: dict[str, dict[str, Any]] = state["topics"]
        topic_ids = list(checkpoints)
        for start in range(0, len(topic_ids), 50):
            ids = topic_ids[start : start + 50]
            topics = await web.topics(ids)
            if any(tid not in topics for tid in ids):
                raise ValueError("Feishu Web replay omitted topic metadata")
            for tid in ids:
                checkpoint = checkpoints[tid]
                old = checkpoint["position"]
                last = topics[tid].last
                if last == -1 and not getattr(topics[tid], "replies", 0):
                    if not isinstance(old, int) or old > int(getattr(topics[tid], "start", 0)) - 1:
                        raise ValueError("Feishu Web topic position regressed")
                    continue
                chat_id = checkpoint["chat_id"]
                chat_type = checkpoint.get("chat_type", 0)
                if not chat_type and hasattr(web, "chats"):
                    chat = (await web.chats([chat_id])).get(chat_id)
                    if chat is None:
                        raise ValueError("Feishu Web replay omitted topic chat metadata")
                    chat_type = chat.type
                if not isinstance(old, int) or last < old:
                    raise ValueError("Feishu Web topic position regressed")
                topic_start = max(old + 1, int(getattr(topics[tid], "start", 0)))
                chunks = []
                for first in reversed(range(topic_start, last + 1, 50)):
                    positions = list(range(first, min(last + 1, first + 50)))
                    messages = await web.topic_messages(tid, chat_id, positions)
                    by_position = _visible_positions(
                        messages, positions, chat_id, topic=True, require_end=positions[-1] == last
                    )
                    chunks.append((positions, by_position))
                for positions, by_position in reversed(chunks):
                    for pos in positions:
                        if pos in by_position:
                            await deliver(
                                PushedMessage(message=by_position[pos], chat_type=chat_type)
                            )
                        with self._guard():
                            state = self._load()
                            state["topics"][tid]["position"] = max(
                                state["topics"][tid]["position"], pos
                            )
                            write_document(self.path, state)
