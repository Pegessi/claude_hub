"""Active-chat background poller for the Feishu personal ``/ai`` command workflow.

When the target Feishu group has no Hub Bot member (the common case for large
cross-team groups), ``im.message.receive_v1`` WebSocket events are not delivered.
This poller uses the owner's ``user_access_token`` to inspect recently active
chats (or an explicit ``watch_chats`` allowlist), maintains per-chat watermarks,
and dispatches newly detected ``/ai <instruction>`` messages from the owner into
``FeishuAiCommandService``.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from typing import Any

from claude_hub.services.feishu_ai_command import (
    FeishuAiCommandService,
    get_feishu_ai_command_service,
    parse_ai_command,
)
from claude_hub.services.feishu_user_client import FeishuUserMessage

logger = logging.getLogger(__name__)

# On the first poll of a chat with no persisted watermark, look back up to 5 minutes
# so a command sent right before enabling the poller is caught without replaying old history.
_INITIAL_WATERMARK_LOOKBACK_MS = 5 * 60 * 1000


class FeishuAiPoller:
    """Supervised background poller for personal ``/ai`` commands."""

    def __init__(
        self,
        service: FeishuAiCommandService | None = None,
        *,
        initial_lookback_ms: int = _INITIAL_WATERMARK_LOOKBACK_MS,
    ) -> None:
        self._service = service or get_feishu_ai_command_service()
        self._initial_lookback_ms = initial_lookback_ms
        self._task: asyncio.Task[None] | None = None
        self._wake_event = asyncio.Event()
        self._stopping = False
        self._poll_count = 0
        self._last_poll_at: float | None = None
        self._last_error: str | None = None
        self._commands_detected = 0
        self._commands_succeeded = 0
        self._commands_failed = 0
        self._skipped_count = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done() and not self._stopping

    def status_dict(self) -> dict[str, Any]:
        watermarks = self._service.get_watermarks()
        return {
            "running": self.running,
            "poll_count": self._poll_count,
            "last_poll_at": self._last_poll_at,
            "last_error": self._last_error,
            "commands_detected": self._commands_detected,
            "commands_succeeded": self._commands_succeeded,
            "commands_failed": self._commands_failed,
            "skipped_count": self._skipped_count,
            "watermarks_count": len(watermarks),
            "recent_runs": self._service.get_recent_runs(),
        }

    async def start(self) -> None:
        self._stopping = False
        if self._task is None or self._task.done():
            self._wake_event = asyncio.Event()
            self._task = asyncio.create_task(self._run_loop(), name="feishu-ai-poller")

    async def reconcile(self) -> None:
        """Wake the poller loop immediately after a configuration or token change."""
        cfg = self._service.get_config()
        if cfg.enabled and not self.running and not self._stopping:
            await self.start()
        self._wake_event.set()

    async def quiesce(self) -> None:
        self._stopping = True
        self._wake_event.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def stop(self) -> None:
        await self.quiesce()
        self._task = None

    async def _run_loop(self) -> None:
        while not self._stopping:
            cfg = self._service.get_config()
            interval = max(2.0, cfg.poll_interval_seconds)
            if cfg.enabled:
                try:
                    await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._last_error = str(exc) or type(exc).__name__
                    logger.warning("Feishu /ai poller tick failed: %s", self._last_error)
            self._wake_event.clear()
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def poll_once(self) -> list[str]:
        """Execute one polling pass across watched or active chats.

        Returns the list of ``message_id``s processed during this pass.
        """
        cfg = self._service.get_config()
        if not cfg.enabled:
            return []

        self._poll_count += 1
        self._last_poll_at = time.time()
        user_client = self._service.build_user_client(cfg)
        record = user_client.resolve_record()
        if record is None:
            self._last_error = "no_user_token"
            return []

        owner_open_id = (cfg.owner_open_id or record.open_id).strip()
        if not owner_open_id:
            self._last_error = "no_owner_open_id"
            return []

        chat_ids: list[str] = list(cfg.watch_chats)
        if not chat_ids:
            chats = await user_client.list_chats(
                sort_type="ByActiveTimeDesc",
                page_size=cfg.max_active_chats,
            )
            chat_ids = [c.chat_id for c in chats if c.chat_id]

        watermarks = self._service.get_watermarks()
        now_ms = int(time.time() * 1000)
        processed_ids: list[str] = []

        last_chat_error: str | None = None
        for chat_id in chat_ids:
            try:
                messages = await user_client.list_messages(
                    container_id_type="chat",
                    container_id=chat_id,
                    sort_type="ByCreateTimeDesc",
                    page_size=min(50, max(10, cfg.max_context_messages)),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_chat_error = f"{chat_id}: {exc}"
                logger.warning("Feishu /ai poller could not read chat %s: %s", chat_id, exc)
                continue
            if not messages:
                continue

            watermark_ms = watermarks.get(chat_id)
            if watermark_ms is None:
                watermark_ms = max(0, now_ms - self._initial_lookback_ms)
                self._service.set_watermark(chat_id, watermark_ms)

            # Process in chronological order (oldest -> newest above watermark)
            ascending = sorted(messages, key=lambda item: item.create_time_ms)
            max_seen_ms = watermark_ms
            for msg in ascending:
                if msg.create_time_ms > max_seen_ms:
                    max_seen_ms = msg.create_time_ms
                if msg.create_time_ms <= watermark_ms:
                    self._skipped_count += 1
                    continue
                if msg.sender_type != "user" or not hmac.compare_digest(
                    msg.sender_open_id.strip(), owner_open_id
                ):
                    self._skipped_count += 1
                    continue
                try:
                    parsed = parse_ai_command(
                        msg.text,
                        prefix=cfg.prefix,
                        default_provider=cfg.provider,
                        default_model=cfg.model,
                        default_reasoning_effort=cfg.reasoning_effort,
                        footer=cfg.footer,
                    )
                except ValueError:
                    parsed = None
                if parsed is None:
                    self._skipped_count += 1
                    continue

                self._commands_detected += 1
                seeded: list[FeishuUserMessage] | None = ascending if not msg.thread_id else None
                result = await self._service.handle_message(
                    msg,
                    source="poller",
                    seeded_context=seeded,
                )
                if result.handled:
                    processed_ids.append(msg.message_id)
                    if result.status == "completed":
                        self._commands_succeeded += 1
                    elif result.status in {"failed", "scope_blocked"}:
                        self._commands_failed += 1

            if max_seen_ms > watermark_ms:
                self._service.set_watermark(chat_id, max_seen_ms)

        self._last_error = last_chat_error
        return processed_ids


_DEFAULT_AI_POLLER: FeishuAiPoller | None = None


def get_feishu_ai_poller() -> FeishuAiPoller:
    global _DEFAULT_AI_POLLER
    if _DEFAULT_AI_POLLER is None:
        _DEFAULT_AI_POLLER = FeishuAiPoller()
    return _DEFAULT_AI_POLLER
