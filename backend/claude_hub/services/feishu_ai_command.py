"""Feishu personal ``/ai <instruction>`` command pipeline and configuration store."""

from __future__ import annotations

import asyncio
import hmac
import inspect
import logging
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from claude_hub.config import settings
from claude_hub.models import AgentType, SessionKind
from claude_hub.services.feishu_ai_command_models import (
    _MASO_MODEL_VARIANTS,
    _MAX_PENDING_QUEUE,
    _MAX_RECENT_RUNS,
    _REACTION_TIMEOUT_SECONDS,
    _STATE_VERSION,
    DEFAULT_AI_COMMAND_ACK_EMOJI,
    DEFAULT_AI_COMMAND_DONE_EMOJI,
    DEFAULT_AI_COMMAND_FOOTER,
    DEFAULT_AI_COMMAND_MODEL,
    DEFAULT_AI_COMMAND_PREFIX,
    DEFAULT_AI_COMMAND_PROVIDER,
    DEFAULT_AI_COMMAND_REASONING_EFFORT,
    DEFAULT_MAX_ACTIVE_CHATS,
    DEFAULT_MAX_CONTEXT_MESSAGES,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_PROVIDER_MODELS,
    PROVIDER_SUPPORTED_REASONING_EFFORTS,
    SUPPORTED_AI_COMMAND_PROVIDERS,
    SUPPORTED_REASONING_EFFORTS,
    AiCommandContextRefs,
    AiCommandExecutionResult,
    FeishuAiCommandConfig,
    ParsedAiCommand,
    bounded_message_uuid,
    build_ai_dispatch_prompt,
    build_ai_post_payload,
    build_interim_reply_text,
    ensure_ai_footer,
    extract_context_references,
    parse_ai_command,
    resolve_ai_command_agent_env,
    validate_ai_command_agent_config,
)
from claude_hub.services.feishu_bot import (
    FeishuMessageDedupStore,
    FeishuMessageEvent,
    FeishuReplyReceipt,
)
from claude_hub.services.feishu_user_client import (
    FeishuScopePreflightResult,
    FeishuUserClient,
    FeishuUserMessage,
    FeishuUserScopeError,
)
from claude_hub.services.feishu_user_token_store import (
    FeishuUserTokenStore,
    get_feishu_user_token_store,
)
from claude_hub.services.local_storage import (
    document_exists,
    read_document,
    write_document,
)
from claude_hub.services.runtime_isolation import resolve_runtime_home

logger = logging.getLogger(__name__)

AiTurnDispatcher = Callable[[str, str, str, str, Mapping[str, Any]], Awaitable[str]]


def _recover_completed_stream_answer(tab_id: str, turn_id: str) -> tuple[str, str | None]:
    """Inspect durable AgentStreamStore evidence for ``(tab_id, turn_id)`` without starting a runtime."""
    if not tab_id or not turn_id:
        return "absent", None
    try:
        from claude_hub.models import AgentStreamEventType
        from claude_hub.services.agent_stream.store import AgentStreamStore

        store = AgentStreamStore("terminal-tabs", f"terminal-tab-{tab_id}")
        events = store._read_rows(-1, None, turn_id)
    except Exception:
        return "unknown", None
    started = False
    for event in events:
        if event.turn_id != turn_id:
            continue
        if event.type == AgentStreamEventType.TURN_STARTED:
            started = True
        elif event.type == AgentStreamEventType.TURN_COMPLETED:
            if event.payload.get("delivery_rejected"):
                started = True
                continue
            status = event.payload.get("status")
            if status in {None, "completed", "success", "ok"}:
                text = str(
                    event.payload.get("final_text") or event.payload.get("assistant_text") or ""
                ).strip()
                return "completed", (text or None)
            return ("cancelled" if status in {"cancelled", "canceled"} else "failed"), None
    return ("started" if started else "absent"), None


class FeishuAiCommandService:
    """Coordinates ``/ai`` configuration, deduplication, context fetch, reactions, and replies."""

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        token_store: FeishuUserTokenStore | None = None,
        dedup_store: FeishuMessageDedupStore | None = None,
        user_client_factory: Callable[[FeishuAiCommandConfig], FeishuUserClient] | None = None,
        tab_creator: Callable[..., Any] | None = None,
        tab_getter: Callable[[str], Any] | None = None,
        tab_env_switcher: Callable[[str, dict[str, str]], Any] | None = None,
        turn_dispatcher: AiTurnDispatcher | None = None,
    ) -> None:
        runtime_home = resolve_runtime_home()
        self._state_path: Path = state_path or (runtime_home / "feishu_ai_command.json")
        self._token_store: FeishuUserTokenStore = token_store or get_feishu_user_token_store()
        self._dedup: FeishuMessageDedupStore = dedup_store or FeishuMessageDedupStore()
        self._user_client_factory = user_client_factory
        self._tab_creator = tab_creator
        self._tab_getter = tab_getter
        self._tab_env_switcher = tab_env_switcher
        self._turn_dispatcher: AiTurnDispatcher | None = turn_dispatcher
        self._lock = threading.RLock()
        self._channel_locks: dict[str, asyncio.Lock] = {}

    def _load_state(self) -> dict[str, Any]:
        default_doc: dict[str, Any] = {
            "version": _STATE_VERSION,
            "config": FeishuAiCommandConfig().to_dict(),
            "watermarks": {},
            "scan_cursors": {},
            "channel_tabs": {},
            "recent_runs": [],
            "pending_queue": [],
        }
        if not document_exists(self._state_path):
            return default_doc
        try:
            raw = read_document(self._state_path)
        except (FileNotFoundError, ValueError, OSError):
            return default_doc
        return dict(raw) if isinstance(raw, Mapping) else default_doc

    def _save_state(self, state: Mapping[str, Any]) -> None:
        write_document(self._state_path, dict(state))

    def is_enabled(self) -> bool:
        """Fast check whether /ai command is enabled without touching token storage."""
        with self._lock:
            if not document_exists(self._state_path):
                return False
            raw_cfg = self._load_state().get("config")
            return bool(isinstance(raw_cfg, Mapping) and raw_cfg.get("enabled"))

    def get_config(self, *, auto_import_lark_cli: bool | None = None) -> FeishuAiCommandConfig:
        with self._lock:
            raw_cfg = self._load_state().get("config")
            cfg = (
                FeishuAiCommandConfig.from_mapping(raw_cfg)
                if isinstance(raw_cfg, Mapping)
                else FeishuAiCommandConfig()
            )
            should_auto = cfg.enabled if auto_import_lark_cli is None else auto_import_lark_cli
            if not cfg.owner_open_id or not cfg.app_id:
                record = self._token_store.resolve_token(
                    open_id=cfg.owner_open_id or None,
                    app_id=cfg.app_id or None,
                    auto_import_lark_cli=should_auto,
                )
                if record is not None:
                    cfg = replace(
                        cfg,
                        owner_open_id=cfg.owner_open_id or record.open_id,
                        app_id=cfg.app_id or record.app_id,
                    )
            return cfg

    def _read_persisted_config(self) -> FeishuAiCommandConfig:
        with self._lock:
            raw_cfg = self._load_state().get("config")
            return (
                FeishuAiCommandConfig.from_mapping(raw_cfg)
                if isinstance(raw_cfg, Mapping)
                else FeishuAiCommandConfig()
            )

    async def aget_config(
        self, *, auto_import_lark_cli: bool | None = None
    ) -> FeishuAiCommandConfig:
        cfg = self._read_persisted_config()
        should_auto = cfg.enabled if auto_import_lark_cli is None else auto_import_lark_cli
        if not cfg.owner_open_id or not cfg.app_id:
            record = await self._token_store.aresolve_token(
                open_id=cfg.owner_open_id or None,
                app_id=cfg.app_id or None,
                auto_import_lark_cli=should_auto,
            )
            latest_cfg = self._read_persisted_config()
            if latest_cfg != cfg:
                cfg = latest_cfg
                if cfg.owner_open_id and cfg.app_id:
                    return cfg
                should_auto = cfg.enabled if auto_import_lark_cli is None else auto_import_lark_cli
                record = await self._token_store.aresolve_token(
                    open_id=cfg.owner_open_id or None,
                    app_id=cfg.app_id or None,
                    auto_import_lark_cli=should_auto,
                )
            if (
                record is not None
                and (not cfg.app_id or record.app_id == cfg.app_id)
                and (not cfg.owner_open_id or record.open_id == cfg.owner_open_id)
            ):
                cfg = replace(
                    cfg,
                    owner_open_id=cfg.owner_open_id or record.open_id,
                    app_id=cfg.app_id or record.app_id,
                )
        return cfg

    def update_config(self, patch: Mapping[str, Any]) -> FeishuAiCommandConfig:
        with self._lock:
            state = self._load_state()
            current_raw = state.get("config")
            merged: dict[str, Any] = (
                dict(current_raw)
                if isinstance(current_raw, Mapping)
                else FeishuAiCommandConfig().to_dict()
            )
            old_provider = str(merged.get("provider") or DEFAULT_AI_COMMAND_PROVIDER.value)
            for key, value in patch.items():
                if value is not None:
                    merged[key] = value
            new_provider = str(merged.get("provider") or old_provider).strip().lower()
            if "provider" in patch and patch["provider"] is not None:
                if "model" not in patch or patch["model"] is None:
                    if new_provider != old_provider.strip().lower():
                        defaults = DEFAULT_PROVIDER_MODELS.get(new_provider, ())
                        if defaults:
                            merged["model"] = defaults[0]
                if "reasoning_effort" not in patch or patch["reasoning_effort"] is None:
                    supported = PROVIDER_SUPPORTED_REASONING_EFFORTS.get(new_provider, ())
                    if not supported:
                        merged["reasoning_effort"] = ""
                    elif str(merged.get("reasoning_effort") or "") not in supported:
                        merged["reasoning_effort"] = "high" if "high" in supported else supported[0]
            updated = FeishuAiCommandConfig.from_mapping(merged)
            state["config"] = updated.to_dict()
            self._save_state(state)
            return updated

    def enqueue_pending_command(
        self,
        message: FeishuUserMessage,
        *,
        source: str = "poller",
        seeded_context: Sequence[FeishuUserMessage] | None = None,
    ) -> bool:
        """Durably record an accepted ``/ai`` command before advancing the poller watermark."""
        with self._lock:
            state = self._load_state()
            queue = [dict(x) for x in (state.get("pending_queue") or []) if isinstance(x, Mapping)]
            mid = message.message_id
            if any(
                isinstance(x.get("message"), Mapping) and x["message"].get("message_id") == mid
                for x in queue
            ):
                return False
            if any(
                isinstance(r, Mapping) and r.get("message_id") == mid
                for r in (state.get("recent_runs") or [])
            ):
                return False
            seeded = [m.to_dict() for m in seeded_context] if seeded_context is not None else None
            queue.append(
                {
                    "message": message.to_dict(),
                    "source": source,
                    "seeded_context": seeded,
                    "state": "queued",
                    "enqueued_at": time.time(),
                }
            )
            state["pending_queue"] = queue
            self._save_state(state)
            return True

    def get_pending_command(self, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            for item in self._load_state().get("pending_queue") or []:
                if isinstance(item, Mapping):
                    msg = item.get("message")
                    if isinstance(msg, Mapping) and msg.get("message_id") == message_id:
                        return dict(item)
            return None

    def mark_pending_command_state(self, message_id: str, cmd_state: str, **fields: Any) -> None:
        """Update lifecycle state and durable progress fields of a pending command."""
        with self._lock:
            state = self._load_state()
            changed = False
            queue: list[dict[str, Any]] = []
            for item in state.get("pending_queue") or []:
                if not isinstance(item, Mapping):
                    continue
                row = dict(item)
                msg = row.get("message")
                if isinstance(msg, Mapping) and msg.get("message_id") == message_id:
                    if row.get("state") != cmd_state or any(
                        row.get(k) != v for k, v in fields.items()
                    ):
                        row["state"] = cmd_state
                        row.update(fields)
                        changed = True
                queue.append(row)
            if changed:
                state["pending_queue"] = queue
                self._save_state(state)

    def reconcile_interrupted_command(self, message_id: str) -> bool:
        """Reconcile an interrupted command using durable dispatch/output evidence."""
        with self._lock:
            state = self._load_state()
            if any(
                isinstance(r, Mapping) and r.get("message_id") == message_id
                for r in (state.get("recent_runs") or [])
            ):
                self.dequeue_pending_command(message_id)
                return False
            item = self.get_pending_command(message_id)
            if item is None:
                return False
            cfg = FeishuAiCommandConfig.from_mapping(state.get("config") or {})
            dedup_key = f"ai_cmd:{cfg.owner_open_id or 'owner'}:{message_id}"
            dispatch_state = str(item.get("dispatch_state") or "not_started")
            if dispatch_state in {"not_started", "retired_retry_safe"}:
                self._dedup.release(dedup_key)
                self.mark_pending_command_state(message_id, "queued", dispatch_state="not_started")
                return True
            if dispatch_state == "turn_completed" and isinstance(item.get("answer"), str):
                self.mark_pending_command_state(message_id, "queued")
                return True
            tab_id = str(item.get("tab_id") or "")
            turn_id = str(item.get("turn_id") or f"feishu-ai-{message_id}")
            ev_state, recovered_answer = _recover_completed_stream_answer(tab_id, turn_id)
            if ev_state == "completed" and recovered_answer:
                self.mark_pending_command_state(
                    message_id,
                    "queued",
                    dispatch_state="turn_completed",
                    answer=recovered_answer,
                )
                return True
            # Turn retirement is uncertain: preserve the dedup claim and do not replay.
            self._dedup.finish(
                dedup_key,
                "interrupted_uncertain",
                stage="ai_command",
                reason=f"uncertain_turn_retirement:{ev_state}",
            )
            raw_msg = item.get("message")
            chat_id = str(raw_msg.get("chat_id") or "") if isinstance(raw_msg, Mapping) else ""
            thread_id = raw_msg.get("thread_id") if isinstance(raw_msg, Mapping) else None
            runs = list(state.get("recent_runs") or [])
            runs.insert(
                0,
                {
                    "message_id": message_id,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "status": "interrupted_uncertain",
                    "reason": f"uncertain_turn_retirement:{ev_state}",
                    "provider": cfg.provider.value,
                    "model": cfg.model,
                    "reasoning_effort": cfg.reasoning_effort,
                    "refs": dict(item.get("refs") or {}),
                    "tab_id": tab_id or None,
                    "source": str(item.get("source") or "poller"),
                    "at": time.time(),
                },
            )
            state["recent_runs"] = runs[:_MAX_RECENT_RUNS]
            self._save_state(state)
            self.dequeue_pending_command(message_id)
            return False

    def dequeue_pending_command(self, message_id: str) -> None:
        """Remove a completed/handled command from the durable pending queue."""
        with self._lock:
            state = self._load_state()
            state["pending_queue"] = [
                dict(x)
                for x in (state.get("pending_queue") or [])
                if isinstance(x, Mapping)
                and not (
                    isinstance(x.get("message"), Mapping)
                    and x["message"].get("message_id") == message_id
                )
            ]
            self._save_state(state)

    def list_pending_commands(self) -> list[dict[str, Any]]:
        """Return all durably enqueued ``/ai`` commands awaiting execution."""
        with self._lock:
            return [
                dict(x)
                for x in (self._load_state().get("pending_queue") or [])
                if isinstance(x, Mapping)
            ]

    def get_scan_cursor(self, chat_id: str) -> dict[str, Any] | None:
        with self._lock:
            cursors = self._load_state().get("scan_cursors")
            raw = cursors.get(chat_id) if isinstance(cursors, Mapping) else None
            return dict(raw) if isinstance(raw, Mapping) else None

    def set_scan_cursor(self, chat_id: str, cursor_state: Mapping[str, Any] | None) -> None:
        with self._lock:
            state = self._load_state()
            cursors = dict(state.get("scan_cursors") or {})
            if cursor_state is None:
                if chat_id not in cursors:
                    return
                cursors.pop(chat_id, None)
            else:
                cursors[chat_id] = dict(cursor_state)
            state["scan_cursors"] = cursors
            self._save_state(state)

    def get_watermarks(self) -> dict[str, int]:
        with self._lock:
            wm = self._load_state().get("watermarks")
            return (
                {
                    k: int(v)
                    for k, v in wm.items()
                    if isinstance(k, str) and isinstance(v, (int, float))
                }
                if isinstance(wm, Mapping)
                else {}
            )

    def set_watermark(self, chat_id: str, create_time_ms: int) -> None:
        with self._lock:
            state = self._load_state()
            wm = dict(state.get("watermarks") or {})
            if create_time_ms > int(wm.get(chat_id) or 0):
                wm[chat_id] = int(create_time_ms)
                state["watermarks"] = wm
                self._save_state(state)

    def get_recent_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            runs = self._load_state().get("recent_runs")
            return list(runs) if isinstance(runs, list) else []

    def _record_run(self, result: AiCommandExecutionResult, *, source: str) -> None:
        with self._lock:
            state = self._load_state()
            runs = list(state.get("recent_runs") or [])
            d = result.to_dict()
            keys = "message_id chat_id thread_id status reason provider model reasoning_effort refs tab_id".split()
            runs.insert(0, {k: d[k] for k in keys} | {"source": source, "at": time.time()})
            state["recent_runs"] = runs[:_MAX_RECENT_RUNS]
            self._save_state(state)

    def build_user_client(self, config: FeishuAiCommandConfig | None = None) -> FeishuUserClient:
        cfg = config or self.get_config()
        if self._user_client_factory is not None:
            return self._user_client_factory(cfg)
        return FeishuUserClient(
            self._token_store,
            open_id=cfg.owner_open_id or None,
            app_id=cfg.app_id or settings.feishu_app_id or None,
        )

    def preflight_status(
        self,
        *,
        auto_import_lark_cli: bool = True,
        config: FeishuAiCommandConfig | None = None,
    ) -> FeishuScopePreflightResult:
        cfg = config or self.get_config(auto_import_lark_cli=auto_import_lark_cli)
        rec = self._token_store.resolve_token(
            open_id=cfg.owner_open_id or None,
            app_id=cfg.app_id or None,
            auto_import_lark_cli=auto_import_lark_cli,
        )
        return self.build_user_client(cfg).preflight_scopes(rec)

    async def apreflight_status(
        self,
        *,
        auto_import_lark_cli: bool = True,
        config: FeishuAiCommandConfig | None = None,
    ) -> FeishuScopePreflightResult:
        cfg = config or await self.aget_config(auto_import_lark_cli=auto_import_lark_cli)
        rec = await self._token_store.aresolve_token(
            open_id=cfg.owner_open_id or None,
            app_id=cfg.app_id or None,
            auto_import_lark_cli=auto_import_lark_cli,
        )
        return self.build_user_client(cfg).preflight_scopes(rec)

    async def aget_config_and_preflight(
        self, *, auto_import_lark_cli: bool = True
    ) -> tuple[FeishuAiCommandConfig, FeishuScopePreflightResult]:
        """Return a coherent ``(config, preflight)`` pair bound to the same account snapshot."""
        for _ in range(3):
            base_cfg = self._read_persisted_config()
            rec = await self._token_store.aresolve_token(
                open_id=base_cfg.owner_open_id or None,
                app_id=base_cfg.app_id or None,
                auto_import_lark_cli=auto_import_lark_cli,
            )
            latest_cfg = self._read_persisted_config()
            if latest_cfg != base_cfg:
                continue
            cfg = base_cfg
            if (
                rec is not None
                and (not cfg.app_id or rec.app_id == cfg.app_id)
                and (not cfg.owner_open_id or rec.open_id == cfg.owner_open_id)
            ):
                cfg = replace(
                    cfg,
                    owner_open_id=cfg.owner_open_id or rec.open_id,
                    app_id=cfg.app_id or rec.app_id,
                )
            return cfg, self.build_user_client(cfg).preflight_scopes(rec)
        cfg = await self.aget_config(auto_import_lark_cli=auto_import_lark_cli)
        preflight = await self.apreflight_status(
            auto_import_lark_cli=auto_import_lark_cli, config=cfg
        )
        return cfg, preflight

    def capabilities_dict(self) -> dict[str, Any]:
        model_efforts: dict[str, list[str]] = {}
        for base, variants in _MASO_MODEL_VARIANTS.items():
            effs = list(variants.keys())
            model_efforts[base] = effs
            for v in set(variants.values()):
                model_efforts[v] = effs
        return {
            "providers": [p.value for p in SUPPORTED_AI_COMMAND_PROVIDERS],
            "reasoning_efforts": [e for e in SUPPORTED_REASONING_EFFORTS if e],
            "provider_reasoning_efforts": {
                k: list(v) for k, v in PROVIDER_SUPPORTED_REASONING_EFFORTS.items()
            },
            "model_reasoning_efforts": model_efforts,
            "provider_models": {k: list(v) for k, v in DEFAULT_PROVIDER_MODELS.items()},
        }

    def is_owner_ai_command_candidate(
        self,
        *,
        sender_open_id: str,
        sender_type: str,
        text: str,
        config: FeishuAiCommandConfig | None = None,
    ) -> bool:
        cfg = config or self.get_config()
        if not cfg.enabled or sender_type != "user" or not cfg.owner_open_id or not sender_open_id:
            return False
        if not hmac.compare_digest(sender_open_id.strip(), cfg.owner_open_id.strip()):
            return False
        try:
            parsed = parse_ai_command(
                text,
                prefix=cfg.prefix,
                default_provider=cfg.provider,
                default_model=cfg.model,
                default_reasoning_effort=cfg.reasoning_effort,
                footer=cfg.footer,
            )
        except ValueError:
            return False
        return parsed is not None

    async def _fetch_context_and_refs(
        self,
        client: FeishuUserClient,
        message: FeishuUserMessage,
        cfg: FeishuAiCommandConfig,
        *,
        seeded_context: Sequence[FeishuUserMessage] | None = None,
    ) -> tuple[list[FeishuUserMessage], AiCommandContextRefs]:
        context_msgs: list[FeishuUserMessage] = list(seeded_context or [])
        if not context_msgs:
            if message.thread_id:
                try:
                    context_msgs = await client.list_messages(
                        container_id_type="thread",
                        container_id=message.thread_id,
                        sort_type="ByCreateTimeAsc",
                        page_size=cfg.max_context_messages,
                    )
                except Exception:
                    logger.warning(
                        "Failed to list Feishu thread messages for %s",
                        message.thread_id,
                    )
            if not context_msgs and message.chat_id:
                try:
                    desc_msgs = await client.list_messages(
                        container_id_type="chat",
                        container_id=message.chat_id,
                        sort_type="ByCreateTimeDesc",
                        page_size=cfg.max_context_messages,
                    )
                    context_msgs = list(reversed(desc_msgs))
                except Exception:
                    logger.warning("Failed to list Feishu chat messages for %s", message.chat_id)
            if message.parent_id and all(m.message_id != message.parent_id for m in context_msgs):
                try:
                    parent_msg = await client.get_message(message.parent_id)
                    if parent_msg is not None:
                        context_msgs.insert(0, parent_msg)
                except Exception:
                    pass

        texts = [m.text for m in context_msgs if m.text]
        if message.text and message.text not in texts:
            texts.append(message.text)
        return context_msgs, extract_context_references(texts)

    async def _ensure_execution_tab(
        self,
        message: FeishuUserMessage,
        parsed: ParsedAiCommand,
        cfg: FeishuAiCommandConfig,
    ) -> str:
        channel_key = f"{message.chat_id}:{message.thread_id or 'main'}:{parsed.provider.value}"
        with self._lock:
            existing_tab_id = dict(self._load_state().get("channel_tabs") or {}).get(channel_key)

        target_env = parsed.effective_env
        if existing_tab_id:
            tab = self._get_tab(existing_tab_id)
            if tab is not None and not getattr(tab, "archived", False):
                current_env = dict(getattr(tab, "env", {}) or {})
                merged_env = resolve_ai_command_agent_env(
                    parsed.provider,
                    parsed.model,
                    parsed.reasoning_effort,
                    base_env=current_env,
                )
                if merged_env != current_env:
                    await self._switch_tab_env(str(existing_tab_id), merged_env, tab=tab)
                return str(existing_tab_id)

        cwd = cfg.cwd.strip() or str(Path.home())
        if not os.path.isdir(cwd):
            cwd = str(Path.home())
        short_target = (message.thread_id or message.chat_id)[-6:]
        created = await self._create_tab(
            name=f"Feishu /ai · {short_target}"[:64],
            session_kind=SessionKind.CHAT,
            agent_type=parsed.provider,
            cwd=cwd,
            solo_mode=True,
            env=target_env,
        )
        tab_id = created if isinstance(created, str) else getattr(created, "id", "")
        if not isinstance(tab_id, str) or not tab_id.strip():
            raise RuntimeError("Failed to create Hub Chat tab for Feishu /ai command")
        tab_id = tab_id.strip()
        with self._lock:
            state = self._load_state()
            channel_tabs = dict(state.get("channel_tabs") or {})
            channel_tabs[channel_key] = tab_id
            state["channel_tabs"] = channel_tabs
            self._save_state(state)
        return tab_id

    def _get_tab(self, tab_id: str) -> Any:
        if self._tab_getter is not None:
            return self._tab_getter(tab_id)
        from claude_hub.services import ttyd_manager

        return ttyd_manager.get_tab(tab_id)

    async def _switch_tab_env(self, tab_id: str, env: dict[str, str], *, tab: Any = None) -> None:
        if self._tab_env_switcher is not None:
            res = self._tab_env_switcher(tab_id, env)
            if inspect.isawaitable(res):
                await res
            return
        switch_fn = getattr(tab, "switch_env", None) if tab is not None else None
        if callable(switch_fn):
            res = switch_fn(env)
            if inspect.isawaitable(res):
                await res
            return
        from claude_hub.services import ttyd_manager

        res = ttyd_manager.switch_env(tab_id, env)
        updated_tab = await res if inspect.isawaitable(res) else res
        try:
            from claude_hub.api.agent_stream import (
                _get_tab_tailer_manager,
                _terminal_tab_stream_session,
            )

            stream_session = _terminal_tab_stream_session(tab_id)
            if stream_session is not None:
                effective_env = dict(getattr(updated_tab, "env", None) or env)
                _get_tab_tailer_manager().set_env(stream_session, effective_env)
        except Exception:
            logger.debug(
                "Could not sync live stream tailer env for tab %s",
                tab_id,
                exc_info=True,
            )

    async def _create_tab(self, **kwargs: Any) -> Any:
        if self._tab_creator is not None:
            res = self._tab_creator(**kwargs)
            return await res if inspect.isawaitable(res) else res
        from claude_hub.services import ttyd_manager

        res = ttyd_manager.create_tab(**kwargs)
        return await res if inspect.isawaitable(res) else res

    async def _dispatch_turn(
        self,
        tab_id: str,
        prompt: str,
        turn_id: str,
        visible_text: str,
        turn_metadata: Mapping[str, Any],
    ) -> str:
        if self._turn_dispatcher is not None:
            return await self._turn_dispatcher(tab_id, prompt, turn_id, visible_text, turn_metadata)
        from claude_hub.api.agent_stream import dispatch_tab_chat_and_wait

        return await dispatch_tab_chat_and_wait(
            tab_id,
            prompt,
            turn_id,
            visible_text=visible_text,
            turn_metadata=dict(turn_metadata),
        )

    def _skip_result(self, msg: FeishuUserMessage, **kw: Any) -> AiCommandExecutionResult:
        kw.setdefault("status", "skipped")
        return AiCommandExecutionResult(
            False, bool(kw.pop("dry_run", False)), msg.message_id, msg.chat_id, msg.thread_id, **kw
        )

    def _done_result(self, msg: FeishuUserMessage, **kw: Any) -> AiCommandExecutionResult:
        tid = kw.pop("thread_id", msg.thread_id)
        dry = bool(kw.pop("dry_run", False))
        return AiCommandExecutionResult(True, dry, msg.message_id, msg.chat_id, tid, **kw)

    async def handle_message(
        self,
        message: FeishuUserMessage,
        *,
        source: str = "poller",
        dry_run: bool = False,
        seeded_context: Sequence[FeishuUserMessage] | None = None,
        config: FeishuAiCommandConfig | None = None,
    ) -> AiCommandExecutionResult:
        """Run the full ``/ai`` command pipeline for one candidate message."""
        cfg = config or await self.aget_config()
        if not dry_run and not cfg.enabled:
            return self._skip_result(message, dry_run=dry_run, reason="disabled")
        if message.sender_type != "user":
            return self._skip_result(message, dry_run=dry_run, reason="not_user_sender")
        if not dry_run:
            if not cfg.owner_open_id.strip():
                return self._skip_result(message, dry_run=dry_run, reason="no_owner_open_id")
            if not hmac.compare_digest(message.sender_open_id.strip(), cfg.owner_open_id.strip()):
                return self._skip_result(message, dry_run=dry_run, reason="sender_not_owner")

        try:
            parsed = parse_ai_command(
                message.text,
                prefix=cfg.prefix,
                default_provider=cfg.provider,
                default_model=cfg.model,
                default_reasoning_effort=cfg.reasoning_effort,
                footer=cfg.footer,
            )
        except ValueError as exc:
            return self._skip_result(
                message, dry_run=dry_run, status="invalid_flags", reason=str(exc)
            )

        if parsed is None:
            return self._skip_result(message, dry_run=dry_run, reason="prefix_not_matched")

        pending_item = None if dry_run else self.get_pending_command(message.message_id)
        resuming_completed = bool(
            pending_item
            and pending_item.get("dispatch_state") == "turn_completed"
            and isinstance(pending_item.get("answer"), str)
        )
        dedup_claim_key = f"ai_cmd:{cfg.owner_open_id or 'owner'}:{message.message_id}"
        if resuming_completed:
            self._dedup.claim(dedup_claim_key)
        elif not dry_run and not self._dedup.claim(dedup_claim_key):
            return self._skip_result(
                message,
                dry_run=dry_run,
                status="duplicate",
                reason="already_claimed",
                parsed=parsed,
            )

        user_client = self.build_user_client(cfg)
        ack_reaction_id: str | None = (
            str(pending_item["ack_reaction_id"])
            if resuming_completed and pending_item and pending_item.get("ack_reaction_id")
            else None
        )
        reply_in_thread = bool(message.thread_id) or cfg.reply_in_thread
        status_val = "completed"
        reason_val = ""
        tab_id: str | None = (
            str(pending_item["tab_id"])
            if resuming_completed and pending_item and pending_item.get("tab_id")
            else None
        )
        interim_receipt = FeishuReplyReceipt()
        final_receipt = FeishuReplyReceipt()
        dispatch_entered = False
        turn_completed = resuming_completed
        answer = str(pending_item["answer"]) if resuming_completed and pending_item else ""

        try:
            if dry_run:
                context_msgs = list(seeded_context or [])
                texts = [m.text for m in context_msgs if m.text]
                if message.text:
                    texts.append(message.text)
                refs = extract_context_references(texts)
                interim_text = build_interim_reply_text(parsed, refs, footer=cfg.footer)
                interim_post = build_ai_post_payload(interim_text, footer=cfg.footer)
                preview_final = ensure_ai_footer(
                    f"[Dry-run preview] Will dispatch to provider={parsed.provider.value}, "
                    f"model={parsed.model or '(default)'}, "
                    f"reasoning_effort={parsed.reasoning_effort or '(default)'}.",
                    footer=cfg.footer,
                )
                return self._done_result(
                    message,
                    status="dry_run",
                    parsed=parsed,
                    dry_run=True,
                    refs=refs,
                    interim_text=interim_text,
                    interim_post=interim_post,
                    final_text=preview_final,
                    final_post=build_ai_post_payload(preview_final, footer=cfg.footer),
                    ack_reaction=cfg.ack_emoji,
                    done_reaction=cfg.done_emoji,
                )

            if hasattr(user_client, "arequire_send_as_user_scope"):
                try:
                    await user_client.arequire_send_as_user_scope(allow_bot_fallback=True)
                except TypeError:
                    await user_client.arequire_send_as_user_scope()
            else:
                async with self._token_store.async_file_lock(
                    app_id=cfg.app_id, open_id=cfg.owner_open_id
                ):
                    try:
                        user_client.require_send_as_user_scope(allow_bot_fallback=True)
                    except TypeError:
                        user_client.require_send_as_user_scope()
            if resuming_completed and pending_item is not None:
                raw_refs = pending_item.get("refs")
                refs = (
                    AiCommandContextRefs(
                        tuple(raw_refs.get("mr_refs") or ()),
                        tuple(raw_refs.get("commit_refs") or ()),
                        tuple(raw_refs.get("doc_refs") or ()),
                    )
                    if isinstance(raw_refs, Mapping)
                    else AiCommandContextRefs()
                )
                interim_text = str(pending_item.get("interim_text") or "")
                interim_post = dict(pending_item.get("interim_post") or {})
                interim_receipt = FeishuReplyReceipt(
                    message_id=str(pending_item.get("interim_message_id") or ""),
                    thread_id=pending_item.get("thread_id"),
                )
            else:
                if cfg.ack_emoji:
                    try:
                        async with asyncio.timeout(_REACTION_TIMEOUT_SECONDS):
                            ack_reaction_id = await user_client.add_reaction_as_user(
                                message.message_id, cfg.ack_emoji
                            )
                    except Exception:
                        logger.warning(
                            "Failed to add %s reaction on %s",
                            cfg.ack_emoji,
                            message.message_id,
                        )

                context_msgs, refs = await self._fetch_context_and_refs(
                    user_client, message, cfg, seeded_context=seeded_context
                )
                interim_text = build_interim_reply_text(parsed, refs, footer=cfg.footer)
                interim_post = build_ai_post_payload(interim_text, footer=cfg.footer)
                try:
                    interim_receipt = await user_client.reply_as_user(
                        message.message_id,
                        "post",
                        interim_post,
                        chat_id=message.chat_id,
                        reply_in_thread=reply_in_thread,
                        uuid=bounded_message_uuid(message.message_id, "interim"),
                    )
                except TypeError:
                    interim_receipt = await user_client.reply_as_user(
                        message.message_id,
                        "post",
                        interim_post,
                        reply_in_thread=reply_in_thread,
                        uuid=bounded_message_uuid(message.message_id, "interim"),
                    )

                lock_key = f"{message.chat_id}:{message.thread_id or 'main'}"
                with self._lock:
                    channel_lock = self._channel_locks.setdefault(lock_key, asyncio.Lock())

                async with channel_lock:
                    tab_id = await self._ensure_execution_tab(message, parsed, cfg)
                    prompt = build_ai_dispatch_prompt(message, parsed, context_msgs, refs)
                    turn_id = f"feishu-ai-{message.message_id}"
                    turn_metadata = {
                        "origin": "feishu_ai_command",
                        "source": source,
                        "message_id": message.message_id,
                        "chat_id": message.chat_id,
                        "thread_id": message.thread_id,
                        "provider": parsed.provider.value,
                        "model": parsed.model,
                        "reasoning_effort": parsed.reasoning_effort,
                        **refs.to_dict(),
                    }
                    dispatch_entered = True
                    self.mark_pending_command_state(
                        message.message_id,
                        "in_flight",
                        dispatch_state="dispatched",
                        tab_id=tab_id,
                        turn_id=turn_id,
                        ack_reaction_id=ack_reaction_id,
                        interim_message_id=interim_receipt.message_id,
                        thread_id=message.thread_id or interim_receipt.thread_id,
                        refs=refs.to_dict(),
                        interim_text=interim_text,
                        interim_post=interim_post,
                    )
                    answer = await self._dispatch_turn(
                        tab_id,
                        prompt,
                        turn_id,
                        parsed.instruction or message.text,
                        turn_metadata,
                    )
                    turn_completed = True
                    self.mark_pending_command_state(
                        message.message_id,
                        "in_flight",
                        dispatch_state="turn_completed",
                        answer=answer,
                    )

            final_text = ensure_ai_footer(answer, footer=cfg.footer)
            final_post = build_ai_post_payload(final_text, footer=cfg.footer)
            try:
                final_receipt = await user_client.reply_as_user(
                    message.message_id,
                    "post",
                    final_post,
                    chat_id=message.chat_id,
                    reply_in_thread=reply_in_thread,
                    uuid=bounded_message_uuid(message.message_id, "final"),
                )
            except TypeError:
                final_receipt = await user_client.reply_as_user(
                    message.message_id,
                    "post",
                    final_post,
                    reply_in_thread=reply_in_thread,
                    uuid=bounded_message_uuid(message.message_id, "final"),
                )
            result = self._done_result(
                message,
                status="completed",
                parsed=parsed,
                thread_id=message.thread_id or interim_receipt.thread_id,
                refs=refs,
                interim_text=interim_text,
                interim_post=interim_post,
                final_text=final_text,
                final_post=final_post,
                ack_reaction=cfg.ack_emoji,
                done_reaction=cfg.done_emoji,
                tab_id=tab_id,
                interim_message_id=interim_receipt.message_id,
                final_message_id=final_receipt.message_id,
            )
            self._record_run(result, source=source)
            return result

        except FeishuUserScopeError as exc:
            status_val, reason_val = "scope_blocked", str(exc)
            logger.error("Feishu /ai command blocked by missing scope: %s", exc)
            result = self._done_result(message, status=status_val, reason=reason_val, parsed=parsed)
            self._record_run(result, source=source)
            return result

        except asyncio.CancelledError as exc:
            status_val, reason_val = "cancelled", "cancelled"
            if turn_completed:
                self.mark_pending_command_state(
                    message.message_id,
                    "interrupted",
                    dispatch_state="turn_completed",
                    answer=answer,
                )
            elif not dispatch_entered:
                self.mark_pending_command_state(
                    message.message_id, "interrupted", dispatch_state="not_started"
                )
                self._dedup.release(dedup_claim_key)
            elif type(exc).__name__ == "ExternalDispatchRetired":
                status_val, reason_val = "cancelled", "retired_retry_safe"
                self.mark_pending_command_state(
                    message.message_id, "interrupted", dispatch_state="retired_retry_safe"
                )
                self._dedup.release(dedup_claim_key)
            else:
                self.mark_pending_command_state(
                    message.message_id, "interrupted", dispatch_state="uncertain"
                )
            raise

        except Exception as exc:
            status_val = "failed"
            reason_val = str(exc) or type(exc).__name__
            logger.exception("Feishu /ai command execution failed for %s", message.message_id)
            error_text = ensure_ai_footer(
                f"没能生成回答，请稍后再试或直接联系本人。（{reason_val[:160]}）",
                footer=cfg.footer,
            )
            error_post = build_ai_post_payload(error_text, footer=cfg.footer)
            try:
                try:
                    final_receipt = await user_client.reply_as_user(
                        message.message_id,
                        "post",
                        error_post,
                        chat_id=message.chat_id,
                        reply_in_thread=reply_in_thread,
                        uuid=bounded_message_uuid(message.message_id, "error"),
                    )
                except TypeError:
                    final_receipt = await user_client.reply_as_user(
                        message.message_id,
                        "post",
                        error_post,
                        reply_in_thread=reply_in_thread,
                        uuid=bounded_message_uuid(message.message_id, "error"),
                    )
            except Exception:
                logger.warning("Failed to post /ai error fallback reply for %s", message.message_id)
            result = self._done_result(
                message,
                status=status_val,
                reason=reason_val,
                parsed=parsed,
                final_text=error_text,
                final_post=error_post,
                tab_id=tab_id,
                interim_message_id=interim_receipt.message_id,
                final_message_id=final_receipt.message_id,
            )
            self._record_run(result, source=source)
            return result

        finally:
            if not dry_run and not (status_val == "cancelled" and turn_completed):
                if ack_reaction_id:
                    try:
                        async with asyncio.timeout(_REACTION_TIMEOUT_SECONDS):
                            await user_client.delete_reaction_as_user(
                                message.message_id, ack_reaction_id
                            )
                    except Exception:
                        logger.debug(
                            "Could not remove ack reaction %s on %s",
                            ack_reaction_id,
                            message.message_id,
                        )
                if status_val == "completed" and cfg.done_emoji:
                    try:
                        async with asyncio.timeout(_REACTION_TIMEOUT_SECONDS):
                            await user_client.add_reaction_as_user(
                                message.message_id, cfg.done_emoji
                            )
                    except Exception:
                        logger.warning(
                            "Failed to add %s reaction on %s",
                            cfg.done_emoji,
                            message.message_id,
                        )
                if status_val != "cancelled":
                    self._dedup.finish(
                        dedup_claim_key,
                        status_val,
                        stage="ai_command",
                        reason=reason_val or None,
                    )

    async def handle_ws_event(self, event: FeishuMessageEvent) -> bool:
        """Handle a WebSocket ``FeishuMessageEvent`` if it matches the owner's ``/ai`` command."""
        cfg = await self.aget_config()
        if not self.is_owner_ai_command_candidate(
            sender_open_id=event.sender_open_id,
            sender_type="user",
            text=event.text,
            config=cfg,
        ):
            return False
        user_msg = FeishuUserMessage(
            event.message_id,
            event.chat_id,
            event.sender_open_id,
            "user",
            event.message_type,
            event.text,
            event.message_created_at_ms,
            event.thread_id,
            event.parent_id,
            event.root_id,
        )
        outcome = await self.handle_message(user_msg, source="websocket")
        return outcome.handled or outcome.status in {
            "duplicate",
            "scope_blocked",
            "failed",
        }


_DEFAULT_AI_COMMAND_SERVICE: FeishuAiCommandService | None = None


def get_feishu_ai_command_service() -> FeishuAiCommandService:
    global _DEFAULT_AI_COMMAND_SERVICE
    if _DEFAULT_AI_COMMAND_SERVICE is None:
        _DEFAULT_AI_COMMAND_SERVICE = FeishuAiCommandService()
    return _DEFAULT_AI_COMMAND_SERVICE
