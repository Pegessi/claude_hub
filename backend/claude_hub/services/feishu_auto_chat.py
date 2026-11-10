"""Auto-provisioning of Chat tabs for Feishu Bots in ``per_channel`` mode."""

from __future__ import annotations

import hmac
import inspect
import logging
import time
from typing import Any, Callable

from claude_hub.models import AgentType, SessionKind
from claude_hub.services.bot_pool.common import MAX_CHANNELS_PER_BOT, BotEntry

logger = logging.getLogger(__name__)

AUTO_CHAT_MIN_INTERVAL_SECONDS = 5.0
DEFAULT_CHAT_AGENT_TYPE = AgentType.CLAUDE
DEFAULT_CHAT_CWD: str | None = None
_MAX_TAB_NAME_CHARS = 64


class FeishuAutoChatError(RuntimeError):
    """Raised when a ``per_channel`` Bot cannot open a new session."""

    def __init__(self, reason: str, user_message: str) -> None:
        super().__init__(user_message)
        self.reason = reason
        self.user_message = user_message


class FeishuChannelLimitError(FeishuAutoChatError):
    """Raised when a Bot already holds ``MAX_CHANNELS_PER_BOT`` channels."""

    def __init__(
        self,
        user_message: str = (
            "This bot could not open a new session because its channel limit has been reached."
        ),
    ) -> None:
        super().__init__("channel_limit_reached", user_message)


class FeishuSenderChannelLimitError(FeishuAutoChatError):
    """Raised when the sender already has an active DM channel on this Bot."""

    def __init__(
        self,
        user_message: str = (
            "This bot could not open a new session because you already have an active session."
        ),
    ) -> None:
        super().__init__("sender_channel_exists", user_message)


class FeishuAutoChatCooldownError(FeishuAutoChatError):
    """Raised when auto-creations on one Bot arrive faster than the cooldown."""

    def __init__(
        self,
        user_message: str = (
            "This bot could not open a new session right now; please wait a few seconds and try again."
        ),
    ) -> None:
        super().__init__("auto_create_rate_limited", user_message)


class FeishuAutoChatCreateError(FeishuAutoChatError):
    """Raised when the underlying Chat tab could not be created."""

    def __init__(
        self,
        user_message: str = (
            "This bot could not open a new session right now; please try again later."
        ),
    ) -> None:
        super().__init__("tab_create_failed", user_message)


def format_dm_tab_name(sender_open_id: str) -> str:
    """Format a new Chat tab name for a Feishu DM using the sender open_id tail."""

    clean = sender_open_id.strip()
    if not clean:
        return "Feishu · DM"
    if len(clean) <= 6:
        return f"Feishu · DM {clean}"[:_MAX_TAB_NAME_CHARS]
    prefix = "ou" if clean.startswith("ou") else clean[:2]
    return f"Feishu · DM {prefix}…{clean[-4:]}"[:_MAX_TAB_NAME_CHARS]


def format_group_tab_name(chat_name: str | None, chat_id: str | None = None) -> str:
    """Format a new Chat tab name for a Feishu group."""

    label = (chat_name or "").strip() or (chat_id or "").strip() or "Group"
    return f"Feishu · {label}"[:_MAX_TAB_NAME_CHARS]


class FeishuAutoChatCreator:
    """Create new Chat tabs and enforce ``per_channel`` provisioning limits."""

    def __init__(
        self,
        manager: Any = None,
        *,
        tab_creator: Callable[..., Any] | None = None,
        default_agent_type: AgentType = DEFAULT_CHAT_AGENT_TYPE,
        default_cwd: str | None = DEFAULT_CHAT_CWD,
        min_interval_seconds: float = AUTO_CHAT_MIN_INTERVAL_SECONDS,
        max_channels_per_bot: int = MAX_CHANNELS_PER_BOT,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._manager = manager
        self._tab_creator = tab_creator
        self.default_agent_type = default_agent_type
        self.default_cwd = default_cwd
        self.min_interval_seconds = min_interval_seconds
        self.max_channels_per_bot = max_channels_per_bot
        self._now = now
        self._last_auto_create_at: dict[tuple[int, str], float] = {}

    def _resolve_manager(self, manager: Any = None) -> Any:
        if manager is not None:
            return manager
        if self._manager is not None:
            return self._manager
        from claude_hub.services import ttyd_manager

        return ttyd_manager

    @staticmethod
    def dm_tab_name(sender_open_id: str) -> str:
        return format_dm_tab_name(sender_open_id)

    @staticmethod
    def group_tab_name(chat_name: str | None, chat_id: str | None = None) -> str:
        return format_group_tab_name(chat_name, chat_id)

    def check_dm_limits(
        self,
        entry: BotEntry,
        sender_open_id: str,
        *,
        now: float | None = None,
        pool_key: int = 0,
    ) -> None:
        if len(entry.bindings) >= self.max_channels_per_bot:
            raise FeishuChannelLimitError()
        if any(
            not binding.is_group and hmac.compare_digest(binding.sender_open_id, sender_open_id)
            for binding in entry.bindings
        ):
            raise FeishuSenderChannelLimitError()
        current_now = self._now() if now is None else now
        last = self._last_auto_create_at.get((pool_key, entry.bot_id))
        if (
            last is not None
            and entry.bindings
            and 0.0 <= current_now - last < self.min_interval_seconds
        ):
            raise FeishuAutoChatCooldownError()

    def record_auto_create(
        self,
        bot_id: str,
        *,
        now: float | None = None,
        pool_key: int = 0,
    ) -> None:
        self._last_auto_create_at[(pool_key, bot_id)] = self._now() if now is None else now

    def reset(self) -> None:
        self._last_auto_create_at.clear()

    async def create_tab(self, name: str, *, manager: Any = None) -> str:
        try:
            if self._tab_creator is not None:
                result = self._tab_creator(name)
                if inspect.isawaitable(result):
                    result = await result
            else:
                target_manager = self._resolve_manager(manager)
                result = target_manager.create_tab(
                    name=name,
                    session_kind=SessionKind.CHAT,
                    agent_type=self.default_agent_type,
                    cwd=self.default_cwd,
                )
                if inspect.isawaitable(result):
                    result = await result
        except FeishuAutoChatError:
            raise
        except Exception as exc:
            logger.warning("Feishu auto-chat tab creation failed: %s", type(exc).__name__)
            raise FeishuAutoChatCreateError() from exc

        if isinstance(result, str) and result.strip():
            return result.strip()
        tab_id = getattr(result, "id", None)
        if isinstance(tab_id, str) and tab_id.strip():
            return tab_id.strip()
        raise FeishuAutoChatCreateError()

    async def create_chat_tab(self, name: str, *, manager: Any = None) -> str:
        return await self.create_tab(name, manager=manager)

    async def delete_tab(self, tab_id: str, *, manager: Any = None) -> None:
        target_manager = self._resolve_manager(manager)
        delete_fn = getattr(target_manager, "delete_tab", None)
        if delete_fn is None:
            return
        try:
            outcome = delete_fn(tab_id)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:
            logger.warning("Failed to roll back auto-created Feishu Chat tab %s", tab_id)


FeishuAutoChatProvisioner = FeishuAutoChatCreator
