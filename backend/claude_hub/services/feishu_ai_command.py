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

import httpx

from claude_hub.config import settings
from claude_hub.models import (
    AgentType,
    SessionKind,
    StreamModelOption,
    StreamReasoningEffortOption,
)
from claude_hub.services import feishu_ai_workspace
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
    DEFAULT_AI_COMMAND_SEND_MODE,
    DEFAULT_MAX_ACTIVE_CHATS,
    DEFAULT_MAX_CONTEXT_MESSAGES,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_PROVIDER_MODELS,
    PROVIDER_SUPPORTED_REASONING_EFFORTS,
    SUPPORTED_AI_COMMAND_PROVIDERS,
    SUPPORTED_AI_COMMAND_SEND_MODES,
    SUPPORTED_REASONING_EFFORTS,
    AiCommandContextRefs,
    AiCommandExecutionResult,
    FeishuAiCommandConfig,
    ParsedAiCommand,
    bounded_message_uuid,
    build_ai_dispatch_prompt,
    build_ai_post_payload,
)
from claude_hub.services.feishu_ai_command_models import (
    build_capabilities_payload as _build_capabilities_payload,
)
from claude_hub.services.feishu_ai_command_models import (
    build_interim_reply_text,
    ensure_ai_footer,
    extract_context_references,
    parse_ai_command,
    register_live_maso_model_variants,
    resolve_ai_command_agent_env,
)
from claude_hub.services.feishu_ai_command_models import (
    serialize_model_option as _serialize_model_option,
)
from claude_hub.services.feishu_ai_command_models import (
    validate_ai_command_agent_config,
)
from claude_hub.services.feishu_bot import (
    FeishuMessageDedupStore,
    FeishuMessageEvent,
    FeishuReplyReceipt,
)
from claude_hub.services.feishu_user_client import (
    FeishuDeliveryUncertainError,
    FeishuScopePreflightResult,
    FeishuUserClient,
    FeishuUserMessage,
    FeishuUserScopeError,
)
from claude_hub.services.feishu_user_token_store import (
    FeishuUserTokenStore,
    get_feishu_user_token_store,
)
from claude_hub.services.feishu_web_session import (
    FeishuWebLoginManager,
    FeishuWebSessionStore,
    get_feishu_web_session_store,
    matches_authorized_owner_sender,
    normalize_and_claim_ai_command_message,
)
from claude_hub.services.local_storage import (
    document_exists,
    read_document,
    write_document,
)
from claude_hub.services.runtime_isolation import resolve_runtime_home

logger = logging.getLogger(__name__)

AiTurnDispatcher = Callable[[str, str, str, str, Mapping[str, Any]], Awaitable[str]]


_AI_MASO_CATALOG_CACHE: tuple[float, list[StreamModelOption]] | None = None
_AI_EFFORT_LABELS: dict[str, str] = {
    "default": "Default",
    "none": "None",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra High",
    "max": "Max",
    "ultra": "Ultra",
}


def _static_maso_model_options() -> list[StreamModelOption]:
    options: list[StreamModelOption] = []
    for base, variants in _MASO_MODEL_VARIANTS.items():
        efforts = [
            StreamReasoningEffortOption(
                id=effort_id,
                label=_AI_EFFORT_LABELS.get(effort_id, effort_id.title()),
                provider_model_id=variant_id,
            )
            for effort_id, variant_id in variants.items()
        ]
        default_effort = (
            "high"
            if "high" in variants
            else next((e.id for e in efforts if e.provider_model_id == base), efforts[0].id)
        )
        options.append(
            StreamModelOption(
                id=base,
                label=base,
                provider_model_id=variants.get(default_effort, base),
                default_reasoning_effort=default_effort,
                supported_reasoning_efforts=efforts,
            )
        )
    return options


def _sync_provider_model_options() -> dict[str, list[StreamModelOption]]:
    from claude_hub.services.agent_stream import native as native_mod
    from claude_hub.services.agent_stream.providers import model_options as provider_model_options

    now = time.monotonic()
    out: dict[str, list[StreamModelOption]] = {}
    for prov_enum in SUPPORTED_AI_COMMAND_PROVIDERS:
        prov = prov_enum.value
        if prov == "maso":
            if _AI_MASO_CATALOG_CACHE is not None and _AI_MASO_CATALOG_CACHE[0] > now:
                out[prov] = list(_AI_MASO_CATALOG_CACHE[1])
            else:
                out[prov] = _static_maso_model_options()
            continue
        if prov == "codex":
            out[prov] = native_mod._codex_model_options({})
            continue
        cached = native_mod._models_cache.get(prov)
        if cached is not None and cached.expires_at > now:
            out[prov] = list(cached.options)
            continue
        if prov == "cursor":
            out[prov] = native_mod._group_cursor_model_options(native_mod._static_options("cursor"))
        else:
            out[prov] = provider_model_options.model_options_from_ids(
                DEFAULT_PROVIDER_MODELS.get(prov, ())
            )
    return out


async def _async_provider_model_options() -> dict[str, list[StreamModelOption]]:
    global _AI_MASO_CATALOG_CACHE
    from claude_hub.services.agent_stream.maso_http import check_maso_service

    out = _sync_provider_model_options()
    now = time.monotonic()
    if _AI_MASO_CATALOG_CACHE is None or _AI_MASO_CATALOG_CACHE[0] <= now:
        try:
            async with asyncio.timeout(0.8):
                live_maso, _ = await check_maso_service(dict(os.environ))
            if live_maso:
                register_live_maso_model_variants(live_maso)
                _AI_MASO_CATALOG_CACHE = (time.monotonic() + 120.0, list(live_maso))
                out["maso"] = list(live_maso)
        except Exception:
            _AI_MASO_CATALOG_CACHE = (time.monotonic() + 30.0, out["maso"])
    return out


class FeishuAiCommandService:
    """Coordinates ``/ai`` configuration, deduplication, context fetch, reactions, and replies."""

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        token_store: FeishuUserTokenStore | None = None,
        web_session_store: FeishuWebSessionStore | None = None,
        web_login_manager: FeishuWebLoginManager | None = None,
        dedup_store: FeishuMessageDedupStore | None = None,
        user_client_factory: Callable[[FeishuAiCommandConfig], FeishuUserClient] | None = None,
        tab_creator: Callable[..., Any] | None = None,
        tab_getter: Callable[[str], Any] | None = None,
        tab_env_switcher: Callable[[str, dict[str, str]], Any] | None = None,
        turn_dispatcher: AiTurnDispatcher | None = None,
        workspace_resolver: Callable[[FeishuAiCommandConfig, str], Any] | None = None,
        managed_session_provider: Callable[..., Any] | None = None,
        managed_session_getter: Callable[[str], Any] | None = None,
        managed_turn_dispatcher: Callable[..., Awaitable[str]] | None = None,
    ) -> None:
        runtime_home = resolve_runtime_home()
        self._state_path: Path = state_path or (runtime_home / "feishu_ai_command.json")
        self._token_store: FeishuUserTokenStore = token_store or get_feishu_user_token_store()
        if web_session_store is not None:
            self._web_session_store = web_session_store
        elif state_path is not None:
            self._web_session_store = FeishuWebSessionStore(
                path=state_path.parent / "feishu_web_session.json",
                key_path=state_path.parent / "feishu_web_session.key",
            )
        else:
            self._web_session_store = get_feishu_web_session_store()
        self._web_login_manager = web_login_manager or FeishuWebLoginManager(
            self._web_session_store
        )
        self._dedup: FeishuMessageDedupStore = dedup_store or FeishuMessageDedupStore()
        self._user_client_factory = user_client_factory
        self._tab_creator = tab_creator
        self._tab_getter = tab_getter
        self._tab_env_switcher = tab_env_switcher
        self._turn_dispatcher: AiTurnDispatcher | None = turn_dispatcher
        # Managed Agent Workspace seams. Production wires these to the singleton
        # workspace manager + managed Chat tailer so /ai Chat tabs are created
        # inside a workspace; tests inject fakes. A None resolver lazily uses
        # the real workspace manager.
        self._workspace_resolver = workspace_resolver
        self._managed_session_provider = managed_session_provider
        self._managed_session_getter = managed_session_getter
        self._managed_turn_dispatcher = managed_turn_dispatcher
        self._lock = threading.RLock()
        self._channel_locks: dict[str, asyncio.Lock] = {}

    @property
    def web_session_store(self) -> FeishuWebSessionStore:
        return self._web_session_store

    @property
    def web_login_manager(self) -> FeishuWebLoginManager:
        return self._web_login_manager

    def switch_owner_to_web_session(self) -> FeishuAiCommandConfig:
        """Explicitly switch ``owner_open_id`` to the currently authenticated web session's identity."""
        sess = self._web_session_store.load(auto_import_larkx=False)
        if sess is not None and sess.is_valid_structure():
            eff_open = (sess.open_id or sess.user_id).strip()
            if eff_open:
                return self.update_config({"owner_open_id": eff_open, "app_id": "web_session"})
        return self.get_config(auto_import_lark_cli=False)

    def _apply_web_session_owner_fallback(
        self, cfg: FeishuAiCommandConfig
    ) -> FeishuAiCommandConfig:
        if cfg.owner_open_id or cfg.send_mode == "openapi":
            return cfg
        sess = self._web_session_store.load(auto_import_larkx=False)
        if sess is not None and sess.is_valid_structure():
            eff_open = (sess.open_id or sess.user_id).strip()
            if eff_open:
                return replace(
                    cfg,
                    owner_open_id=eff_open,
                    app_id=cfg.app_id or "web_session",
                )
        return cfg

    def _matches_owner_sender(self, sender_id: str, cfg: FeishuAiCommandConfig) -> bool:
        return matches_authorized_owner_sender(
            sender_id,
            owner_open_id=cfg.owner_open_id,
            web_session=self._web_session_store.load(auto_import_larkx=False),
            send_mode=cfg.send_mode,
        )

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
            return self._apply_web_session_owner_fallback(cfg)

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
        return self._apply_web_session_owner_fallback(cfg)

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
            if (
                "model" in patch
                and patch["model"] is not None
                and not str(patch["model"]).strip()
                and ("reasoning_effort" not in patch or patch["reasoning_effort"] is None)
                and new_provider in {"maso", "cursor", "claude", "antigravity"}
            ):
                merged["reasoning_effort"] = ""
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
            managed_record = self._channel_record_for_tab(tab_id)
            ev_state, recovered_answer = feishu_ai_workspace.recover_completed_stream_answer(
                tab_id,
                turn_id,
                managed_workspace_id=managed_record.get("workspace_id") or None,
                managed_session_id=managed_record.get("session_id") or None,
            )
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
            web_session_store=self._web_session_store,
            send_mode=cfg.send_mode,
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
            cfg = self._apply_web_session_owner_fallback(cfg)
            return cfg, self.build_user_client(cfg).preflight_scopes(rec)
        cfg = await self.aget_config(auto_import_lark_cli=auto_import_lark_cli)
        preflight = await self.apreflight_status(
            auto_import_lark_cli=auto_import_lark_cli, config=cfg
        )
        return cfg, preflight

    def capabilities_dict(self) -> dict[str, Any]:
        return _build_capabilities_payload(_sync_provider_model_options())

    async def acapabilities_dict(self) -> dict[str, Any]:
        return _build_capabilities_payload(await _async_provider_model_options())

    def _is_valid_ai_command_text(
        self,
        text: str,
        cfg: FeishuAiCommandConfig,
    ) -> bool:
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

    def is_owner_ai_command_candidate(
        self,
        *,
        sender_open_id: str,
        sender_type: str,
        text: str,
        config: FeishuAiCommandConfig | None = None,
    ) -> bool:
        cfg = config or self.get_config()
        if not cfg.enabled or sender_type != "user" or not sender_open_id:
            return False
        if not self._matches_owner_sender(sender_open_id, cfg):
            return False
        return self._is_valid_ai_command_text(text, cfg)

    async def _verify_cross_app_ws_owner(
        self,
        event: FeishuMessageEvent,
        cfg: FeishuAiCommandConfig,
    ) -> FeishuUserMessage | None:
        """Authoritatively verify a cross-app WebSocket ``/ai`` message under ``cfg.app_id``.

        Feishu ``open_id`` values are app-scoped: an event received on another Hub pool bot
        reports that bot app's ``sender_open_id`` rather than ``cfg.owner_open_id``. Instead
        of trusting display names or unverified local aliases, fetch ``event.message_id`` via
        the owner's token on ``cfg.app_id`` so Feishu returns the authoritative sender
        ``open_id`` in ``cfg.app_id``'s namespace.
        """
        if not cfg.enabled or not cfg.owner_open_id.strip() or not event.message_id.strip():
            return None
        if not self._is_valid_ai_command_text(event.text, cfg):
            return None
        user_client = self.build_user_client(cfg)
        try:
            async with asyncio.timeout(5.0):
                fetched = await user_client.get_message(event.message_id)
        except Exception:
            logger.debug(
                "Cross-app WS /ai owner verification could not fetch message %s",
                event.message_id,
                exc_info=True,
            )
            return None
        if fetched is None or fetched.sender_type != "user":
            return None
        if not hmac.compare_digest(fetched.sender_open_id.strip(), cfg.owner_open_id.strip()):
            return None
        return FeishuUserMessage(
            message_id=event.message_id,
            chat_id=event.chat_id or fetched.chat_id,
            sender_open_id=cfg.owner_open_id.strip(),
            sender_type="user",
            msg_type=event.message_type or fetched.msg_type,
            text=event.text,
            create_time_ms=event.message_created_at_ms or fetched.create_time_ms,
            thread_id=event.thread_id or fetched.thread_id,
            parent_id=event.parent_id or fetched.parent_id,
            root_id=event.root_id or fetched.root_id,
        )

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
        return await feishu_ai_workspace.ensure_execution_tab(self, message, parsed, cfg)

    @staticmethod
    def _channel_key(message: FeishuUserMessage, parsed: ParsedAiCommand) -> str:
        return feishu_ai_workspace.channel_key(message, parsed)

    @staticmethod
    def _resolve_cwd(cfg: FeishuAiCommandConfig) -> str:
        return feishu_ai_workspace.resolve_cwd(cfg)

    def _uses_legacy_tabs(self) -> bool:
        # An injected tab_creator is the explicit legacy/terminal seam used by
        # older callers and unit tests. Production constructs the service with
        # no tab_creator, so it defaults to managed workspace Chat tabs.
        return self._tab_creator is not None

    @staticmethod
    def _provider_supports_managed_chat(agent_type: AgentType) -> bool:
        return feishu_ai_workspace.provider_supports_managed_chat(agent_type)

    async def _switch_managed_env(self, session_id: str, env: Mapping[str, str]) -> None:
        await feishu_ai_workspace.switch_managed_env(session_id, env)

    @staticmethod
    def _normalize_channel_record(raw: Any) -> dict[str, str]:
        return feishu_ai_workspace.normalize_channel_record(raw)

    def _store_channel_record(self, channel_key: str, record: Mapping[str, str]) -> None:
        with self._lock:
            state = self._load_state()
            channel_tabs = dict(state.get("channel_tabs") or {})
            channel_tabs[channel_key] = dict(record)
            state["channel_tabs"] = channel_tabs
            self._save_state(state)

    def _channel_record_for_tab(self, tab_id: str) -> dict[str, str]:
        if not tab_id:
            return {}
        with self._lock:
            channel_tabs = dict(self._load_state().get("channel_tabs") or {})
        for raw in channel_tabs.values():
            record = self._normalize_channel_record(raw)
            if record.get("tab_id") == tab_id:
                return record
        return {}

    async def _create_legacy_tab(
        self,
        message: FeishuUserMessage,
        parsed: ParsedAiCommand,
        cfg: FeishuAiCommandConfig,
        target_env: Mapping[str, str],
    ) -> str:
        return await feishu_ai_workspace.create_legacy_tab(self, message, parsed, cfg, target_env)

    def _resolve_workspace(self, cfg: FeishuAiCommandConfig, cwd: str) -> Any:
        return feishu_ai_workspace.resolve_workspace(
            cfg, cwd, resolver_seam=self._workspace_resolver
        )

    async def _provide_managed_session(
        self,
        *,
        workspace_id: str,
        agent_type: AgentType,
        env: Mapping[str, str],
        title: str,
        cwd: str | None = None,
    ) -> Any:
        return await feishu_ai_workspace.provide_managed_session(
            workspace_id=workspace_id,
            agent_type=agent_type,
            env=env,
            title=title,
            cwd=cwd,
            provider_seam=self._managed_session_provider,
        )

    async def _dispatch_managed_turn(
        self,
        session: Any,
        prompt: str,
        turn_id: str,
        visible_text: str,
        turn_metadata: Mapping[str, Any],
    ) -> str:
        return await feishu_ai_workspace.dispatch_managed_turn(
            session,
            prompt,
            turn_id,
            visible_text,
            turn_metadata,
            dispatcher_seam=self._managed_turn_dispatcher,
        )

    def _get_tab(self, tab_id: str) -> Any:
        if self._tab_getter is not None:
            return self._tab_getter(tab_id)
        from claude_hub.services import ttyd_manager

        return ttyd_manager.get_tab(tab_id)

    async def _switch_tab_env(self, tab_id: str, env: dict[str, str], *, tab: Any = None) -> None:
        await feishu_ai_workspace.switch_tab_env(self, tab_id, env, tab=tab)

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
        return await feishu_ai_workspace.dispatch_turn(
            self, tab_id, prompt, turn_id, visible_text, turn_metadata
        )

    def _get_managed_session(self, session_id: str) -> Any:
        return feishu_ai_workspace.get_managed_session(
            session_id, getter_seam=self._managed_session_getter
        )

    def _skip_result(self, msg: FeishuUserMessage, **kw: Any) -> AiCommandExecutionResult:
        kw.setdefault("status", "skipped")
        return AiCommandExecutionResult(
            False,
            bool(kw.pop("dry_run", False)),
            msg.message_id,
            msg.chat_id,
            msg.thread_id,
            **kw,
        )

    def _done_result(self, msg: FeishuUserMessage, **kw: Any) -> AiCommandExecutionResult:
        tid = kw.pop("thread_id", msg.thread_id)
        dry = bool(kw.pop("dry_run", False))
        return AiCommandExecutionResult(True, dry, msg.message_id, msg.chat_id, tid, **kw)

    async def _reply_ai_command(
        self,
        user_client: FeishuUserClient,
        message_id: str,
        post_payload: Mapping[str, object],
        *,
        chat_id: str,
        reply_in_thread: bool,
        uuid: str,
    ) -> FeishuReplyReceipt:
        try:
            return await user_client.reply_as_user(
                message_id,
                "post",
                post_payload,
                chat_id=chat_id,
                reply_in_thread=reply_in_thread,
                uuid=uuid,
                allow_bound_or_dm_fallback_on_not_in_chat=True,
            )
        except TypeError:
            try:
                return await user_client.reply_as_user(
                    message_id,
                    "post",
                    post_payload,
                    chat_id=chat_id,
                    reply_in_thread=reply_in_thread,
                    uuid=uuid,
                )
            except TypeError:
                return await user_client.reply_as_user(
                    message_id,
                    "post",
                    post_payload,
                    reply_in_thread=reply_in_thread,
                    uuid=uuid,
                )

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
            if not self._matches_owner_sender(message.sender_open_id, cfg):
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
        user_client = self.build_user_client(cfg)
        (
            message,
            dedup_claim_key,
            extra_dedup_keys,
            claimed_ok,
        ) = await normalize_and_claim_ai_command_message(
            message=message,
            parsed_instruction=parsed.instruction,
            owner_open_id=cfg.owner_open_id,
            user_client=user_client,
            dedup_store=self._dedup,
            lock=self._lock,
            dry_run=dry_run,
            resuming_completed=resuming_completed,
        )
        if not claimed_ok:
            return self._skip_result(
                message,
                dry_run=dry_run,
                status="duplicate",
                reason="already_claimed",
                parsed=parsed,
            )
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
        final_reply_started = False
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
                interim_receipt = await self._reply_ai_command(
                    user_client,
                    message.message_id,
                    interim_post,
                    chat_id=message.chat_id,
                    reply_in_thread=reply_in_thread,
                    uuid=bounded_message_uuid(message.message_id, "interim"),
                )

                if (
                    not isinstance(interim_receipt.message_id, str)
                    or not interim_receipt.message_id.strip()
                ):
                    raise FeishuDeliveryUncertainError(
                        "Feishu interim reply message identity is unavailable; delivery may have occurred"
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
            final_reply_started = True
            final_receipt = await self._reply_ai_command(
                user_client,
                message.message_id,
                final_post,
                chat_id=message.chat_id,
                reply_in_thread=reply_in_thread,
                uuid=bounded_message_uuid(message.message_id, "final"),
            )
            if (
                not isinstance(final_receipt.message_id, str)
                or not final_receipt.message_id.strip()
            ):
                raise FeishuDeliveryUncertainError(
                    "Feishu final reply message identity is unavailable; delivery may have occurred"
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
                for dk in (dedup_claim_key, *extra_dedup_keys):
                    self._dedup.release(dk)
            elif type(exc).__name__ == "ExternalDispatchRetired":
                status_val, reason_val = "cancelled", "retired_retry_safe"
                self.mark_pending_command_state(
                    message.message_id,
                    "interrupted",
                    dispatch_state="retired_retry_safe",
                )
                for dk in (dedup_claim_key, *extra_dedup_keys):
                    self._dedup.release(dk)
            else:
                self.mark_pending_command_state(
                    message.message_id, "interrupted", dispatch_state="uncertain"
                )
            raise

        except Exception as exc:
            status_val = "failed"
            reason_val = str(exc) or type(exc).__name__
            logger.exception("Feishu /ai command execution failed for %s", message.message_id)
            uncertain_final_delivery = final_reply_started and isinstance(
                exc,
                (
                    FeishuDeliveryUncertainError,
                    httpx.TimeoutException,
                    httpx.ReadError,
                    httpx.WriteError,
                    httpx.RemoteProtocolError,
                    httpx.DecodingError,
                ),
            )
            error_text = ""
            error_post: dict[str, Any] = {}
            if not uncertain_final_delivery:
                error_text = ensure_ai_footer(
                    f"没能生成回答，请稍后再试或直接联系本人。（{reason_val[:160]}）",
                    footer=cfg.footer,
                )
                error_post = build_ai_post_payload(error_text, footer=cfg.footer)
                try:
                    final_receipt = await self._reply_ai_command(
                        user_client,
                        message.message_id,
                        error_post,
                        chat_id=message.chat_id,
                        reply_in_thread=reply_in_thread,
                        uuid=bounded_message_uuid(message.message_id, "error"),
                    )
                except Exception:
                    logger.warning(
                        "Failed to post /ai error fallback reply for %s",
                        message.message_id,
                    )
            result = self._done_result(
                message,
                status=status_val,
                reason=reason_val,
                parsed=parsed,
                final_text=final_text if uncertain_final_delivery else error_text,
                final_post=final_post if uncertain_final_delivery else error_post,
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
                    for dk in (dedup_claim_key, *extra_dedup_keys):
                        self._dedup.finish(
                            dk,
                            status_val,
                            stage="ai_command",
                            reason=reason_val or None,
                        )

    async def handle_ws_event(self, event: FeishuMessageEvent) -> bool:
        """Handle a WebSocket ``FeishuMessageEvent`` if it matches the owner's ``/ai`` command."""
        cfg = await self.aget_config()
        if self.is_owner_ai_command_candidate(
            sender_open_id=event.sender_open_id,
            sender_type="user",
            text=event.text,
            config=cfg,
        ):
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
        else:
            verified_msg = await self._verify_cross_app_ws_owner(event, cfg)
            if verified_msg is None:
                return False
            user_msg = verified_msg
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
