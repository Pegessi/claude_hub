"""Deliver a realtime diagnosis to the bound Bot and retain passive Dot context."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from contextlib import closing

from claude_hub.models import WorkspaceAttachmentCreate

from .dot.events_gateway import (
    ExternalEvent,
    has_external_event_receipt,
    record_suppressed_external_event,
    submit_external_event,
)
from .dot.storage.store import DotStore
from .feishu_realtime_agent import RealtimeDecision, diagnose_realtime_event
from .feishu_realtime_matching import FeishuRealtimeEventConfig


def _read_delivery(store: DotStore, key: str) -> tuple[RealtimeDecision | None, bool]:
    store.ensure()
    with closing(sqlite3.connect(store.home / "index.sqlite.events")) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS realtime_triage (key TEXT PRIMARY KEY, decision TEXT NOT NULL, notified INTEGER NOT NULL DEFAULT 0)"
        )
        row = db.execute(
            "SELECT decision, notified FROM realtime_triage WHERE key = ?", (key,)
        ).fetchone()
    return (RealtimeDecision.model_validate_json(row[0]), bool(row[1])) if row else (None, False)


def _save_delivery(store: DotStore, key: str, decision: RealtimeDecision, notified: bool) -> None:
    with closing(sqlite3.connect(store.home / "index.sqlite.events")) as db, db:
        db.execute(
            "INSERT INTO realtime_triage (key, decision, notified) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET decision=excluded.decision, notified=excluded.notified",
            (key, decision.model_dump_json(), int(notified)),
        )


def _forget_delivery(store: DotStore, key: str) -> None:
    with closing(sqlite3.connect(store.home / "index.sqlite.events")) as db, db:
        db.execute("DELETE FROM realtime_triage WHERE key = ?", (key,))


async def deliver_realtime_triage(
    event: ExternalEvent,
    config: FeishuRealtimeEventConfig,
    *,
    still_unanswered: Callable[[], bool] = lambda: True,
    attachments: list[WorkspaceAttachmentCreate] | None = None,
) -> bool:
    from .dot import get_dot_service

    dot = get_dot_service()
    if await has_external_event_receipt(event.source, event.dedupe_key, dot):
        return True
    decision, notified = await asyncio.to_thread(_read_delivery, dot.store, event.dedupe_key)

    async def send_card(item: RealtimeDecision) -> bool:
        payload = event.payload
        context = " · ".join(
            str(payload.get(k) or "") for k in ("chat_name", "chat_id", "sender_id")
        )
        parts = [f"**Source**: {event.title}\n{context}", f"**Summary**\n{item.summary}"]
        if item.reply_draft:
            parts.append(f"**Suggested reply**\n{item.reply_draft}")
        if event.url:
            parts.append(f"[Open source]({event.url})")
        return await dot.notify("\n\n".join(parts), title=event.title) in {"sent", "repeat"}

    async def initial(item: RealtimeDecision) -> bool:
        nonlocal decision, notified
        # An existing reservation represents an already attempted first card, even
        # if the process died before receiving the remote send result.
        if decision is not None and decision.decision == "investigate" and notified:
            return still_unanswered() and FeishuRealtimeEventConfig.from_runtime().enabled
        decision, notified = item, False
        await asyncio.to_thread(_save_delivery, dot.store, event.dedupe_key, item, False)
        if not still_unanswered() or not FeishuRealtimeEventConfig.from_runtime().enabled:
            return False
        await asyncio.to_thread(_save_delivery, dot.store, event.dedupe_key, item, True)
        notified = True
        if not await send_card(item):
            await asyncio.to_thread(_save_delivery, dot.store, event.dedupe_key, item, False)
            notified = False
            return False
        return True

    if decision is None or decision.decision == "investigate":
        result = await diagnose_realtime_event(
            event,
            config,
            on_initial=initial,
            resume_investigation=decision is not None and notified,
            attachments=attachments,
        )
        if result.decision == "investigate":
            if not still_unanswered():
                await record_suppressed_external_event(event.source, event.dedupe_key, dot)
                await asyncio.to_thread(_forget_delivery, dot.store, event.dedupe_key)
                return True
            return False
        decision, notified = result, False
        await asyncio.to_thread(_save_delivery, dot.store, event.dedupe_key, decision, False)
    if decision.decision == "skip" or (not notified and not still_unanswered()):
        await record_suppressed_external_event(event.source, event.dedupe_key, dot)
        await asyncio.to_thread(_forget_delivery, dot.store, event.dedupe_key)
        return True
    if not FeishuRealtimeEventConfig.from_runtime().enabled:
        return False
    if not notified:
        # Reserve before the network call: a crash with an ambiguous result must not
        # replay the card. Explicit send failure releases the reservation for retry.
        await asyncio.to_thread(_save_delivery, dot.store, event.dedupe_key, decision, True)
        if not await send_card(decision):
            await asyncio.to_thread(_save_delivery, dot.store, event.dedupe_key, decision, False)
            return False
    passive = event.model_copy(
        update={
            "event_type": "triage." + event.event_type,
            "severity": "normal",
            "summary": decision.summary[:1000],
            "payload": {
                **event.payload,
                "triage_summary": decision.summary,
                "reply_draft": decision.reply_draft,
            },
        }
    )
    accepted = await submit_external_event(passive, dot) or await has_external_event_receipt(
        event.source, event.dedupe_key, dot
    )
    if accepted:
        await asyncio.to_thread(_forget_delivery, dot.store, event.dedupe_key)
    return accepted
