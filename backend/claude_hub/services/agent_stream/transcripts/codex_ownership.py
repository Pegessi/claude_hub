"""CodexOwnership with per-adapter state inherited from its typed owner."""

from __future__ import annotations

from typing import Any, Dict, Optional, Set

from ....models import AgentStreamEvent, AgentStreamEventType, AgentType
from ..base import AgentStreamAdapter, NormalizeContext
from .codex_values import (
    _CODE_MODE_NESTED_PREFIX,
    _COLLAB_MESSAGE_TOOLS,
    _COLLAB_SPAWN_TOOLS,
    _TRANSIENT_NOTICE_MESSAGE_ID,
    _clean_thread_id,
    _transient_provider_notice,
)


class CodexOwnership(AgentStreamAdapter):
    """Adapter for Codex CLI's ``~/.codex/sessions/**/rollout-*.jsonl`` logs."""

    adapter_id = "codex-jsonl"
    schema_version = 1
    spec_agent_type = AgentType.CODEX

    def __init__(self) -> None:
        super().__init__()
        self._latest_usage: Dict[str, Dict[str, Any]] = {}
        # Per-session collab thread registry, learned from the main agent's
        # ``spawnAgent`` / ``sendInput`` calls. Used only as a fallback to
        # attribute ``code-mode-nested`` items when the provider omits a
        # distinguishing ``threadId`` (see ``_code_mode_thread``). Keyed by
        # NormalizeContext.session_id so concurrent tabs never cross wires.
        self._spawned_threads: Dict[str, Set[str]] = {}
        self._messaged_threads: Dict[str, Set[str]] = {}
        # Multi-spawn attribution: maps a ``code-mode-nested:<depth>:<parent>``
        # item's parent call token to the concrete child thread that owns the
        # code-mode host. Learned the first time an item with that token
        # carries an explicit owner thread (``params.threadId`` / the item's
        # own thread fields); later started/completed/delta records for the
        # same host — which may omit the thread id — then still attribute to
        # the right child instead of a shared synthetic bucket. Per session.
        self._nested_owner_by_parent: Dict[str, Dict[str, str]] = {}
        # In-flight reasoning items per turn, so a cancelled/interrupted turn
        # (or any terminal without ``item/completed`` for reasoning) can
        # finalize the "Thinking…" status instead of leaving it stale. Keyed
        # by turn (``ctx.turn_id`` or session id), value maps reasoning item
        # id → the sub-thread it belongs to (None for the main thread).
        self._inflight_reasoning: Dict[str, Dict[str, Optional[str]]] = {}

    def _error_or_status_event(
        self, ctx: NormalizeContext, message: str, sub_thread: Optional[str] = None
    ) -> AgentStreamEvent:
        """Map an error-channel message to ERROR, or to a coalesced STATUS.

        A recoverable provider reconnect/backoff notice (``Reconnecting… n/m``)
        is not a turn-terminal error: the turn is still alive. Emit it as one
        stable-id snapshot STATUS (the UI replaces it in place and never treats
        it as terminal) instead of an ERROR that would drop the active-turn
        lock and hide Stop.
        """
        if sub_thread is not None:
            # Child failures are visible inside the child's timeline, never a
            # terminal ERROR for the parent Chat or its scheduled/Goal run.
            return ctx.event(
                AgentStreamEventType.STATUS,
                {"text": message, "provider_status": "error"},
                sub_thread_id=sub_thread,
            )
        if _transient_provider_notice(message):
            return ctx.event(
                AgentStreamEventType.STATUS,
                {
                    "text": message,
                    "provider_status": "provider/reconnecting",
                    "snapshot": True,
                },
                message_id=_TRANSIENT_NOTICE_MESSAGE_ID,
            )
        return ctx.event(AgentStreamEventType.ERROR, {"message": message})

    def _remember_collab_threads(
        self, tool_name: str, args: Dict[str, Any], ctx: NormalizeContext
    ) -> None:
        """Record which child threads a main-agent collab call addressed.

        ``spawnAgent`` owns the local worker thread; ``sendInput`` may also
        address pre-existing remote peer threads. The registry lets us place
        ``code-mode-nested`` work on the spawned worker when the provider does
        not repeat an explicit ``threadId`` on every nested item."""
        raw = args.get("receiverThreadIds") if isinstance(args, dict) else None
        if not isinstance(raw, list):
            return
        ids = {t for t in raw if isinstance(t, str) and t.strip()}
        if not ids:
            return
        if tool_name in _COLLAB_SPAWN_TOOLS:
            target = self._spawned_threads
        elif tool_name in _COLLAB_MESSAGE_TOOLS:
            target = self._messaged_threads
        else:
            return
        target.setdefault(ctx.session_id, set()).update(ids)

    @staticmethod
    def _nested_parent_token(item_id: str) -> Optional[str]:
        """Extract the host call token from a ``code-mode-nested`` item id.

        Real id: ``code-mode-nested:29:<host_call_id>:exec-<uuid>``. All exec
        items of one nested code-mode host share ``<host_call_id>``; each
        parallel spawned worker gets a distinct host, so in a multi-spawn turn
        this token — not the constant depth ``29`` — disambiguates the owner.
        Returns ``None`` for malformed/non-nested ids."""
        parts = item_id.split(":")
        if len(parts) >= 3 and parts[0] == "code-mode-nested" and parts[2]:
            return parts[2]
        return None

    def _remember_nested_owner(self, ctx: NormalizeContext, item_id: str, thread_id: str) -> None:
        parent = self._nested_parent_token(item_id)
        if parent is None:
            return
        owners = self._nested_owner_by_parent.setdefault(ctx.session_id, {})
        known = owners.get(parent)
        # First authoritative sighting wins; a contradiction is left untouched
        # (real protocol keeps one host per worker, so this never flips).
        if known is None:
            owners[parent] = thread_id

    def _code_mode_thread(self, ctx: NormalizeContext, item_id: str) -> str:
        """Resolve the owning thread for a ``code-mode-nested`` item.

        Order:
        1. The host call token learned from a prior explicit-owner record of
           this same nested host (multi-spawn attribution).
        2. The unique thread the main agent spawned (single local worker).
        3. The unique addressed (spawned-or-messaged) thread.
        4. A stable synthetic group derived from the nesting token, so
           ambiguous work still nests off the main stream instead of being
           flattened into the main agent's bubble."""
        parent = self._nested_parent_token(item_id)
        if parent is not None:
            owner = self._nested_owner_by_parent.get(ctx.session_id, {}).get(parent)
            if owner is not None:
                return owner
        spawned = self._spawned_threads.get(ctx.session_id, set())
        if len(spawned) == 1:
            return next(iter(spawned))
        union = spawned | self._messaged_threads.get(ctx.session_id, set())
        if len(union) == 1:
            return next(iter(union))
        depth = "nested"
        parts = item_id.split(":")
        if len(parts) >= 2 and parts[0] == "code-mode-nested" and parts[1]:
            depth = parts[1]
        return f"code-mode-{depth}"

    @staticmethod
    def _item_owner_thread(item: Any) -> Optional[str]:
        """Owner thread id carried inside an ``item`` payload, if any.

        Observed/candidate fields across app-server protocol versions:
        top-level ``threadId`` / ``thread_id`` and collab's
        ``senderThreadId`` / ``agentThreadId``. Non-string/blank → None."""
        if not isinstance(item, dict):
            return None
        for key in ("threadId", "thread_id", "senderThreadId", "agentThreadId"):
            value = _clean_thread_id(item.get(key))
            if value is not None:
                return value
        return None

    def _resolve_sub_thread(
        self,
        params: Any,
        ctx: NormalizeContext,
        item_id: Optional[str] = None,
        item: Any = None,
    ) -> Optional[str]:
        """Return the sub-agent thread id a record belongs to, or ``None`` for
        a main-thread (or unattributable) record.

        1. An explicit ``params.threadId`` (or an owner id inside ``item``)
           differing from the user's main thread is authoritative — the collab
           protocol's own routing field. For a ``code-mode-nested`` item it
           also teaches the host-token → child-thread mapping used to place
           the item's later thread-less records (multi-spawn attribution).
        2. Otherwise a ``code-mode-nested`` item id marks nested worker work;
           place it via the learned host owner, then the spawn/addressed-thread
           registry fallback.

        Never returns the main thread id itself. With no ``main_thread_id`` in
        context (one-shot/transcript paths) the explicit check is skipped and
        only the nested-id fallback can attribute — both degrade safely."""
        explicit: Optional[str] = None
        if isinstance(params, dict):
            explicit = _clean_thread_id(params.get("threadId"))
        if explicit is None:
            explicit = self._item_owner_thread(item)
        main_id = _clean_thread_id(ctx.main_thread_id)
        nested_id = _clean_thread_id(item_id)
        is_nested = nested_id is not None and nested_id.startswith(_CODE_MODE_NESTED_PREFIX)
        if explicit is not None and main_id is not None and explicit != main_id:
            if is_nested and nested_id is not None:
                self._remember_nested_owner(ctx, nested_id, explicit)
            return explicit
        if is_nested and nested_id is not None:
            return self._code_mode_thread(ctx, nested_id)
        return None

    @staticmethod
    def _child_lifecycle_event(
        ctx: NormalizeContext, sub_thread: str, status: str
    ) -> AgentStreamEvent:
        labels = {
            "active": "Sub-agent turn active",
            "completed": "Sub-agent turn completed",
            "failed": "Sub-agent turn failed",
            "cancelled": "Sub-agent turn cancelled",
        }
        return ctx.event(
            AgentStreamEventType.STATUS,
            {
                "text": labels[status],
                "provider_status": "subagent_lifecycle",
                "subagent_status": status,
                "snapshot": True,
            },
            message_id=f"subagent-lifecycle:{sub_thread}",
            sub_thread_id=sub_thread,
        )
