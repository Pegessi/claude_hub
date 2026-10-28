"""Authenticated card callbacks and durable question mirror projection.

Only this POST surface is cookie-free. Possession of a card value alone never
identifies an authorized operator. No callback value is executed as a command.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
import uuid
from collections import deque
from datetime import timedelta
from typing import Any

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from fastapi import APIRouter, HTTPException, Request

from ....models import User
from ....models.chat_message import ChatQuestion, QuestionAnswerCreate, QuestionMirror, utc_now
from ....services.agent_stream.question_answers import answers
from ....services.chat_message_inbox import InboxError
from ....services.feishu_bot import FeishuBotClient, FeishuMessageEvent
from ....services.feishu_bot_pool import FeishuBotPoolError
from ....services.feishu_message_format import build_feishu_question_card_payload

_rates: dict[str, deque[float]] = {}
_mirror_locks: dict[str, asyncio.Lock] = {}


def current_binding(q: ChatQuestion) -> tuple[Any, Any]:
    from ... import feishu_bot as bot

    mirror = q.mirror
    if mirror is None:
        raise InboxError(409, "Question has no Feishu mirror")
    try:
        effective = bot._pool.effective(mirror.bot_id)
        entry = bot._pool.snapshot().get(mirror.bot_id)
        binding = entry.binding if entry else None
        if (
            not effective.callback_enabled
            or not binding
            or not bot._owner_is_authorized(binding.owner)
            or not bot._binding_still_routes(effective, binding)
            or (
                effective.app_id,
                effective.generation,
                effective.revision,
                effective.expected_tenant_key,
                binding.pairing_id,
                binding.chat_id,
                binding.sender_open_id,
                binding.tab_id,
            )
            != (
                mirror.app_id,
                mirror.generation,
                mirror.revision,
                mirror.tenant_key,
                mirror.pairing_id,
                mirror.chat_id,
                mirror.sender_open_id,
                q.tab_id,
            )
        ):
            raise InboxError(409, "Question binding changed")
        return effective, binding
    except FeishuBotPoolError as exc:
        raise InboxError(409, "Question binding unavailable") from exc


async def mirror_question(q: ChatQuestion, *, remirror: bool = False) -> bool:
    async with _mirror_locks.setdefault(q.question_ref, asyncio.Lock()):
        return await _mirror_question(q, remirror=remirror)


async def _mirror_question(q: ChatQuestion, *, remirror: bool = False) -> bool:
    from ... import feishu_bot as bot

    inbox = answers.inbox()
    if q.mirror is None or remirror:
        if not remirror and q.mirror_checked:
            return False
        q.mirror_checked = True
        inbox._save()
        if q.state != "open":
            return False
        answers.live(q)
        entry = bot._bot_bound_to_tab(q.tab_id)
        if not entry or not entry.binding:
            return False
        effective = bot._pool.effective(entry.bot_id)
        if not effective.callback_enabled or not effective.expected_tenant_key:
            return False
        binding = entry.binding
        if not bot._owner_is_authorized(binding.owner):
            return False
        q.mirror = QuestionMirror(
            bot_id=entry.bot_id,
            app_id=effective.app_id,
            tenant_key=effective.expected_tenant_key,
            pairing_id=binding.pairing_id,
            generation=effective.generation,
            revision=effective.revision,
            chat_id=binding.chat_id,
            sender_open_id=binding.sender_open_id,
            send_uuid=str(uuid.uuid4()),
        )
        if remirror:
            q.expires_at = utc_now() + timedelta(hours=24)
        inbox._save()
    mirror = q.mirror
    try:
        effective, _ = current_binding(q)
    except InboxError:
        mirror.state = "disabled"
        mirror.action_digests.clear()
        inbox._save()
        return False
    remote_state = "expired" if q.state == "open" and q.expires_at <= utc_now() else q.state
    if mirror.retry_at is not None and mirror.retry_at > utc_now():
        return mirror.state == "sent"
    if mirror.state in {"queued", "sent"} and (
        mirror.state == "queued"
        or mirror.displayed_state != remote_state
        or q.reply_state in {"pending", "sending"}
    ):
        mirror.retry_at = utc_now() + timedelta(
            seconds=min(3600, 3 * 2 ** min(mirror.attempts, 10))
        )
        mirror.attempts += 1
        inbox._save()
    if mirror.state == "queued":
        if remote_state != "open":
            mirror.state = "disabled"
            inbox._save()
            return False
        values = [(label, secrets.token_urlsafe(32)) for label in q.options]
        other = secrets.token_urlsafe(32)
        mirror.action_digests = {
            hashlib.sha256(token.encode()).hexdigest(): {
                "operation": "question_answer",
                "selected": label,
            }
            for label, token in values
        }
        mirror.action_digests[hashlib.sha256(other.encode()).hexdigest()] = {
            "operation": "question_text_hint"
        }
        card = build_feishu_question_card_payload(
            q.prompt, values, question_ref=q.question_ref, other_token=other
        )
        client = FeishuBotClient(effective.config)
        # Token acquisition outside gate; recheck exact binding before side effect.
        await client.get_tenant_token()
        async with bot._gate(effective.bot_id):
            current_binding(q)
            answers.live(q)
            if q.state != "open":
                return False
            mirror.state = "sending"
            inbox._save()
            try:
                async with asyncio.timeout(15):
                    message_id = await client.send_message_with_identity(
                        mirror.chat_id, card.msg_type, card.content, uuid=mirror.send_uuid
                    )
            except Exception:
                mirror.state = "uncertain"
                inbox._save()
                return False
            mirror.message_id, mirror.state = message_id, "sent"
            mirror.displayed_state = "open"
            mirror.retry_at, mirror.attempts = None, 0
            inbox._save()
    elif mirror.state == "sending":
        # Persisted send fence after restart is ambiguous; no blind duplicate.
        mirror.state = "uncertain"
        inbox._save()
    if mirror.state == "sent" and mirror.displayed_state != remote_state:
        item = inbox.messages.get(q.answer_id or "")
        answer = None
        if item and item.message:
            answer = json.loads(item.message)["answers"][0]["selected"][0]
        card = build_feishu_question_card_payload(
            q.prompt,
            [],
            question_ref=q.question_ref,
            other_token="",
            state=remote_state,
            answer=answer,
        )
        client = FeishuBotClient(effective.config)
        await client.get_tenant_token()
        async with bot._gate(effective.bot_id):
            current_binding(q)
            async with asyncio.timeout(15):
                await client.patch_message_card(str(mirror.message_id), card.content)
            mirror.displayed_state = remote_state
            mirror.retry_at, mirror.attempts = None, 0
            inbox._save()
    if q.reply_state == "sending":
        q.reply_state = "uncertain"
        inbox._save()
    elif q.reply_state == "pending" and mirror.state == "sent":
        client = FeishuBotClient(effective.config)
        await client.get_tenant_token()
        async with bot._gate(effective.bot_id):
            current_binding(q)
            q.reply_state = "sending"
            inbox._save()
            try:
                async with asyncio.timeout(15):
                    await client.send_message_with_identity(
                        mirror.chat_id,
                        "text",
                        {"text": q.reply_text[:20000]},
                        uuid=str(
                            uuid.uuid5(uuid.NAMESPACE_URL, f"question-reply:{q.question_ref}")
                        ),
                    )
            except Exception:
                q.reply_state = "uncertain"
            else:
                q.reply_state, q.reply_text = "sent", ""
            inbox._save()
    return mirror.state == "sent"


def _decode(raw: bytes, headers: Any, effective: Any) -> dict[str, Any]:
    import base64

    key, token = effective.encrypt_key, effective.verification_token
    if not effective.callback_enabled or not key or not token:
        raise HTTPException(403, "Card callbacks disabled")
    try:
        signature = headers.get("x-lark-signature")
        if signature:
            stamp, nonce = headers.get("x-lark-request-timestamp", ""), headers.get(
                "x-lark-request-nonce", ""
            )
            if (
                not stamp.isdecimal()
                or len(stamp) > 16
                or not 1 <= len(nonce) <= 256
                or abs(time.time() - int(stamp)) > 300
            ):
                raise ValueError
            expected = hashlib.sha256((stamp + nonce + key).encode() + raw).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
        envelope = json.loads(raw)
        encrypted = base64.b64decode(envelope["encrypt"], validate=True)
        if len(encrypted) < 32 or len(encrypted) % 16:
            raise ValueError
        cipher = AES.new(hashlib.sha256(key.encode()).digest(), AES.MODE_CBC, encrypted[:16])
        payload = json.loads(unpad(cipher.decrypt(encrypted[16:]), 16))
        if not isinstance(payload, dict):
            raise ValueError
        challenge = payload.get("type") == "url_verification"
        if not challenge and not signature:
            raise ValueError
        actual = payload.get("token") if challenge else payload.get("header", {}).get("token")
        if not isinstance(actual, str) or not hmac.compare_digest(actual, token):
            raise ValueError
        return payload
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(403, "Invalid card callback authentication") from None


def _toast(text: str, kind: str = "info") -> dict[str, Any]:
    return {"toast": {"type": kind, "content": text}}


async def handle_action(bot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    header, event = payload.get("header"), payload.get("event")
    if not isinstance(header, dict) or not isinstance(event, dict):
        raise HTTPException(400, "Invalid callback schema")
    if payload.get("schema") != "2.0" or header.get("event_type") != "card.action.trigger":
        raise HTTPException(400, "Unsupported callback type")
    event_id = header.get("event_id")
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 256:
        raise HTTPException(400, "Invalid callback identity")
    inbox = answers.inbox()
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    event_key = f"{bot_id}:{event_id}"
    prior = inbox.callback_receipts.get(event_key)
    if prior:
        if prior["fingerprint"] != fingerprint:
            raise HTTPException(409, "Callback event payload changed")
        return _toast("Answer already received")
    context, operator, action = event.get("context"), event.get("operator"), event.get("action")
    if (
        not isinstance(context, dict)
        or not isinstance(operator, dict)
        or not isinstance(action, dict)
    ):
        raise HTTPException(400, "Invalid card context")
    value = action.get("value")
    if (
        not isinstance(value, dict)
        or type(value.get("v")) is not int
        or value.get("v") != 1
        or value.get("hub_action") not in {"question_answer", "question_text_hint"}
    ):
        raise HTTPException(400, "Unsupported card operation")
    token = value.get("hub_token")
    if not isinstance(token, str) or not 16 <= len(token) <= 128:
        raise HTTPException(403, "Invalid card action")
    digest = hashlib.sha256(token.encode()).hexdigest()
    q = next(
        (
            q
            for q in inbox.questions.values()
            if q.mirror and q.mirror.bot_id == bot_id and digest in q.mirror.action_digests
        ),
        None,
    )
    if not q or not q.mirror:
        raise HTTPException(403, "Unknown card action")
    try:
        _, binding = current_binding(q)
        m = q.mirror
        if (
            header.get("app_id"),
            header.get("tenant_key"),
            operator.get("tenant_key"),
            operator.get("open_id"),
            context.get("open_chat_id"),
            context.get("open_message_id"),
            event.get("host"),
        ) != (
            m.app_id,
            m.tenant_key,
            m.tenant_key,
            m.sender_open_id,
            m.chat_id,
            m.message_id,
            "im_message",
        ) or not m.message_id:
            raise HTTPException(403, "Card binding mismatch")
        stamp = header.get("create_time")
        if (
            not isinstance(stamp, str)
            or not stamp.isdecimal()
            or abs(time.time() - int(stamp) / 1_000_000) > 300
        ):
            raise HTTPException(403, "Invalid card event time")
        if q.expires_at <= utc_now() or q.state != "open":
            return _toast("This question is expired or already answered", "warning")
        action_record = m.action_digests[digest]
        if action_record["operation"] != value["hub_action"]:
            raise HTTPException(400, "Invalid card operation")
        if action_record["operation"] == "question_text_hint":
            if value["hub_action"] != "question_text_hint":
                raise HTTPException(400, "Invalid card operation")
            return _toast(f"Reply to this card, or send /answer {q.question_ref} your text")
        if value["hub_action"] != "question_answer":
            raise HTTPException(400, "Invalid card operation")
        user = User(open_id=binding.owner.open_id, name="Feishu", email=binding.owner.email)
        await answers.admit(
            q,
            user,
            QuestionAnswerCreate(
                call_id=f"card:{bot_id}:{hashlib.sha256(event_id.encode()).hexdigest()}",
                selected=[action_record["selected"]],
            ),
            callback=(event_key, fingerprint),
            source="feishu",
            mirror_send_uuid=m.send_uuid,
            action_digest=digest,
        )
        return _toast("Answer received; delivering to Hub")
    except InboxError as exc:
        if exc.status == 409:
            return _toast("This question is stale or already answered", "warning")
        raise HTTPException(exc.status, exc.detail) from exc


async def intercept_reply(bot_id: str, event: FeishuMessageEvent) -> bool:
    """Run before batching, never infer an answer from uncorrelated prose."""
    from ... import feishu_bot as bot

    inbox = answers.inbox()
    explicit = event.text.strip().split(maxsplit=2)
    ref = explicit[1] if len(explicit) >= 2 and explicit[0] == "/answer" else None
    q = next(
        (
            q
            for q in inbox.questions.values()
            if q.mirror
            and q.mirror.bot_id == bot_id
            and (
                (ref is not None and q.question_ref == ref)
                or (event.parent_id and q.mirror.message_id == event.parent_id)
            )
        ),
        None,
    )
    if q is None:
        return ref is not None  # explicit unknown answer never becomes a prompt
    try:
        effective, binding = current_binding(q)
        if (event.chat_type, event.app_id, event.chat_id, event.sender_open_id) != (
            "p2p",
            effective.app_id,
            binding.chat_id,
            binding.sender_open_id,
        ):
            raise InboxError(403, "Reply binding mismatch")
        if not bot._message_matches_binding_time(event, binding) or q.expires_at <= utc_now():
            raise InboxError(409, "Reply expired")
        text = explicit[2] if ref and len(explicit) == 3 else "" if ref else event.text.strip()
        await answers.admit(
            q,
            User(open_id=binding.owner.open_id, name="Feishu", email=binding.owner.email),
            QuestionAnswerCreate(
                call_id=f"reply:{bot_id}:{hashlib.sha256(event.message_id.encode()).hexdigest()}",
                selected=[text],
            ),
            source="feishu",
            mirror_send_uuid=q.mirror.send_uuid if q.mirror else None,
        )
    except (InboxError, ValueError):
        return True
    return True


def register_card_routes(router: APIRouter) -> None:
    @router.post("/bots/{bot_id}/card-actions")
    async def callback(bot_id: str, request: Request) -> dict[str, Any]:
        from ... import feishu_bot as bot

        # Trust scheme only after deployment's configured proxy middleware.
        if request.url.scheme != "https":
            raise HTTPException(400, "HTTPS required")
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise HTTPException(415, "JSON required")
        try:
            effective = bot._pool.effective(bot_id)
        except FeishuBotPoolError:
            raise HTTPException(403, "Bot unavailable") from None
        if len(bot_id) > 128 or (bot_id not in _rates and len(_rates) >= 128):
            raise HTTPException(429, "Callback capacity limit")
        bucket = _rates.setdefault(bot_id, deque())
        now = time.monotonic()
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= 60:
            raise HTTPException(429, "Callback rate limit")
        bucket.append(now)
        raw = bytearray()
        try:
            async with asyncio.timeout(1):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > 65536:
                        raise HTTPException(413, "Callback too large")
                    raw.extend(chunk)
        except TimeoutError:
            raise HTTPException(408, "Callback read timeout") from None
        try:
            effective = bot._pool.effective(bot_id)
        except FeishuBotPoolError:
            raise HTTPException(403, "Bot unavailable") from None
        payload = _decode(bytes(raw), request.headers, effective)
        if payload.get("type") == "url_verification":
            challenge = payload.get("challenge")
            if not isinstance(challenge, str) or not 1 <= len(challenge) <= 256:
                raise HTTPException(400, "Invalid URL challenge")
            return {"challenge": challenge}
        inbox = answers.inbox()
        nonce_key = f"{bot_id}:{hashlib.sha256(request.headers['x-lark-request-nonce'].encode()).hexdigest()}"
        body_digest = hashlib.sha256(bytes(raw)).hexdigest()
        async with inbox._lock:
            inbox.callback_nonces = {
                k: v
                for k, v in inbox.callback_nonces.items()
                if float(v["expires_at"]) > time.time()
            }
            previous = inbox.callback_nonces.get(nonce_key)
            if previous and previous["fingerprint"] != body_digest:
                raise HTTPException(409, "Callback nonce reused with changed body")
            if not previous:
                inbox.callback_nonces[nonce_key] = {
                    "fingerprint": body_digest,
                    "expires_at": str(time.time() + 600),
                }
                inbox._save()
        try:
            async with asyncio.timeout(1.5):
                return await handle_action(bot_id, payload)
        except TimeoutError:
            raise HTTPException(503, "Question admission is busy; retry explicitly") from None
