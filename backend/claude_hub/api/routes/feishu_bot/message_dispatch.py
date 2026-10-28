"""Feishu message batching, admission, and native Chat dispatch."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from fastapi import HTTPException

from claude_hub.api.agent_stream import (
    CHAT_ERROR_REASON_HEADER,
    AgentStreamAttachment,
    ExternalDispatchRetired,
    ExternalInteractionRequired,
)
from claude_hub.api.routes.agent_stream.dispatch import ExternalQuestionPending
from claude_hub.api.routes.feishu_bot.bindings import FeishuBotRouteBindings
from claude_hub.api.routes.feishu_bot.queueing import TabDispatchItem as _TabDispatchItem
from claude_hub.api.routes.feishu_bot.queueing import TabDispatchQueue as _TabDispatchQueue
from claude_hub.services.agent_stream.attachments import preview_within_cache_bounds
from claude_hub.services.agent_stream.turn_source import (
    FEISHU_PROVIDER_TEXT_FORMAT_V2,
    format_feishu_provider_text_v2,
)
from claude_hub.services.feishu_bot import (
    FeishuBotClient,
    FeishuMessageEvent,
    FeishuMessageTimeStatus,
    feishu_message_time_notice,
    feishu_message_time_status,
)
from claude_hub.services.feishu_bot_pool import (
    BotBinding,
    EffectiveBot,
    FeishuBotPoolError,
    FeishuBotRevisionConflict,
    PairingClaim,
    turn_id_for,
)
from claude_hub.services.feishu_message_format import build_feishu_message_payload

logger = logging.getLogger(__name__)
_BIND_CODE_RE = re.compile(r"^(?:bind\s+)?(CH-[A-Z0-9]{10})$", re.IGNORECASE)


# How long a finished batch waits for a stop a newer message asked for.
_STOP_SETTLE_SECONDS = 15.0


class FeishuMessageTimeRejected(HTTPException):
    """Only the local admission guard can prove this turn was never submitted."""

    def __init__(self, reason: FeishuMessageTimeStatus) -> None:
        super().__init__(
            status_code=409,
            detail="message_outside_binding_period",
            headers={
                CHAT_ERROR_REASON_HEADER: "message_outside_binding_period",
                "X-Claude-Hub-Feishu-Reject-Reason": reason.value,
            },
        )
        self.reason = reason


class FeishuMessageDispatcher:
    """Stateless message algorithms backed by façade-owned runtime state."""

    def __init__(self, bindings: FeishuBotRouteBindings) -> None:
        self.bindings = bindings

    def _binding_code(self, text: str) -> str | None:
        match = _BIND_CODE_RE.fullmatch(text.strip())
        return match.group(1).upper() if match is not None else None

    def _turn_metadata(self, effective: EffectiveBot, event: FeishuMessageEvent) -> dict[str, Any]:
        """Additive only: ``bot_id`` joins the existing ``feishu`` sub-object so no
        Task schema or frontend reader has to change."""

        return {
            "origin": "feishu",
            "provider_text_format": FEISHU_PROVIDER_TEXT_FORMAT_V2,
            "feishu": {
                "bot_id": effective.bot_id,
                "app_id": event.app_id,
                "bot_name": effective.feishu_name,
                "bot_avatar_url": effective.feishu_avatar_url,
                "chat_id": event.chat_id,
                "message_id": event.message_id,
                "sender_open_id": event.sender_open_id,
                "chat_type": event.chat_type,
                "thread_id": event.thread_id,
            },
        }

    def _image_mime(self, content: bytes) -> str | None:
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            return "image/webp"
        return None

    async def _event_attachments(
        self,
        client: FeishuBotClient,
        event: FeishuMessageEvent,
        *,
        remaining_count: int,
        remaining_bytes: int,
    ) -> tuple[list[AgentStreamAttachment], int, list[int]]:
        """Download one event's images within the shared native-turn budget."""

        attachments: list[AgentStreamAttachment] = []
        used_bytes = 0
        omitted: list[int] = []
        for index, image_key in enumerate(event.image_keys, start=1):
            if len(attachments) >= remaining_count:
                logger.info(
                    "Feishu message image omitted for %s item %d: batch count limit",
                    event.message_id,
                    index,
                )
                omitted.append(index)
                continue
            try:
                content = await client.download_message_image(event.message_id, image_key)
                mime_type = self._image_mime(content)
                if mime_type is None:
                    raise ValueError("unsupported image bytes")
            except Exception as exc:
                logger.info(
                    "Feishu message image unavailable for %s item %d: %s",
                    event.message_id,
                    index,
                    type(exc).__name__,
                )
                omitted.append(index)
                continue
            if len(content) > remaining_bytes - used_bytes:
                logger.info(
                    "Feishu message image omitted for %s item %d: batch byte limit",
                    event.message_id,
                    index,
                )
                omitted.append(index)
                continue
            encoded = base64.b64encode(content).decode("ascii")
            attachments.append(
                AgentStreamAttachment(
                    filename=f"feishu-image-{index}.{mime_type.split('/', 1)[1]}",
                    mime_type=mime_type,
                    data_url=f"data:{mime_type};base64,{encoded}",
                    preview_is_original=preview_within_cache_bounds(mime_type, content),
                )
            )
            used_bytes += len(content)
        return attachments, used_bytes, omitted

    async def _refresh_missing_profile(
        self,
        effective: EffectiveBot,
        client: FeishuBotClient,
        *,
        require_open_id: bool = False,
    ) -> EffectiveBot:
        if effective.feishu_name is not None and (
            not require_open_id or effective.feishu_open_id is not None
        ):
            return effective
        try:
            profile = await client.get_bot_profile()
        except Exception as exc:
            logger.info(
                "Feishu Bot profile refresh unavailable for %s: %s",
                effective.bot_id,
                type(exc).__name__,
            )
            profile = None

        if profile is not None:
            profile_target = effective
            for _attempt in range(2):
                try:
                    self.bindings.pool().update_profile(
                        effective.bot_id,
                        feishu_name=profile.name,
                        feishu_avatar_url=profile.avatar_url,
                        feishu_open_id=profile.open_id,
                        expected_revision=profile_target.revision,
                    )
                except FeishuBotRevisionConflict:
                    try:
                        current = self.bindings.pool().effective(effective.bot_id)
                    except FeishuBotPoolError:
                        return effective
                    if (
                        current.app_id != effective.app_id
                        or current.config != effective.config
                        or current.generation != effective.generation
                    ):
                        return effective
                    profile_target = current
                    continue
                except Exception as exc:
                    logger.info(
                        "Feishu Bot profile refresh unavailable for %s: %s",
                        effective.bot_id,
                        type(exc).__name__,
                    )
                    break
                try:
                    current = self.bindings.pool().effective(effective.bot_id)
                except FeishuBotPoolError:
                    return effective
                if (
                    current.app_id != effective.app_id
                    or current.config != effective.config
                    or current.generation != effective.generation
                ):
                    return effective
                return current
            return EffectiveBot(
                bot_id=profile_target.bot_id,
                app_id=profile_target.app_id,
                config=profile_target.config,
                revision=profile_target.revision,
                generation=profile_target.generation,
                feishu_name=profile.name,
                feishu_avatar_url=profile.avatar_url,
                feishu_open_id=profile.open_id,
            )

        try:
            fallback = self.bindings.pool().snapshot().get(effective.bot_id)
        except FeishuBotPoolError:
            fallback = None
        if (
            fallback is not None
            and fallback.config is not None
            and fallback.app_id == effective.app_id
            and fallback.config == effective.config
            and fallback.generation == effective.generation
        ):
            return EffectiveBot(
                bot_id=effective.bot_id,
                app_id=fallback.app_id,
                config=fallback.config,
                revision=fallback.revision,
                generation=fallback.generation,
                feishu_name=fallback.feishu_name or fallback.name,
                feishu_avatar_url=fallback.feishu_avatar_url,
                feishu_open_id=fallback.feishu_open_id,
            )
        return effective

    def _binding_still_routes(self, effective: EffectiveBot, binding: BotBinding) -> bool:
        """Compare the pairing id, not just the fields.

        Re-pairing the same Chat produces a new pairing id, so a reply computed for
        the previous binding can never be delivered against the new one.
        """

        current = self.bindings.pool().routed_binding(
            effective.bot_id,
            sender_open_id=binding.sender_open_id,
            chat_id=binding.chat_id,
        )
        if current is None or current.pairing_id != binding.pairing_id:
            return False
        return self.bindings.owner_is_authorized(current.owner)

    def _message_time_status(
        self, event: FeishuMessageEvent, binding: BotBinding
    ) -> FeishuMessageTimeStatus:
        now = self.bindings.now()
        skew = self.bindings.feishu_message_future_skew_seconds()
        result = feishu_message_time_status(
            event.message_created_at_ms,
            activated_at=binding.created_at,
            now=now,
            future_skew_seconds=skew,
        )
        if result != FeishuMessageTimeStatus.VALID:
            offset_ms = (
                None
                if result == FeishuMessageTimeStatus.INVALID_TIMESTAMP
                else event.message_created_at_ms - now * 1000
            )
            logger.info(
                "Feishu message time rejected: message=%s pairing=%s reason=%s "
                "offset_ms=%s allowed_future_seconds=%s",
                event.message_id,
                binding.pairing_id,
                result.value,
                offset_ms,
                skew,
            )
        return result

    def _message_matches_binding_time(self, event: FeishuMessageEvent, binding: BotBinding) -> bool:
        return self._message_time_status(event, binding) == FeishuMessageTimeStatus.VALID

    def _validate_binding_dispatch(
        self, effective: EffectiveBot, binding: BotBinding, event: FeishuMessageEvent
    ) -> None:
        """Validate a message while the caller holds its bot's admission gate."""
        if not self.bindings.pool().is_current(effective) or not self._binding_still_routes(
            effective, binding
        ):
            raise HTTPException(status_code=409, detail="bot_binding_revoked")
        time_status = self._message_time_status(event, binding)
        if time_status != FeishuMessageTimeStatus.VALID:
            raise FeishuMessageTimeRejected(time_status)
        self.bindings.validate_saved_target(binding)

    @asynccontextmanager
    async def _admit_binding_dispatch(
        self, effective: EffectiveBot, binding: BotBinding, event: FeishuMessageEvent
    ) -> AsyncIterator[None]:
        async with self.bindings.gate(effective.bot_id):
            self._validate_binding_dispatch(effective, binding, event)
            yield

    @asynccontextmanager
    async def _admit_batch_dispatch(
        self, queue: _TabDispatchQueue, batch: list[_TabDispatchItem]
    ) -> AsyncIterator[None]:
        """Validate the claimed batch and mark history only after native handoff."""
        leader = batch[0]
        # Coalesced items share a bot and pairing, but their timestamps differ.
        # Keep one gate through every check and the handoff; nested single-message
        # guards would reacquire the same non-reentrant lock.
        async with self.bindings.gate(leader.effective.bot_id):
            for item in batch:
                self._validate_binding_dispatch(item.effective, item.binding, item.event)
            yield
        context = (
            leader.effective.bot_id,
            leader.binding.pairing_id,
            leader.event.chat_id,
            leader.event.thread_id,
        )
        queue.dispatched_contexts[context] = None
        queue.dispatched_contexts.move_to_end(context)
        while len(queue.dispatched_contexts) > self.bindings.max_tab_dispatch_contexts():
            queue.dispatched_contexts.popitem(last=False)

    def _claim_still_live(self, bot_id: str, claim: PairingClaim) -> bool:
        try:
            entry = self.bindings.pool().snapshot().get(bot_id)
        except FeishuBotPoolError:
            return False
        if entry is None or not entry.enabled:
            return False
        current = next((item for item in entry.claims if item.pairing_id == claim.pairing_id), None)
        if current is None:
            return False
        return self.bindings.owner_is_authorized(current.owner)

    async def _reply_if_current(
        self,
        client: FeishuBotClient,
        event: FeishuMessageEvent,
        effective: EffectiveBot,
        text: str,
        *,
        still_valid: Callable[[], bool] | None = None,
        formatted: bool = False,
    ) -> bool:
        try:
            # Token acquisition can block on Feishu and must stay outside the gate.
            token = await client.get_tenant_token()
            async with self.bindings.gate(effective.bot_id):
                if not self.bindings.pool().is_current(effective):
                    return False
                if still_valid is not None and not still_valid():
                    return False
                # An already-sent request cannot be recalled, so a timeout is never retried.
                if formatted:
                    payload = build_feishu_message_payload(text)
                    if event.thread_id is not None:
                        outbound = client.reply_message(
                            event.message_id,
                            payload.msg_type,
                            payload.content,
                            access_token=token,
                            reply_in_thread=True,
                        )
                    else:
                        outbound = client.reply_message(
                            event.message_id,
                            payload.msg_type,
                            payload.content,
                            access_token=token,
                        )
                else:
                    if event.thread_id is not None:
                        outbound = client.reply_text(
                            event.message_id, text, access_token=token, reply_in_thread=True
                        )
                    else:
                        outbound = client.reply_text(event.message_id, text, access_token=token)
                await asyncio.wait_for(
                    outbound, timeout=self.bindings.outbound_post_timeout_seconds()
                )
                return True
        except Exception as exc:
            logger.warning("Feishu Bot reply failed: %s", type(exc).__name__)
            return False

    async def _add_processing_reaction(
        self, client: FeishuBotClient, event: FeishuMessageEvent, emoji_type: str = "Typing"
    ) -> str | None:
        """Best-effort hint; legacy Bots may not have the reaction scope."""

        try:
            # ``wait_for`` creates a child task and then waits for its cancellation
            # handshake when the route is cancelled. Keeping the HTTP await in this
            # task lets shutdown cancellation release its FIFO reservation promptly.
            async with asyncio.timeout(self.bindings.reaction_post_timeout_seconds()):
                return await client.add_reaction(event.message_id, emoji_type)
        except Exception as exc:
            logger.info("Feishu Bot processing reaction unavailable: %s", type(exc).__name__)
            return None

    async def _delete_processing_reaction(
        self, client: FeishuBotClient, event: FeishuMessageEvent, reaction_id: str | None
    ) -> None:
        if reaction_id is None:
            return
        try:
            async with asyncio.timeout(self.bindings.reaction_post_timeout_seconds()):
                await client.delete_reaction(event.message_id, reaction_id)
        except Exception as exc:
            logger.info(
                "Feishu Bot processing reaction cleanup unavailable: %s", type(exc).__name__
            )

    async def _wait_for_reaction_dispatch(self, item: _TabDispatchItem) -> bool:
        """Wait for dispatch or completion without coupling either to Feishu I/O."""

        dispatch = asyncio.create_task(item.reaction_dispatch_started.wait())
        finished = asyncio.create_task(item.reaction_finished.wait())
        try:
            await asyncio.wait({dispatch, finished}, return_when=asyncio.FIRST_COMPLETED)
            return item.reaction_dispatch_started.is_set() and not item.reaction_finished.is_set()
        finally:
            for waiter in (dispatch, finished):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(dispatch, finished, return_exceptions=True)

    async def _maintain_processing_reaction(self, item: _TabDispatchItem) -> None:
        """Run the optional reaction lifecycle outside the native dispatch path."""

        try:
            item.queued_reaction_id = await self.bindings.add_processing_reaction(
                item.client, item.event, "OneSecond"
            )
            if not await self._wait_for_reaction_dispatch(item):
                return
            await self.bindings.delete_processing_reaction(
                item.client, item.event, item.queued_reaction_id
            )
            item.queued_reaction_id = None
            if item.reaction_finished.is_set():
                return
            item.processing_reaction_id = await self.bindings.add_processing_reaction(
                item.client, item.event
            )
            await item.reaction_finished.wait()
        finally:
            await self.bindings.delete_processing_reaction(
                item.client,
                item.event,
                item.processing_reaction_id or item.queued_reaction_id,
            )
            item.processing_reaction_id = None
            item.queued_reaction_id = None

    async def _finish_processing_reaction(self, item: _TabDispatchItem) -> None:
        """Finish the owned reaction task even if route cancellation repeats."""

        task = item.reaction_task
        if task is None:
            return
        item.reaction_finished.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # A second shutdown cancellation must not orphan the shielded task
                # or replace the precise ExternalDispatchRetired already propagating
                # from the route. Reaction I/O is bounded by its own timeouts.
                continue
        await task

    def _batch_visible_text(self, items: list[_TabDispatchItem], *, followup: bool) -> str:
        if len(items) == 1 and not followup:
            return items[0].event.text
        intro = (
            "用户在你处理上一条消息期间补充了以下信息，请结合已有上下文统一处理："
            if followup
            else "用户在短时间内连续发送了以下消息，请作为一次请求统一处理："
        )
        messages = "\n\n".join(
            f"[消息 {index}]\n{item.event.text}" for index, item in enumerate(items, start=1)
        )
        return f"{intro}\n\n{messages}"

    async def _stopped_for_newer(self, batch: list[_TabDispatchItem]) -> bool | None:
        """None when no newer message asked to stop this batch; else whether it stopped."""
        stop = next((item.stop_task for item in batch if item.stop_task is not None), None)
        if stop is None:
            return None
        return await asyncio.shield(stop)

    async def _settle_stop(self, queue: _TabDispatchQueue, batch: list[_TabDispatchItem]) -> None:
        stop = next((item.stop_task for item in batch if item.stop_task is not None), None)
        if stop is None:
            return
        if not stop.done():
            try:
                await asyncio.wait_for(asyncio.shield(stop), timeout=_STOP_SETTLE_SECONDS)
            except BaseException:
                # Shutdown or a stop that hangs: give up on it, never on the batch.
                stop.cancel()
        queue.interrupted_text = ""

    def _interrupted_text(self, earlier: str, items: list[_TabDispatchItem]) -> str:
        messages = "\n\n".join(
            f"[新消息 {index}]\n{item.event.text}" for index, item in enumerate(items, start=1)
        )
        return (
            "你回复用户上一条消息时，用户又发来了新消息，上一次回复已停止、未发出。"
            "请结合两者给出一个回复：\n\n"
            f"[上一条]\n{earlier}\n\n{messages}"
        )

    def _batch_matches(self, first: _TabDispatchItem, candidate: _TabDispatchItem) -> bool:
        return (
            candidate.bot_id == first.bot_id
            and candidate.binding.pairing_id == first.binding.pairing_id
            and candidate.effective.generation == first.effective.generation
            and candidate.effective.revision == first.effective.revision
            and candidate.event.chat_id == first.event.chat_id
            and candidate.event.thread_id == first.event.thread_id
        )

    async def _collect_tab_dispatch_batch(
        self, queue: _TabDispatchQueue, leader: _TabDispatchItem
    ) -> tuple[list[_TabDispatchItem], bool]:
        """Wait for a short quiet period and identify one ordered batch."""

        loop = asyncio.get_running_loop()
        first_arrival = leader.arrived_at
        while True:
            # Clear before taking the snapshot: an arrival after this point sets
            # the event, while an earlier arrival is already represented below.
            # Clearing after the snapshot would lose a wake-up in between and
            # could dispatch before the intended quiet period elapsed.
            queue.changed.clear()
            live = [item for item in queue.pending if not item.cancelled]
            matching: list[_TabDispatchItem] = []
            for item in live:
                if matching and not self._batch_matches(leader, item):
                    break
                if self._batch_matches(leader, item):
                    matching.append(item)
            last_arrival = matching[-1].arrived_at if matching else first_arrival
            deadline = min(
                first_arrival + self.bindings.message_coalesce_max_wait_seconds(),
                last_arrival + self.bindings.message_coalesce_quiet_seconds(),
            )
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(queue.changed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                break

        candidates: list[_TabDispatchItem] = []
        for item in list(queue.pending):
            if item.cancelled:
                queue.pending.remove(item)
                continue
            if candidates and not self._batch_matches(leader, item):
                break
            if not self._batch_matches(leader, item):
                break
            candidates.append(item)
        context = (
            leader.bot_id,
            leader.binding.pairing_id,
            leader.event.chat_id,
            leader.event.thread_id,
        )
        return candidates, context in queue.dispatched_contexts

    def _claim_tab_dispatch_batch(
        self, queue: _TabDispatchQueue, candidates: list[_TabDispatchItem]
    ) -> list[_TabDispatchItem]:
        """Synchronously commit candidates immediately before native dispatch."""

        batch = [item for item in candidates if item in queue.pending and not item.cancelled]
        for item in batch:
            queue.pending.remove(item)
        queue.processor = None
        queue.active = batch
        for item in batch:
            item.dispatch_started = True
            if not item.start.done():
                item.start.set_result(None)
        return batch

    def _finish_tab_dispatch_batch(
        self, tab_id: str, queue: _TabDispatchQueue, batch: list[_TabDispatchItem], status: str
    ) -> None:
        for item in batch:
            if not item.outcome.done():
                item.outcome.set_result(status)
            item.reaction_finished.set()
        queue.active = []
        self.bindings.promote_tab_dispatch(tab_id, queue)

    async def _dispatch_tab_batch(
        self, tab_id: str, queue: _TabDispatchQueue, leader: _TabDispatchItem
    ) -> str:
        batch: list[_TabDispatchItem] = []
        status = "failed"
        try:
            try:
                candidates, followup = await self._collect_tab_dispatch_batch(queue, leader)
                if not candidates:
                    raise ExternalDispatchRetired(
                        turn_id_for(leader.bot_id, leader.event.message_id)
                    )
                batch = self._claim_tab_dispatch_batch(queue, candidates)
                if not batch:
                    raise ExternalDispatchRetired(
                        turn_id_for(leader.bot_id, leader.event.message_id)
                    )
                batch[0].reaction_dispatch_started.set()
                visible_text = self._batch_visible_text(batch, followup=followup)
                # The user's own words; a later stop carries these, without intros.
                core_text = "\n\n".join(item.event.text for item in batch)
                if queue.interrupted_text:
                    # The answer in progress was stopped for these messages.
                    visible_text = self._interrupted_text(queue.interrupted_text, batch)
                    core_text = f"{queue.interrupted_text}\n\n{core_text}"
                    queue.interrupted_text = ""
                attachments: list[AgentStreamAttachment] = []
                attachment_bytes = 0
                omitted_images: list[str] = []
                for message_index, item in enumerate(batch, start=1):
                    event_attachments, event_bytes, omitted = await self.bindings.event_attachments(
                        item.client,
                        item.event,
                        remaining_count=self.bindings.max_batch_image_attachments()
                        - len(attachments),
                        remaining_bytes=self.bindings.max_batch_image_bytes() - attachment_bytes,
                    )
                    attachments.extend(event_attachments)
                    attachment_bytes += event_bytes
                    omitted_images.extend(
                        (
                            f"消息 {message_index} 的图片 {image_index}"
                            if len(batch) > 1
                            else f"图片 {image_index}"
                        )
                        for image_index in omitted
                    )
                if omitted_images:
                    visible_text += (
                        "\n\n[系统提示：以下图片未能转发给模型："
                        + "、".join(omitted_images)
                        + "。请勿假设已经看到这些图片；如需分析，请提示用户重新发送。]"
                    )
                images = sum(len(item.event.image_keys) for item in batch)
                queue.active_text = core_text + (
                    f"\n\n[系统提示：上一条消息附带的 {images} 张图片不会随本次重新发送；"
                    "如需分析，请提示用户重新发送。]"
                    if images
                    else ""
                )
                for item in batch:
                    item.native_bridge_started = True
                assistant_text = await self.bindings.dispatch_tab_chat_and_wait(
                    tab_id,
                    format_feishu_provider_text_v2(
                        visible_text,
                        app_id=leader.event.app_id,
                        chat_id=leader.event.chat_id,
                        message_id=leader.event.message_id,
                        sender_open_id=leader.event.sender_open_id,
                    ),
                    turn_id_for(leader.bot_id, leader.event.message_id),
                    visible_text=visible_text,
                    turn_metadata={
                        **self._turn_metadata(leader.effective, leader.event),
                        "feishu_message_ids": [item.event.message_id for item in batch],
                        "feishu_coalesced": len(batch) > 1,
                        "feishu_followup": followup,
                    },
                    attachments=attachments,
                    admission_guard=lambda: self._admit_batch_dispatch(queue, batch),
                )
                if await self._stopped_for_newer(batch) is not None:
                    # It ended before the stop took: this answer is sent, so the
                    # newer message follows it as usual.
                    queue.interrupted_text = ""
                status = (
                    "completed"
                    if await self.bindings.reply_if_current(
                        batch[-1].client,
                        batch[-1].event,
                        batch[-1].effective,
                        assistant_text,
                        still_valid=lambda: self._binding_still_routes(
                            batch[-1].effective, batch[-1].binding
                        ),
                        formatted=True,
                    )
                    else "reply_failed"
                )
            except ExternalDispatchRetired:
                status = "retired"
            except FeishuMessageTimeRejected as exc:
                for item in batch:
                    item.admission_time_rejection = exc.reason
                notice = feishu_message_time_notice(exc.reason)
                if notice is None:
                    status = "pre_dispatch_rejected"
                else:
                    # Cancellation during this post must not make an uncertain
                    # notification eligible for another send or native dispatch.
                    status = "reply_failed"
                    target = batch[-1] if batch else leader
                    if await self.bindings.reply_if_current(
                        target.client,
                        target.event,
                        target.effective,
                        notice,
                        still_valid=lambda: self._binding_still_routes(
                            target.effective, target.binding
                        ),
                    ):
                        status = "pre_dispatch_rejected"
            except ExternalQuestionPending:
                status = "completed"  # the durable question, not this waiter, owns continuation
            except ExternalInteractionRequired:
                target = batch[-1] if batch else leader
                await self.bindings.reply_if_current(
                    target.client,
                    target.event,
                    target.effective,
                    "本次处理需要在 Claude Hub 网页确认或回答，已停止等待。请在网页处理后重试。",
                    still_valid=lambda: self._binding_still_routes(
                        target.effective, target.binding
                    ),
                )
                status = "failed"
            except HTTPException as exc:
                logger.warning("Feishu Bot target rejected: status=%s", exc.status_code)
                reason = (
                    (exc.headers or {}).get(CHAT_ERROR_REASON_HEADER)
                    if exc.status_code == 409
                    else None
                )
                if reason == "message_outside_binding_period":
                    status = "failed"
                else:
                    if reason == "chat_busy":
                        message = (
                            "Claude Hub Chat 正在处理其他消息或等待网页回答，本条消息尚未执行。"
                        )
                    elif exc.status_code == 409 and reason != "binding_target_missing":
                        message = (
                            "Claude Hub Chat 当前不可用，本条消息尚未执行。"
                            "请在网页检查 Chat 状态后重试。"
                        )
                    else:
                        message = "Claude Hub 目标当前不可用，请在网页重新配对。"
                    target = batch[-1] if batch else leader
                    await self.bindings.reply_if_current(
                        target.client,
                        target.event,
                        target.effective,
                        message,
                        still_valid=lambda: self._binding_still_routes(
                            target.effective, target.binding
                        ),
                    )
                    status = "failed"
            except asyncio.CancelledError:
                if batch and not any(item.native_bridge_started for item in batch):
                    status = "retired"
                    return status
                raise
            except Exception:
                if await self._stopped_for_newer(batch):
                    # Stopped for a newer message, which answers this one too.
                    status = "superseded"
                    return status
                logger.exception("Feishu Bot message dispatch failed")
                target = batch[-1] if batch else leader
                await self.bindings.reply_if_current(
                    target.client,
                    target.event,
                    target.effective,
                    "Claude Hub 处理消息失败，请稍后重试。",
                    still_valid=lambda: self._binding_still_routes(
                        target.effective, target.binding
                    ),
                )
                status = "failed"
            return status
        finally:
            # This outer finally also runs when cancellation strikes while an error
            # reply is being posted from one of the handlers above. No path may
            # leave a claimed batch in ``queue.active`` with unresolved followers.
            if status != "superseded":
                # Answered, failed or retired here: a stop that lands late must not
                # make the next batch claim this one was stopped.
                await self._settle_stop(queue, batch)
            if batch:
                self._finish_tab_dispatch_batch(tab_id, queue, batch, status)
