"""Deliver a realtime diagnosis to the bound Bot and retain passive Dot context."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from contextlib import closing

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
) -> bool:
    from .dot import get_dot_service

    dot = get_dot_service()
    if await has_external_event_receipt(event.source, event.dedupe_key, dot):
        return True
    decision, notified = await asyncio.to_thread(_read_delivery, dot.store, event.dedupe_key)
    if decision is None:
        decision = await diagnose_realtime_event(event, config)
        await asyncio.to_thread(_save_delivery, dot.store, event.dedupe_key, decision, False)
    if decision.decision == "skip" or (not notified and not still_unanswered()):
        await record_suppressed_external_event(event.source, event.dedupe_key, dot)
        await asyncio.to_thread(_forget_delivery, dot.store, event.dedupe_key)
        return True
    # Disabling intake while diagnosis runs must also suppress outbound delivery.
    if not FeishuRealtimeEventConfig.from_runtime().enabled:
        return False
    payload = event.payload
    context = " · ".join(str(payload.get(k) or "") for k in ("chat_name", "chat_id", "sender_id"))
    parts = [f"**Source**: {event.title}\n{context}", f"**Summary**\n{decision.summary}"]
    if decision.reply_draft:
        parts.append(f"**Suggested reply**\n{decision.reply_draft}")
    if event.url:
        parts.append(f"[Open source]({event.url})")
    if not notified:
        if await dot.notify("\n\n".join(parts), title=event.title) not in {"sent", "repeat"}:
            return False
        await asyncio.to_thread(_save_delivery, dot.store, event.dedupe_key, decision, True)
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
