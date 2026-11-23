"""Active-chat background poller for the Feishu personal ``/ai`` command workflow.

When the target Feishu group has no Hub Bot member (the common case for large
cross-team groups), ``im.message.receive_v1`` WebSocket events are not delivered.
This poller uses the owner's ``user_access_token`` to inspect recently active
chats (or an explicit ``watch_chats`` allowlist), pages through the entire
unprocessed interval above each chat's watermark, durably enqueues detected
``/ai <instruction>`` commands from the owner before advancing the watermark,
and executes turns asynchronously so a slow turn never blocks intake across chats.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from claude_hub.services.feishu_ai_command import (
    FeishuAiCommandConfig,
    FeishuAiCommandService,
    get_feishu_ai_command_service,
    parse_ai_command,
)
from claude_hub.services.feishu_realtime_events import (
    FeishuRealtimeEventRouter,
    get_feishu_realtime_event_router,
)
from claude_hub.services.feishu_user_client import (
    FeishuMessageSearchPage,
    FeishuUserClient,
    FeishuUserMessage,
)

logger = logging.getLogger(__name__)

# On the first poll of a chat with no persisted watermark, look back up to 5 minutes
# so a command sent right before enabling the poller is caught without replaying old history.
_INITIAL_WATERMARK_LOOKBACK_MS = 5 * 60 * 1000
_MAX_POLL_PAGES_PER_CHAT = 25
_MAX_CONCURRENT_DISPATCHES = 4
_MAX_SEARCH_PAGES_PER_POLL = 5
_MAX_SEARCH_MESSAGE_CACHE = 512


def _message_from_dict(raw: Mapping[str, Any]) -> FeishuUserMessage | None:
    msg_id = str(raw.get("message_id") or "").strip()
    chat_id = str(raw.get("chat_id") or "").strip()
    if not msg_id or not chat_id:
        return None
    return FeishuUserMessage(
        message_id=msg_id,
        chat_id=chat_id,
        sender_open_id=str(raw.get("sender_open_id") or "").strip(),
        sender_type=str(raw.get("sender_type") or "user").strip(),
        msg_type=str(raw.get("msg_type") or "text").strip(),
        text=str(raw.get("text") or ""),
        create_time_ms=int(raw.get("create_time_ms") or 0),
        thread_id=(
            str(raw["thread_id"]).strip()
            if isinstance(raw.get("thread_id"), str) and raw["thread_id"].strip()
            else None
        ),
        parent_id=(
            str(raw["parent_id"]).strip()
            if isinstance(raw.get("parent_id"), str) and raw["parent_id"].strip()
            else None
        ),
        root_id=(
            str(raw["root_id"]).strip()
            if isinstance(raw.get("root_id"), str) and raw["root_id"].strip()
            else None
        ),
    )


class FeishuAiPoller:
    """Supervised background poller for personal ``/ai`` commands."""

    def __init__(
        self,
        service: FeishuAiCommandService | None = None,
        *,
        realtime_router: FeishuRealtimeEventRouter | None = None,
        initial_lookback_ms: int = _INITIAL_WATERMARK_LOOKBACK_MS,
        max_pages_per_chat: int = _MAX_POLL_PAGES_PER_CHAT,
        max_concurrent_dispatches: int = _MAX_CONCURRENT_DISPATCHES,
    ) -> None:
        self._service = service or get_feishu_ai_command_service()
        self._realtime_router = realtime_router or get_feishu_realtime_event_router()
        self._initial_lookback_ms = initial_lookback_ms
        self._max_pages_per_chat = max(1, max_pages_per_chat)
        self._dispatch_sem = asyncio.Semaphore(max(1, max_concurrent_dispatches))
        self._task: asyncio.Task[None] | None = None
        self._frontier_task: asyncio.Task[None] | None = None
        self._frontier_stop = asyncio.Event()
        self._frontier_connected = False
        self._last_frontier_connected_at: float | None = None
        self._last_frontier_error: str | None = None
        self._realtime_event_queue: OrderedDict[
            str, tuple[Any, FeishuUserMessage, Any, Any, set[str], bool]
        ] = OrderedDict()
        self._realtime_queue_worker: asyncio.Task[None] | None = None
        self._cached_user_client: Any = None
        self._cached_client_key: tuple[str, str, str] | None = None
        self._worker_tasks: set[asyncio.Task[None]] = set()
        self._in_flight_ids: set[str] = set()
        self._search_msg_cache: OrderedDict[str, FeishuUserMessage | None] = OrderedDict()
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
    def realtime_router(self) -> FeishuRealtimeEventRouter:
        return self._realtime_router

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done() and not self._stopping

    @property
    def frontier_running(self) -> bool:
        return (
            self._frontier_task is not None
            and not self._frontier_task.done()
            and not self._stopping
        )

    @property
    def realtime_stream_healthy(self) -> bool:
        return (
            self.frontier_running
            and self._frontier_connected
            and self._last_frontier_error is None
            and self._realtime_router.resolve_config().enabled
            and not self._realtime_router.needs_patrol_fallback
        )

    def _on_frontier_state(self, exc: Exception | None) -> None:
        if exc is None:
            self._frontier_connected = True
            self._last_frontier_connected_at = time.time()
            self._last_frontier_error = None
        else:
            self._frontier_connected = False
            self._last_frontier_error = str(exc) or type(exc).__name__

    def _resolve_owner_ids(self, cfg: FeishuAiCommandConfig, wt: Any) -> set[str]:
        ids: set[str] = set()
        if cfg.owner_open_id and cfg.owner_open_id.strip():
            ids.add(cfg.owner_open_id.strip())
        if wt is not None and hasattr(wt, "resolve_owner_identity"):
            try:
                resolved = wt.resolve_owner_identity()
                if isinstance(resolved, str) and resolved.strip():
                    ids.add(resolved.strip())
            except Exception:
                pass
        web_store = getattr(self._service, "web_session_store", None)
        if web_store is not None and hasattr(web_store, "load"):
            try:
                sess = web_store.load(auto_import_larkx=False)
                if sess is not None:
                    for attr in ("user_id", "open_id"):
                        val = getattr(sess, attr, "")
                        if isinstance(val, str) and val.strip():
                            ids.add(val.strip())
            except Exception:
                pass
        return ids

    def status_dict(self) -> dict[str, Any]:
        watermarks = self._service.get_watermarks()
        return {
            "running": self.running,
            "frontier_running": self.frontier_running,
            "frontier_connected": self._frontier_connected,
            "realtime_stream_healthy": self.realtime_stream_healthy,
            "realtime_events_emitted": self._realtime_router.emitted_count,
            "realtime_events_suppressed": self._realtime_router.suppressed_count,
            "poll_count": self._poll_count,
            "last_poll_at": self._last_poll_at,
            "last_error": self._last_error,
            "commands_detected": self._commands_detected,
            "commands_succeeded": self._commands_succeeded,
            "commands_failed": self._commands_failed,
            "skipped_count": self._skipped_count,
            "in_flight_count": len(self._in_flight_ids),
            "pending_queue_count": len(self._service.list_pending_commands()),
            "watermarks_count": len(watermarks),
            "recent_runs": self._service.get_recent_runs(),
        }

    def _enqueue_realtime_event(
        self,
        pushed: Any,
        user_msg: FeishuUserMessage,
        *,
        web_transport: Any,
        user_client: Any,
        owner_ids: set[str],
        is_owner_sender: bool,
    ) -> None:
        msg_id = (
            user_msg.message_id or getattr(getattr(pushed, "message", None), "id", "") or ""
        ).strip()
        if not msg_id:
            return
        self._realtime_event_queue[msg_id] = (
            pushed,
            user_msg,
            web_transport,
            user_client,
            owner_ids,
            is_owner_sender,
        )
        self._realtime_event_queue.move_to_end(msg_id)
        while len(self._realtime_event_queue) > 256:
            dropped_id, (_, dropped_um, _, _, _, _) = self._realtime_event_queue.popitem(last=False)
            self._realtime_router._add_fallback_record(
                dropped_id,
                dropped_id,
                "uncovered_message",
                "",
                self._realtime_router._chat_keys(dropped_um.chat_id, web_transport),
                dropped_um.thread_id or "",
                dropped_um.sender_open_id,
                dropped_um.text,
                dropped_um.create_time_ms,
            )
        if self._realtime_queue_worker is None or self._realtime_queue_worker.done():
            task = asyncio.create_task(
                self._drain_realtime_event_queue(),
                name="feishu-realtime-event-worker",
            )
            self._realtime_queue_worker = task
            self._worker_tasks.add(task)
            task.add_done_callback(self._worker_tasks.discard)

    async def _drain_realtime_event_queue(self) -> None:
        while self._realtime_event_queue and not self._stopping:
            _mid, (pushed, user_msg, wt, uc, owner_ids, is_owner) = (
                self._realtime_event_queue.popitem(last=False)
            )
            try:
                await self._realtime_router.handle_pushed_message(
                    pushed,
                    user_msg,
                    web_transport=wt,
                    user_client=uc,
                    owner_ids=owner_ids,
                    is_owner_sender=is_owner,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Feishu real-time event background worker failed for %s",
                    user_msg.message_id,
                    exc_info=True,
                )

    def _get_or_build_user_client(self, cfg: FeishuAiCommandConfig) -> Any:
        if getattr(self._service, "_user_client_factory", None) is not None:
            return self._service.build_user_client(cfg)
        key = (cfg.owner_open_id or "", cfg.app_id or "", cfg.send_mode or "auto")
        if self._cached_user_client is None or self._cached_client_key != key:
            self._cached_user_client = self._service.build_user_client(cfg)
            self._cached_client_key = key
        return self._cached_user_client

    async def _handle_frontier_pushed_web_message(self, pushed: Any) -> bool:
        return await self.handle_pushed_web_message(pushed, background_realtime=True)

    async def handle_pushed_web_message(
        self,
        pushed: Any,
        *,
        background_realtime: bool = False,
    ) -> bool:
        """Process a ``PushedMessage`` delivered over ``FeishuWebFrontierListener``."""
        cfg = await self._service.aget_config()
        rt_cfg = self._realtime_router.resolve_config()
        if (not cfg.enabled and not rt_cfg.enabled) or cfg.send_mode == "openapi":
            return False
        user_client = self._get_or_build_user_client(cfg)
        if (
            hasattr(user_client, "should_use_web_session")
            and not user_client.should_use_web_session()
        ):
            return False
        wt = (
            user_client._get_web_transport() if hasattr(user_client, "_get_web_transport") else None
        )
        if wt is None:
            return False
        if self.frontier_running:
            self._frontier_connected = True
            self._last_frontier_error = None
        user_msg = wt.ingest_pushed_message(pushed)
        if user_msg is None:
            return False
        is_owner = bool(
            user_msg.sender_type == "user"
            and self._service._matches_owner_sender(user_msg.sender_open_id, cfg)
        )
        if cfg.enabled and self._service.is_owner_ai_command_candidate(
            sender_open_id=user_msg.sender_open_id,
            sender_type=user_msg.sender_type,
            text=user_msg.text,
            config=cfg,
        ):
            self._commands_detected += 1
            self._service.enqueue_pending_command(user_msg, source="web_frontier")
            self._schedule_durable_pending_queue()
            return True

        owner_ids = self._resolve_owner_ids(cfg, wt)
        if (
            is_owner
            or user_msg.sender_open_id in owner_ids
            or getattr(pushed.message, "from_id", "") in owner_ids
        ):
            self._realtime_router.record_owner_pushed_message(
                pushed,
                user_msg,
                web_transport=wt,
            )
            return False

        if background_realtime:
            self._enqueue_realtime_event(
                pushed,
                user_msg,
                web_transport=wt,
                user_client=user_client,
                owner_ids=owner_ids,
                is_owner_sender=False,
            )
            return True

        return await self._realtime_router.handle_pushed_message(
            pushed,
            user_msg,
            web_transport=wt,
            user_client=user_client,
            owner_ids=owner_ids,
            is_owner_sender=False,
        )

    def _ensure_frontier_task(self, cfg: Any) -> None:
        if self._stopping or not cfg.enabled or cfg.send_mode == "openapi":
            if self._frontier_task is not None and not self._frontier_task.done():
                self._frontier_stop.set()
                self._frontier_task.cancel()
            self._frontier_connected = False
            return
        user_client = self._get_or_build_user_client(cfg)
        if (
            hasattr(user_client, "should_use_web_session")
            and not user_client.should_use_web_session()
        ):
            if self._frontier_task is not None and not self._frontier_task.done():
                self._frontier_stop.set()
                self._frontier_task.cancel()
            self._frontier_connected = False
            return
        wt = (
            user_client._get_web_transport() if hasattr(user_client, "_get_web_transport") else None
        )
        if wt is None:
            if self._frontier_task is not None and not self._frontier_task.done():
                self._frontier_stop.set()
                self._frontier_task.cancel()
            self._frontier_connected = False
            return
        if self._frontier_task is None or self._frontier_task.done():
            self._frontier_stop = asyncio.Event()
            try:
                listener = wt.create_frontier_listener(
                    self._handle_frontier_pushed_web_message,
                    on_state=self._on_frontier_state,
                )
            except TypeError:
                listener = wt.create_frontier_listener(self._handle_frontier_pushed_web_message)
            self._frontier_task = asyncio.create_task(
                listener.run(stop_event=self._frontier_stop),
                name="feishu-web-frontier",
            )

    async def start(self) -> None:
        self._stopping = False
        if self._task is None or self._task.done():
            self._wake_event = asyncio.Event()
            self._task = asyncio.create_task(self._run_loop(), name="feishu-ai-poller")

    async def reconcile(self) -> None:
        """Wake the poller loop immediately after a configuration or token change."""
        cfg = self._service.get_config(auto_import_lark_cli=False)
        if cfg.enabled and not self.running and not self._stopping:
            await self.start()
        self._ensure_frontier_task(cfg)
        self._wake_event.set()

    async def wait_for_inflight(self, timeout: float = 5.0) -> None:
        """Wait for currently scheduled background command workers to finish."""
        if not self._worker_tasks:
            return
        pending = list(self._worker_tasks)
        await asyncio.wait(pending, timeout=max(0.01, timeout))

    async def quiesce(self) -> None:
        self._stopping = True
        self._frontier_connected = False
        self._wake_event.set()
        self._frontier_stop.set()
        if self._frontier_task is not None and not self._frontier_task.done():
            self._frontier_task.cancel()
            try:
                await self._frontier_task
            except asyncio.CancelledError:
                pass
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._worker_tasks:
            for worker in list(self._worker_tasks):
                worker.cancel()
            await asyncio.gather(*list(self._worker_tasks), return_exceptions=True)
            self._worker_tasks.clear()
            self._in_flight_ids.clear()
        self._realtime_event_queue.clear()
        self._realtime_queue_worker = None
        self._cached_user_client = None
        self._cached_client_key = None

    async def stop(self) -> None:
        await self.quiesce()
        self._task = None
        self._frontier_task = None

    async def _run_loop(self) -> None:
        while not self._stopping:
            interval = 15.0
            try:
                cfg = await self._service.aget_config()
                interval = max(2.0, cfg.poll_interval_seconds)
                self._ensure_frontier_task(cfg)
                if cfg.enabled:
                    await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc) or type(exc).__name__
                logger.warning("Feishu /ai poller tick failed: %s", self._last_error)
                try:
                    raw_cfg = self._service.get_config(auto_import_lark_cli=False)
                    interval = max(2.0, raw_cfg.poll_interval_seconds)
                except Exception:
                    interval = 2.0
            self._wake_event.clear()
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def _spawn_command_worker(
        self,
        msg: FeishuUserMessage,
        *,
        source: str,
        seeded_context: list[FeishuUserMessage] | None,
    ) -> asyncio.Task[None] | None:
        if msg.message_id in self._in_flight_ids:
            return None
        self._in_flight_ids.add(msg.message_id)

        async def _runner() -> None:
            started_dispatch = False
            completed_terminal = False
            try:
                async with self._dispatch_sem:
                    started_dispatch = True
                    self._service.mark_pending_command_state(msg.message_id, "in_flight")
                    result = await self._service.handle_message(
                        msg,
                        source=source,
                        seeded_context=seeded_context,
                    )
                completed_terminal = True
                if result.handled:
                    if result.status == "completed":
                        self._commands_succeeded += 1
                    elif result.status in {"failed", "scope_blocked"}:
                        self._commands_failed += 1
            except asyncio.CancelledError:
                if started_dispatch:
                    self._service.mark_pending_command_state(msg.message_id, "interrupted")
                raise
            except Exception:
                completed_terminal = True
                self._commands_failed += 1
                logger.warning(
                    "Feishu /ai background command execution failed for %s",
                    msg.message_id,
                    exc_info=True,
                )
            finally:
                if completed_terminal:
                    self._service.dequeue_pending_command(msg.message_id)
                self._in_flight_ids.discard(msg.message_id)

        task = asyncio.create_task(_runner(), name=f"feishu-ai-cmd-{msg.message_id}")
        self._worker_tasks.add(task)
        task.add_done_callback(self._worker_tasks.discard)
        return task

    def _schedule_durable_pending_queue(
        self,
    ) -> tuple[list[asyncio.Task[None]], list[str]]:
        spawned: list[asyncio.Task[None]] = []
        scheduled_ids: list[str] = []
        entries = self._service.list_pending_commands()
        # Dispatch proven-unstarted (`queued`) jobs before interrupted in-flight jobs
        ordered = sorted(
            enumerate(entries),
            key=lambda pair: (
                0 if str(pair[1].get("state") or "queued") == "queued" else 1,
                pair[0],
            ),
        )
        for _, entry in ordered:
            raw_msg = entry.get("message")
            if not isinstance(raw_msg, Mapping):
                continue
            msg = _message_from_dict(raw_msg)
            if msg is None or msg.message_id in self._in_flight_ids:
                continue
            cmd_state = str(entry.get("state") or "queued")
            if cmd_state in {"in_flight", "interrupted"}:
                if not self._service.reconcile_interrupted_command(msg.message_id):
                    continue
            raw_ctx = entry.get("seeded_context")
            seeded: list[FeishuUserMessage] | None = None
            if isinstance(raw_ctx, list):
                seeded = [
                    parsed
                    for item in raw_ctx
                    if isinstance(item, Mapping)
                    for parsed in (_message_from_dict(item),)
                    if parsed is not None
                ]
            source = str(entry.get("source") or "poller")
            task = self._spawn_command_worker(msg, source=source, seeded_context=seeded)
            if task is not None:
                spawned.append(task)
                scheduled_ids.append(msg.message_id)
        return spawned, scheduled_ids

    async def _collect_unprocessed_chat_messages(
        self,
        user_client: FeishuUserClient,
        chat_id: str,
        *,
        watermark_ms: int,
        cfg: FeishuAiCommandConfig,
        resume_cursor: str | None = None,
        resume_upper_ms: int = 0,
    ) -> tuple[list[FeishuUserMessage], bool, str | None, int]:
        """Page backwards from newest (or ``resume_cursor``) until ``watermark_ms`` is reached.

        Returns ``(messages, reached_watermark, next_cursor, scan_upper_ms)``.
        """
        page_size = min(50, max(20, cfg.max_context_messages))
        collected: list[FeishuUserMessage] = []
        seen_ids: set[str] = set()
        seen_tokens: set[str] = set()
        cursor: str | None = resume_cursor or None
        if cursor:
            seen_tokens.add(cursor)
        reached_watermark = False
        next_cursor: str | None = None
        scan_upper_ms = max(watermark_ms, int(resume_upper_ms or 0))

        for _ in range(self._max_pages_per_chat):
            page = await user_client.list_messages_page(
                container_id_type="chat",
                container_id=chat_id,
                sort_type="ByCreateTimeDesc",
                page_size=page_size,
                page_token=cursor,
            )
            for item in page.items:
                if item.create_time_ms > scan_upper_ms:
                    scan_upper_ms = item.create_time_ms
                if item.message_id not in seen_ids:
                    seen_ids.add(item.message_id)
                    collected.append(item)
            if not page.items:
                reached_watermark = True
                next_cursor = None
                break
            oldest_on_page = min(m.create_time_ms for m in page.items)
            if oldest_on_page <= watermark_ms:
                reached_watermark = True
                next_cursor = None
                break
            if not page.has_more or not page.page_token or page.page_token in seen_tokens:
                reached_watermark = True
                next_cursor = None
                break
            seen_tokens.add(page.page_token)
            cursor = page.page_token
            next_cursor = page.page_token

        return collected, reached_watermark, next_cursor, scan_upper_ms

    def _remember_search_message(self, message_id: str, fetched: FeishuUserMessage | None) -> None:
        self._search_msg_cache[message_id] = fetched
        self._search_msg_cache.move_to_end(message_id)
        while len(self._search_msg_cache) > _MAX_SEARCH_MESSAGE_CACHE:
            self._search_msg_cache.popitem(last=False)

    async def _fetch_search_page(
        self,
        user_client: FeishuUserClient,
        query: str,
        *,
        page_size: int,
        page_token: str | None,
        start_time: str,
        owner_open_id: str,
    ) -> FeishuMessageSearchPage:
        if hasattr(user_client, "search_messages_page"):
            return await user_client.search_messages_page(
                query,
                page_size=page_size,
                page_token=page_token,
                start_time=start_time,
                from_ids=[owner_open_id],
            )
        kwargs: dict[str, Any] = {
            "page_size": page_size,
            "start_time": start_time,
            "from_ids": [owner_open_id],
        }
        if page_token:
            kwargs["page_token"] = page_token
        try:
            items = await user_client.search_messages(query, **kwargs)
        except TypeError:
            kwargs.pop("page_token", None)
            try:
                items = await user_client.search_messages(query, **kwargs)
            except TypeError:
                items = await user_client.search_messages(
                    query,
                    page_size=page_size,
                    start_time=start_time,
                )
        return FeishuMessageSearchPage(items=tuple(items or ()), has_more=False, page_token=None)

    async def _discover_recent_owner_command_chats(
        self,
        user_client: FeishuUserClient,
        *,
        cfg: FeishuAiCommandConfig,
        owner_open_id: str,
        now_ms: int,
        known_chat_ids: set[str],
    ) -> list[str]:
        """Supplement active-chat polling with a bounded owner ``/ai`` message search.

        Already-queued, in-flight, completed, non-owner, and watermarked hits are
        skipped BEFORE consuming the per-tick ``get_message`` / chat-discovery
        budget, and search paginates across pages when earlier hits are settled.
        """
        has_search = hasattr(user_client, "search_messages_page") or hasattr(
            user_client, "search_messages"
        )
        if not has_search or not hasattr(user_client, "get_message"):
            return []
        query = cfg.prefix.lstrip("/").strip() or "ai"
        lookback_sec = max(60, int(self._initial_lookback_ms / 1000))
        start_time = str(max(0, int(now_ms / 1000) - lookback_sec))
        max_discover = max(10, min(20, cfg.max_active_chats))
        max_fetches = max_discover
        page_size = min(50, max(20, max_discover * 2))

        seen_msg_ids = {
            str(entry["message"]["message_id"])
            for entry in self._service.list_pending_commands()
            if isinstance(entry.get("message"), Mapping) and entry["message"].get("message_id")
        } | {
            str(run["message_id"])
            for run in self._service.get_recent_runs()
            if isinstance(run, Mapping) and run.get("message_id")
        }
        watermarks = self._service.get_watermarks()

        discovered: list[str] = []
        fetches = 0
        cursor: str | None = None
        seen_tokens: set[str] = set()

        try:
            async with asyncio.timeout(8.0):
                for _ in range(_MAX_SEARCH_PAGES_PER_POLL):
                    try:
                        async with asyncio.timeout(5.0):
                            page = await self._fetch_search_page(
                                user_client,
                                query,
                                page_size=page_size,
                                page_token=cursor,
                                start_time=start_time,
                                owner_open_id=owner_open_id,
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.debug(
                            "Feishu /ai message search supplement unavailable",
                            exc_info=True,
                        )
                        break

                    if not page.items:
                        break

                    for mid in page.items:
                        if mid in seen_msg_ids or mid in self._in_flight_ids:
                            continue
                        if mid in self._search_msg_cache:
                            fetched = self._search_msg_cache[mid]
                            self._search_msg_cache.move_to_end(mid)
                        else:
                            if fetches >= max_fetches:
                                break
                            fetches += 1
                            try:
                                async with asyncio.timeout(3.0):
                                    fetched = await user_client.get_message(mid)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                continue
                            self._remember_search_message(mid, fetched)

                        if fetched is None or not fetched.chat_id:
                            continue
                        if fetched.chat_id in known_chat_ids or fetched.chat_id in discovered:
                            continue
                        wm = watermarks.get(fetched.chat_id)
                        if (
                            wm is not None
                            and fetched.create_time_ms > 0
                            and fetched.create_time_ms <= wm
                        ):
                            continue
                        if fetched.sender_type == "user" and self._service._matches_owner_sender(
                            fetched.sender_open_id, cfg
                        ):
                            discovered.append(fetched.chat_id)
                            if len(discovered) >= max_discover:
                                break

                    if len(discovered) >= max_discover or fetches >= max_fetches:
                        break
                    if not page.has_more or not page.page_token or page.page_token in seen_tokens:
                        break
                    seen_tokens.add(page.page_token)
                    cursor = page.page_token
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.debug("Feishu /ai message search supplement timed out", exc_info=True)
        return discovered

    async def poll_once(self) -> list[str]:
        """Execute one polling pass across watched or active chats.

        Pages through each chat's unprocessed interval down to its watermark,
        durably enqueues owner ``/ai`` commands before advancing the watermark,
        and dispatches execution in background tasks so long-running turns never
        block intake across chats.
        """
        try:
            cfg = await self._service.aget_config()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = str(exc) or type(exc).__name__
            raise
        if not cfg.enabled:
            return []

        user_client = self._service.build_user_client(cfg)
        try:
            if hasattr(user_client, "aresolve_record"):
                record = await user_client.aresolve_record()
            else:
                async with self._service._token_store.async_file_lock(
                    app_id=cfg.app_id, open_id=cfg.owner_open_id
                ):
                    record = user_client.resolve_record()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = str(exc) or type(exc).__name__
            raise
        self._poll_count += 1
        self._last_poll_at = time.time()
        if record is None:
            self._last_error = "no_user_token"
            return []

        owner_open_id = (cfg.owner_open_id or record.open_id).strip()
        if not owner_open_id:
            self._last_error = "no_owner_open_id"
            return []

        spawned_tasks, processed_ids = self._schedule_durable_pending_queue()
        if len(self._service.list_pending_commands()) >= 256:
            if spawned_tasks:
                await asyncio.wait(spawned_tasks, timeout=0.05)
            self._last_error = "pending_queue_backpressure"
            return processed_ids

        watermarks = self._service.get_watermarks()
        now_ms = int(time.time() * 1000)

        chat_ids: list[str] = list(cfg.watch_chats)
        if not chat_ids:
            chats = await user_client.list_chats(
                sort_type="ByActiveTimeDesc",
                page_size=cfg.max_active_chats,
            )
            chat_ids = [c.chat_id for c in chats if c.chat_id]
            extra_chats = await self._discover_recent_owner_command_chats(
                user_client,
                cfg=cfg,
                owner_open_id=owner_open_id,
                now_ms=now_ms,
                known_chat_ids=set(chat_ids),
            )
            for extra_chat_id in extra_chats:
                if extra_chat_id not in chat_ids:
                    chat_ids.append(extra_chat_id)

        last_chat_error: str | None = None
        for chat_id in chat_ids:
            watermark_ms = watermarks.get(chat_id)
            if watermark_ms is None:
                watermark_ms = max(0, now_ms - self._initial_lookback_ms)
                self._service.set_watermark(chat_id, watermark_ms)

            saved_scan = self._service.get_scan_cursor(chat_id)
            resume_cursor: str | None = None
            resume_upper_ms = watermark_ms
            if (
                isinstance(saved_scan, Mapping)
                and int(saved_scan.get("watermark_ms") or 0) == watermark_ms
            ):
                raw_cur = saved_scan.get("cursor")
                if isinstance(raw_cur, str) and raw_cur:
                    resume_cursor = raw_cur
                resume_upper_ms = max(watermark_ms, int(saved_scan.get("upper_ms") or 0))

            try:
                (
                    messages,
                    _reached,
                    next_cursor,
                    scan_upper_ms,
                ) = await self._collect_unprocessed_chat_messages(
                    user_client,
                    chat_id,
                    watermark_ms=watermark_ms,
                    cfg=cfg,
                    resume_cursor=resume_cursor,
                    resume_upper_ms=resume_upper_ms,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_chat_error = f"{chat_id}: {exc}"
                logger.warning("Feishu /ai poller could not read chat %s: %s", chat_id, exc)
                continue
            if not messages:
                if _reached:
                    self._service.set_scan_cursor(chat_id, None)
                    if scan_upper_ms > watermark_ms:
                        self._service.set_watermark(chat_id, scan_upper_ms)
                continue

            # Process in chronological order (oldest -> newest above watermark)
            ascending = sorted(messages, key=lambda item: (item.create_time_ms, item.message_id))
            max_seen_ms = scan_upper_ms
            for idx, msg in enumerate(ascending):
                if msg.create_time_ms > max_seen_ms:
                    max_seen_ms = msg.create_time_ms
                if msg.create_time_ms <= watermark_ms:
                    self._skipped_count += 1
                    continue
                if msg.sender_type != "user" or not self._service._matches_owner_sender(
                    msg.sender_open_id, cfg
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

                seeded: list[FeishuUserMessage] | None = (
                    ascending[max(0, idx + 1 - cfg.max_context_messages) : idx + 1]
                    if not msg.thread_id
                    else None
                )
                enqueued = self._service.enqueue_pending_command(
                    msg,
                    source="poller",
                    seeded_context=seeded,
                )
                if enqueued or msg.message_id not in self._in_flight_ids:
                    if enqueued:
                        self._commands_detected += 1
                    if msg.message_id not in processed_ids:
                        processed_ids.append(msg.message_id)
                    task = self._spawn_command_worker(
                        msg,
                        source="poller",
                        seeded_context=seeded,
                    )
                    if task is not None:
                        spawned_tasks.append(task)

            if _reached:
                self._service.set_scan_cursor(chat_id, None)
                if max_seen_ms > watermark_ms:
                    self._service.set_watermark(chat_id, max_seen_ms)
            elif next_cursor:
                self._service.set_scan_cursor(
                    chat_id,
                    {
                        "cursor": next_cursor,
                        "upper_ms": max_seen_ms,
                        "watermark_ms": watermark_ms,
                    },
                )

        if spawned_tasks:
            # Give fast dispatchers a short bounded yield while leaving slow turns in background
            await asyncio.wait(spawned_tasks, timeout=0.05)

        self._last_error = last_chat_error
        return processed_ids


_DEFAULT_AI_POLLER: FeishuAiPoller | None = None


def get_feishu_ai_poller() -> FeishuAiPoller:
    global _DEFAULT_AI_POLLER
    if _DEFAULT_AI_POLLER is None:
        _DEFAULT_AI_POLLER = FeishuAiPoller()
    return _DEFAULT_AI_POLLER
