"""Deliver a realtime diagnosis to the bound Bot and retain passive Dot context."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from typing import cast

from claude_hub.models import WorkspaceAttachmentCreate

from .agent_stream.redaction import mask_secrets
from .dot.events_gateway import (
    ExternalEvent,
    has_external_event_receipt,
    record_suppressed_external_event,
    submit_external_event,
)
from .dot.storage.store import DotStore
from .feishu_message_format import build_feishu_card_payload
from .feishu_realtime_agent import (
    RealtimeDecision,
    archive_realtime_event_sessions,
    diagnose_realtime_event,
)
from .feishu_realtime_matching import FeishuRealtimeEventConfig

logger = logging.getLogger(__name__)


@dataclass
class RealtimeDelivery:
    decision: RealtimeDecision | None = None
    notified: bool = False
    followup_due: bool = False
    initial_summary: str = ""
    message_id: str = ""
    app_id: str = ""
    event: ExternalEvent | None = None
    attachments: list[WorkspaceAttachmentCreate] = field(default_factory=list)


def _read_delivery(store: DotStore, key: str) -> RealtimeDelivery:
    store.ensure()
    with closing(sqlite3.connect(store.home / "index.sqlite.events")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "CREATE TABLE IF NOT EXISTS realtime_triage (key TEXT PRIMARY KEY, decision TEXT NOT NULL, notified INTEGER NOT NULL DEFAULT 0)"
        )
        columns = {row[1] for row in db.execute("PRAGMA table_info(realtime_triage)")}
        if "followup_due" not in columns:
            db.execute(
                "ALTER TABLE realtime_triage ADD COLUMN followup_due INTEGER NOT NULL DEFAULT 0"
            )
            db.execute(
                "UPDATE realtime_triage SET followup_due=1 "
                "WHERE notified=1 AND json_extract(decision, '$.decision')='investigate'"
            )
        for name in ("initial_summary", "message_id", "app_id", "event", "attachments"):
            if name not in columns:
                db.execute(
                    f"ALTER TABLE realtime_triage ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                )
        if "initial_summary" not in columns:
            db.execute(
                "UPDATE realtime_triage SET initial_summary=json_extract(decision, '$.summary') "
                "WHERE initial_summary='' AND json_extract(decision, '$.decision')='investigate'"
            )
        row = db.execute(
            "SELECT decision, notified, followup_due, initial_summary, message_id, app_id, event, attachments "
            "FROM realtime_triage WHERE key = ? OR EXISTS ("
            "SELECT 1 FROM json_each(CASE WHEN event='' THEN '{}' ELSE event END, "
            "'$.payload.coalesced_message_ids') WHERE value=?) ORDER BY key=? DESC LIMIT 1",
            (key, key, key),
        ).fetchone()
    if row is None:
        return RealtimeDelivery()
    return RealtimeDelivery(
        decision=RealtimeDecision.model_validate_json(row[0]) if row[0] != "null" else None,
        notified=bool(row[1]),
        followup_due=bool(row[2]),
        initial_summary=row[3],
        message_id=row[4],
        app_id=row[5],
        event=ExternalEvent.model_validate_json(row[6]) if row[6] else None,
        attachments=[
            WorkspaceAttachmentCreate.model_validate(item) for item in json.loads(row[7] or "[]")
        ],
    )


def _save_delivery(store: DotStore, key: str, state: RealtimeDelivery) -> None:
    with closing(sqlite3.connect(store.home / "index.sqlite.events")) as db, db:
        db.execute(
            "INSERT INTO realtime_triage (key, decision, notified, followup_due, initial_summary, message_id, app_id, event, attachments) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
            "decision=excluded.decision, notified=excluded.notified, followup_due=excluded.followup_due, "
            "initial_summary=excluded.initial_summary, message_id=excluded.message_id, app_id=excluded.app_id, "
            "event=excluded.event, attachments=excluded.attachments",
            (
                key,
                state.decision.model_dump_json() if state.decision else "null",
                int(state.notified),
                int(state.followup_due),
                state.initial_summary,
                state.message_id,
                state.app_id,
                state.event.model_dump_json() if state.event else "",
                json.dumps([item.model_dump(mode="json") for item in state.attachments]),
            ),
        )


def _forget_delivery(store: DotStore, key: str) -> None:
    with closing(sqlite3.connect(store.home / "index.sqlite.events")) as db, db:
        db.execute("DELETE FROM realtime_triage WHERE key = ?", (key,))


async def realtime_message_state(key: str) -> tuple[RealtimeDelivery, bool]:
    """Read owned batch membership before terminal receipts can suppress a member."""
    from .dot import get_dot_service

    dot = get_dot_service()
    state = await asyncio.to_thread(_read_delivery, dot.store, key)
    terminal = (
        state.event is None
        and state.decision is None
        and not state.followup_due
        and await has_external_event_receipt("feishu", key, dot)
    )
    return state, terminal


async def has_pending_realtime_followup(key: str) -> bool:
    """Let replay finish a promised follow-up even after a source-chat reply."""
    from .dot import get_dot_service

    state = await asyncio.to_thread(_read_delivery, get_dot_service().store, key)
    return state.followup_due


def source_context(event: ExternalEvent) -> str:
    labels = []
    for kind in ("chat", "sender"):
        name = " ".join(str(event.payload.get(f"{kind}_name") or "").split())
        identity = str(event.payload.get(f"{kind}_id") or "").strip()
        label = (
            f"{name} ({identity})" if name and identity and name != identity else name or identity
        )
        if label:
            labels.append(label)
    return " · ".join(labels)


def realtime_card(event: ExternalEvent, state: RealtimeDelivery) -> dict[str, object]:
    assert state.decision is not None
    item = state.decision
    parts = [f"**Summary**\n{state.initial_summary or item.summary}"]
    if state.initial_summary:
        conclusion = "正在核查…" if item.decision == "investigate" else item.summary
        parts.append(f"**Investigation**\n{conclusion}")
    if item.reply_draft:
        parts.append(f"**Suggested reply**\n{item.reply_draft}")
    if event.url:
        parts.append(f"[Open source]({event.url})")
    body = mask_secrets("\n\n".join(parts))
    payload = build_feishu_card_payload(mask_secrets(event.title), body)
    # Keep Source genuinely plaintext: names must never become mentions or links.
    source = {
        "tag": "div",
        "text": {
            "tag": "plain_text",
            "content": mask_secrets(
                f"Source: {source_context(event) or event.title}\nMessage: {event.dedupe_key}"
            ),
        },
    }
    if payload.msg_type == "interactive":
        card = dict(payload.content)
        elements = list(cast(dict[str, list[object]], card["body"])["elements"])
    else:
        card = {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": mask_secrets(event.title)}},
        }
        elements = [{"tag": "div", "text": {"tag": "plain_text", "content": body}}]
    card["body"] = {"elements": [source, *elements]}
    card["config"] = {"update_multi": True}
    return card


async def deliver_realtime_triage(
    event: ExternalEvent,
    config: FeishuRealtimeEventConfig,
    *,
    still_unanswered: Callable[[], bool] = lambda: True,
    attachments: list[WorkspaceAttachmentCreate] | None = None,
) -> bool:
    from .dot import get_dot_service

    dot = get_dot_service()
    state = await asyncio.to_thread(_read_delivery, dot.store, event.dedupe_key)
    # A legacy suppression receipt must not erase the outstanding obligation.
    if (
        state.event is None
        and state.decision is None
        and await has_external_event_receipt(event.source, event.dedupe_key, dot)
    ):
        return True
    if state.event is None:
        state.event = event
        state.attachments = attachments or []
    else:
        event = state.event
        attachments = state.attachments

    async def save() -> None:
        await asyncio.to_thread(_save_delivery, dot.store, event.dedupe_key, state)

    async def receipt_members(*, include_primary: bool) -> None:
        # Keep the batch row until all constituent receipts are durable: a crash
        # between primary submission and member receipt must not create new turns.
        raw_members = event.payload.get("coalesced_message_ids")
        members = (
            {key for key in raw_members if isinstance(key, str)}
            if isinstance(raw_members, list)
            else set()
        )
        members.add(event.dedupe_key)
        for key in members:
            if include_primary or key != event.dedupe_key:
                await record_suppressed_external_event(event.source, str(key), dot)

    async def send_card(*, backfill: bool = False) -> bool:
        from claude_hub.api.feishu_bot import push_shared_card_to_tab

        tab_id = dot.tab_id()
        if not tab_id:
            return False
        sent, message_id, app_id = await push_shared_card_to_tab(
            tab_id,
            realtime_card(event, state),
            message_id=state.message_id if backfill else "",
            app_id=state.app_id if backfill else "",
        )
        if sent and not backfill:
            state.message_id, state.app_id = message_id, app_id
            await save()
        return sent

    async def initial(item: RealtimeDecision) -> bool:
        if (
            state.decision is not None
            and state.decision.decision == "investigate"
            and state.notified
        ):
            return FeishuRealtimeEventConfig.from_runtime().enabled
        state.decision, state.notified = item, False
        state.initial_summary = item.summary
        await save()
        if not still_unanswered() or not FeishuRealtimeEventConfig.from_runtime().enabled:
            return False
        # Reserve before sending; an ambiguous process exit must not duplicate Beat 1.
        state.followup_due = state.notified = True
        await save()
        if not await send_card():
            state.followup_due = state.notified = False
            await save()
            return False
        return True

    if state.decision is None or state.decision.decision == "investigate":
        try:
            result = await diagnose_realtime_event(
                event,
                config,
                on_initial=initial,
                resume_investigation=state.followup_due,
                attachments=attachments,
            )
        except Exception:
            if not state.followup_due:
                raise
            # Cancellation deliberately escapes, leaving followup_due for replay.
            logger.warning(
                "Realtime investigation could not finish for %s", event.dedupe_key, exc_info=True
            )
            result = RealtimeDecision(
                decision="notify",
                summary=(
                    "Investigation could not be completed (调查未能完成). "
                    "No verified conclusion is available; please check the source manually. "
                    "尚无可验证结论，请人工核对原消息。"
                ),
            )
        if result.decision == "investigate":
            if not state.followup_due and not still_unanswered():
                await archive_realtime_event_sessions(event)
                await receipt_members(include_primary=True)
                await asyncio.to_thread(_forget_delivery, dot.store, event.dedupe_key)
                return True
            return False
        state.decision, state.notified = result, False
        if not state.followup_due:
            state.initial_summary = ""
        await save()
    if not state.followup_due and (
        state.decision.decision == "skip" or (not state.notified and not still_unanswered())
    ):
        await archive_realtime_event_sessions(event)
        await receipt_members(include_primary=True)
        await asyncio.to_thread(_forget_delivery, dot.store, event.dedupe_key)
        return True
    if not FeishuRealtimeEventConfig.from_runtime().enabled:
        return False
    if not state.notified:
        state.notified = True
        await save()
        if not await send_card(backfill=state.followup_due):
            state.notified = False
            await save()
            return False
    # Cleanup retries never rerun diagnosis or erase a completed conclusion.
    await archive_realtime_event_sessions(event)
    passive = event.model_copy(
        update={
            "event_type": "triage." + event.event_type,
            "severity": "normal",
            "summary": state.decision.summary[:1000],
            "payload": {
                **event.payload,
                "triage_summary": state.decision.summary,
                "reply_draft": state.decision.reply_draft,
            },
        }
    )
    accepted = await submit_external_event(passive, dot) or await has_external_event_receipt(
        event.source, event.dedupe_key, dot
    )
    if accepted:
        await receipt_members(include_primary=False)
        await asyncio.to_thread(_forget_delivery, dot.store, event.dedupe_key)
    return accepted
