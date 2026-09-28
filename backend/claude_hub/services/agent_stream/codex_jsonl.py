"""Codex CLI rollout transcript adapter.

Tails ``~/.codex/sessions/**/rollout-*.jsonl`` — the append-only rollout log
Codex writes per session.

Mapping (chosen to avoid the double-emit Codex produces when it persists both
an ``event_msg`` and its mirrored ``response_item``):

- ``user_message`` (event_msg)            → ``turn_started``
- ``message`` (response_item, role=assistant) → ``text_delta``
- ``agent_reasoning`` (event_msg)         → ``thinking_delta``
- ``function_call`` / ``custom_tool_call`` (response_item) → ``tool_call_started``
- ``function_call_output`` / ``custom_tool_call_output`` → ``tool_call_completed``
- ``error`` / ``stream_error`` (event_msg) → ``error``
- ``task_complete`` (event_msg)            → ``turn_completed``

Source-location reuses ``ttyd_manager``'s ``_codex_candidates_for_cwd`` /
``_codex_scan_sessions`` / ``_pick_backfill_session``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ...models import (
    AgentStreamEvent,
    AgentStreamEventType,
    ManagedSession,
    StreamCapabilities,
)
from ...models.agent_stream import normalize_provider_usage
from ..ttyd_manager import (
    _codex_candidates_for_cwd,
    _codex_scan_sessions,
    _pick_backfill_session,
)
from .base import (
    AgentStreamAdapter,
    NormalizeContext,
    discover_source_cached,
    resolve_cwd,
    resolve_process_hint,
)
from .fork_seed import strip_fork_seed_history
from .native import (
    _CODEX_QUESTION_METHODS,
    codex_normalize_questions,
    strip_hub_runtime_guidance,
)

_FLAT_OBJ_RE = re.compile(r"\{[^{}]*\}")
_CMD_RE = re.compile(r'"cmd"\s*:\s*"((?:[^"\\]|\\.)*)"')
# A transient provider control-plane notice delivered through the ``error``
# channel. TraeX/Codex report their OWN transport reconnect/backoff attempts
# (e.g. "Reconnecting… 1/5") as an error notification while the turn stays
# alive and keeps queueing/generating. These are NOT terminal: mapping them to
# ``error`` made the frontend treat the turn as finished (dropping its
# in-flight lock and hiding Stop) for what was really a recoverable retry. Map
# them to a single coalesced STATUS instead. Real, fatal provider failures
# (turn/completed error, process death) keep their own terminal paths.
_TRANSIENT_NOTICE_RE = re.compile(r"^\s*(?:reconnect(?:ing|ed)?|retrying)\b", re.IGNORECASE)
_TRANSIENT_NOTICE_MESSAGE_ID = "provider-status:reconnect"


def _transient_provider_notice(message: Any) -> bool:
    """True when an ``error``-channel message is a recoverable reconnect."""
    return isinstance(message, str) and bool(_TRANSIENT_NOTICE_RE.match(message))


# Prefix TraeX's code-mode host gives the nested sub-agent's item ids, e.g.
# ``code-mode-nested:29:call_…:exec-…``. The ``29`` token is a nesting depth /
# cell namespace, NOT the receiver thread id, so it cannot by itself split two
# concurrent sub-agents — it only marks "this item ran inside the nested
# code-mode worker". See ``_code_mode_thread`` for the attribution fallback.
_CODE_MODE_NESTED_PREFIX = "code-mode-nested:"

# Tool names on the Codex/TraeX collab protocol that address a child thread.
# ``spawnAgent`` launches (and owns) the local worker; ``sendInput`` posts a
# follow-up to one or more already-running threads.
_COLLAB_SPAWN_TOOLS = {"spawnAgent"}
_COLLAB_MESSAGE_TOOLS = {"sendInput"}


def _clean_thread_id(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


def _codex_tool_args(raw_input: Any) -> Dict[str, Any]:
    """Best-effort parse of a codex ``custom_tool_call.input`` string."""
    if not isinstance(raw_input, str) or not raw_input.strip():
        return {}
    try:
        parsed = json.loads(raw_input)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    m = _FLAT_OBJ_RE.search(raw_input)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    m = _CMD_RE.search(raw_input)
    if m:
        try:
            return {"cmd": json.loads('"' + m.group(1) + '"')}
        except (json.JSONDecodeError, ValueError):
            return {"cmd": m.group(1)}
    return {"input": raw_input[:200]}


def _codex_extract_text(content: Any) -> str:
    """Flatten a codex content value (list of blocks or string) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _codex_parse_arguments(arguments: Any) -> Dict[str, Any]:
    """Parse a ``function_call.arguments`` JSON string into a dict."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
            if isinstance(parsed, dict):
                return parsed
            return {"raw": arguments}
        except (json.JSONDecodeError, ValueError):
            return {"raw": arguments[:200]}
    return {}


class CodexJsonlAdapter(AgentStreamAdapter):
    """Adapter for Codex CLI's ``~/.codex/sessions/**/rollout-*.jsonl`` logs."""

    adapter_id = "codex-jsonl"
    schema_version = 1
    supports_approval_ui = True
    supports_tool_timeline = True

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

    def capabilities(self, session: ManagedSession) -> StreamCapabilities:
        """Advertise structured Codex chat only after its rollout exists."""
        if discover_source_cached(self, session) is None:
            return StreamCapabilities(
                structured=False,
                adapter_id=self.adapter_id,
                schema_version=self.schema_version,
                sources=[],
                supports_approval_ui=self.supports_approval_ui,
                supports_tool_timeline=self.supports_tool_timeline,
            )
        return super().capabilities(session)

    # ── source discovery ─────────────────────────────────────────────────────

    def discover_source(self, session: ManagedSession) -> Optional[Path]:
        cwd = resolve_cwd(session)
        candidates = _codex_candidates_for_cwd(cwd)
        if not candidates:
            return None

        _, agent_session_id = resolve_process_hint(session)
        if agent_session_id:
            return self._find_pinned(candidates, agent_session_id, cwd)

        created = self._session_created_epoch(session)
        if created is None:
            return None
        scored = [(abs(start - created), sid, cand_path) for start, sid, cand_path in candidates]
        picked = _pick_backfill_session(created, scored)
        if picked:
            for _start, sid, cand_path in candidates:
                if sid == picked:
                    p = Path(cand_path)
                    return p if p.is_file() else None
        return None

    @staticmethod
    def _find_pinned(candidates: List[tuple], agent_session_id: str, cwd: str) -> Optional[Path]:
        for _start, sid, path in candidates:
            if sid == agent_session_id:
                p = Path(path)
                return p if p.is_file() else None
        entry = _codex_scan_sessions().get(agent_session_id)
        if entry is None or not entry.cwd:
            return None
        try:
            same_cwd = Path(entry.cwd).resolve() == Path(cwd).resolve()
        except OSError:
            same_cwd = entry.cwd == cwd
        if same_cwd:
            p = Path(entry.path)
            return p if p.is_file() else None
        return None

    @staticmethod
    def _session_created_epoch(session: ManagedSession) -> Optional[float]:
        created = session.created_at
        if isinstance(created, datetime):
            return created.timestamp()
        if isinstance(created, (int, float)):
            return float(created)
        return None

    # ── normalization ────────────────────────────────────────────────────────

    def normalize_line(self, raw: Dict[str, Any], ctx: NormalizeContext) -> List[AgentStreamEvent]:
        events: List[AgentStreamEvent] = []
        if not isinstance(raw, dict):
            return events

        # Native app-server transport emits JSON-RPC notifications with a
        # ``method`` field (slash-delimited, e.g. ``item/agentMessage/delta``).
        # Transcript files use ``type``/``payload`` instead.
        method = raw.get("method")
        if isinstance(method, str):
            return self._normalize_notification(method, raw.get("params"), ctx)

        top_type = raw.get("type")
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            payload = raw
        payload_type = payload.get("type")
        if not isinstance(payload_type, str):
            return events

        if top_type == "event_msg":
            events.extend(self._normalize_event_msg(payload, payload_type, ctx))
        elif top_type == "response_item":
            events.extend(self._normalize_response_item(payload, payload_type, ctx))
        return events

    def _error_or_status_event(self, ctx: NormalizeContext, message: str) -> AgentStreamEvent:
        """Map an error-channel message to ERROR, or to a coalesced STATUS.

        A recoverable provider reconnect/backoff notice (``Reconnecting… n/m``)
        is not a turn-terminal error: the turn is still alive. Emit it as one
        stable-id snapshot STATUS (the UI replaces it in place and never treats
        it as terminal) instead of an ERROR that would drop the active-turn
        lock and hide Stop.
        """
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

    # ── sub-agent thread attribution ─────────────────────────────────────────

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

    def _normalize_notification(
        self, method: str, params: Any, ctx: NormalizeContext
    ) -> List[AgentStreamEvent]:
        """Normalize a Codex app-server JSON-RPC notification."""
        events: List[AgentStreamEvent] = []
        if not isinstance(params, dict):
            return events
        sub_thread = self._resolve_sub_thread(params, ctx, params.get("itemId"))
        if method == "turn/started":
            turn = params.get("turn")
            provider_turn_id = turn.get("id") if isinstance(turn, dict) else params.get("turnId")
            payload: Dict[str, Any] = {"summary": ""}
            if isinstance(provider_turn_id, str) and provider_turn_id:
                payload["provider_turn_id"] = provider_turn_id
            events.append(ctx.event(AgentStreamEventType.TURN_STARTED, payload))
        elif method == "thread/tokenUsage/updated":
            usage = normalize_provider_usage(params, "codex")
            if usage is not None:
                self._latest_usage[ctx.session_id] = usage
        elif method.startswith("thread/goal/") or method.startswith("goal/"):
            events.append(
                ctx.event(
                    AgentStreamEventType.STATUS,
                    {"provider_notification": method, "goal": dict(params)},
                )
            )
        elif method == "turn/completed":
            turn = params.get("turn")
            status = "completed"
            if isinstance(turn, dict):
                turn_status = turn.get("status")
                if turn_status == "interrupted":
                    turn_status = "cancelled"
                if turn_status in ("failed", "cancelled", "completed"):
                    status = turn_status
                error = turn.get("error")
                if isinstance(error, dict) and error.get("message"):
                    events.append(
                        ctx.event(AgentStreamEventType.ERROR, {"message": error["message"]})
                    )
            completed: Dict[str, Any] = {"status": status}
            usage = normalize_provider_usage(params, "codex") or self._latest_usage.pop(
                ctx.session_id, None
            )
            if usage is not None:
                completed["usage"] = usage
            events.append(ctx.event(AgentStreamEventType.TURN_COMPLETED, completed))
            # Finalize any in-flight reasoning statuses so a cancelled/
            # interrupted turn (or any terminal without ``item/completed`` for
            # reasoning) never shows a stale "Thinking…" indicator. The final
            # status replaces the in-flight one in place (same message_id +
            # snapshot).
            turn_key = ctx.turn_id or ctx.session_id
            inflight = self._inflight_reasoning.pop(turn_key, {})
            for rs_id, sub_thread in inflight.items():
                final_text = (
                    "Thinking interrupted"
                    if status in ("cancelled", "failed")
                    else "Done thinking"
                )
                events.append(
                    ctx.event(
                        AgentStreamEventType.STATUS,
                        {
                            "text": final_text,
                            "provider_status": "reasoning",
                            "snapshot": True,
                        },
                        message_id=f"reasoning:{rs_id}",
                        sub_thread_id=sub_thread,
                    )
                )
        elif method == "error":
            error = params.get("error")
            if isinstance(error, dict) and error.get("message"):
                events.append(self._error_or_status_event(ctx, error["message"]))
        elif method in {"item/started", "item/completed"}:
            events.extend(
                self._normalize_tool_item(params.get("item"), method, ctx, params.get("threadId"))
            )
        elif method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str) and delta:
                events.append(
                    ctx.event(
                        AgentStreamEventType.TEXT_DELTA,
                        {"text": delta},
                        sub_thread_id=sub_thread,
                    )
                )
        elif method == "item/reasoning/textDelta":
            delta = params.get("delta")
            if isinstance(delta, str) and delta:
                events.append(
                    ctx.event(
                        AgentStreamEventType.THINKING_DELTA,
                        {"text": delta},
                        sub_thread_id=sub_thread,
                    )
                )
        elif method == "item/plan/delta":
            delta = params.get("delta")
            if isinstance(delta, str) and delta:
                events.append(
                    ctx.event(
                        AgentStreamEventType.TEXT_DELTA,
                        {"text": delta, "plan": True, "plan_kind": "proposal"},
                        message_id=self._plan_message_id(params.get("itemId"), ctx),
                    )
                )
        elif method == "turn/plan/updated":
            # This is the execution checklist, distinct from a proposed plan.
            steps = params.get("plan")
            if isinstance(steps, list):
                lines = []
                explanation = params.get("explanation")
                if isinstance(explanation, str) and explanation.strip():
                    lines.append(explanation.strip())
                for step in steps:
                    if not isinstance(step, dict) or not isinstance(step.get("step"), str):
                        continue
                    label = step["step"].strip()
                    if not label:
                        continue
                    step_status = step.get("status")
                    marker = "x" if step_status == "completed" else " "
                    suffix = " (in progress)" if step_status == "inProgress" else ""
                    lines.append(f"- [{marker}] {label}{suffix}")
                events.append(
                    ctx.event(
                        AgentStreamEventType.TEXT_DELTA,
                        {
                            "text": "\n".join(lines),
                            "plan": True,
                            "plan_kind": "progress",
                            "snapshot": True,
                        },
                        message_id=f"plan-progress:{ctx.turn_id or params.get('turnId', '')}",
                    )
                )
        elif method in _CODEX_QUESTION_METHODS:
            events.extend(self._normalize_question(params, ctx))
        return events

    @staticmethod
    def _plan_message_id(item_id: Any, ctx: NormalizeContext) -> str:
        return (
            f"plan:{item_id if isinstance(item_id, str) and item_id else ctx.turn_id or 'proposal'}"
        )

    def _normalize_tool_item(
        self,
        item: Any,
        method: str,
        ctx: NormalizeContext,
        params_thread_id: Any = None,
    ) -> List[AgentStreamEvent]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return []
        kind = item.get("type")
        if kind == "plan":
            # The protocol explicitly says deltas need not match the final
            # plan. Publish an authoritative replacement, including empty
            # snapshots, instead of dropping it or appending it twice.
            if method != "item/completed" or not isinstance(item.get("text"), str):
                return []
            return [
                ctx.event(
                    AgentStreamEventType.TEXT_DELTA,
                    {
                        "text": item["text"],
                        "plan": True,
                        "plan_kind": "proposal",
                        "snapshot": True,
                    },
                    message_id=self._plan_message_id(item["id"], ctx),
                )
            ]
        if kind == "reasoning":
            # The current app-server emits ``item/started`` + ``item/completed``
            # for reasoning items but NO ``item/reasoning/textDelta`` — the
            # reasoning content/summary are empty and the actual reasoning is
            # encrypted server-side (only token counts are exposed via
            # ``thread/tokenUsage/updated``). Emit a truthful in-flight status
            # so the user sees process activity without fabricating reasoning
            # content. The stable message_id + snapshot lets the completion
            # update replace the start indicator in place.
            if method not in ("item/started", "item/completed"):
                return []
            text = "Thinking…" if method == "item/started" else "Done thinking"
            # Route to the owning child thread (a child's reasoning must not
            # appear on the main stream).
            sub_thread = self._resolve_sub_thread(
                {"threadId": params_thread_id} if isinstance(params_thread_id, str) else {},
                ctx,
                item["id"],
                item,
            )
            # Track in-flight reasoning so a cancelled/interrupted turn can
            # finalize the status instead of leaving "Thinking…" stale.
            turn_key = ctx.turn_id or ctx.session_id
            if method == "item/started":
                self._inflight_reasoning.setdefault(turn_key, {})[item["id"]] = sub_thread
            else:
                self._inflight_reasoning.get(turn_key, {}).pop(item["id"], None)
            return [
                ctx.event(
                    AgentStreamEventType.STATUS,
                    {
                        "text": text,
                        "provider_status": "reasoning",
                        "snapshot": True,
                    },
                    message_id=f"reasoning:{item['id']}",
                    sub_thread_id=sub_thread,
                )
            ]
        name: str
        args: Dict[str, Any]
        result: Any
        if kind == "commandExecution":
            name, args = "exec_command", {"cmd": item.get("command"), "cwd": item.get("cwd")}
            result = item.get("aggregatedOutput") or ""
        elif kind == "fileChange":
            name, args = "apply_patch", {"changes": item.get("changes", [])}
            result = args
        elif kind in {"mcpToolCall", "dynamicToolCall"}:
            name = str(item.get("tool") or kind)
            args = _codex_parse_arguments(item.get("arguments"))
            result = item.get("error") or item.get("result") or item.get("contentItems")
        elif kind == "webSearch":
            name, args = "web_search", {"query": item.get("query")}
            result = item.get("action")
        elif kind == "imageView":
            name, args = "view_image", {"path": item.get("path")}
            result = ""
        elif kind == "collabAgentToolCall":
            name = str(item.get("tool") or kind)
            args = {
                "prompt": item.get("prompt"),
                "receiverThreadIds": item.get("receiverThreadIds"),
            }
            result = item.get("agentsStates")
        else:
            # Text/reasoning/plan items already arrive as deltas.
            return []
        call_id = item["id"]
        # Collab calls (spawnAgent/sendInput) are issued BY the main agent and
        # stay on the main stream as instruction cards; they also teach us the
        # receiver threads. Every other item that carries a sub-thread id (or a
        # ``code-mode-nested`` item id) is the child's own work and is nested.
        is_collab = kind == "collabAgentToolCall"
        if is_collab and method == "item/started":
            self._remember_collab_threads(name, args, ctx)
        sub_thread: Optional[str] = None
        if not is_collab:
            sub_thread = self._resolve_sub_thread(
                {"threadId": params_thread_id} if isinstance(params_thread_id, str) else {},
                ctx,
                call_id,
                item,
            )
        if method == "item/started":
            return [
                ctx.event(
                    AgentStreamEventType.TOOL_CALL_STARTED,
                    {
                        "tool_call_id": call_id,
                        "name": name,
                        "args": args,
                    },
                    call_id=call_id,
                    sub_thread_id=sub_thread,
                )
            ]
        failed = item.get("status") in {"failed", "declined"} or item.get("success") is False
        if kind == "commandExecution" and item.get("exitCode") not in (None, 0):
            failed = True
        return [
            ctx.event(
                AgentStreamEventType.TOOL_CALL_COMPLETED,
                {
                    "tool_call_id": call_id,
                    "status": "failed" if failed else "completed",
                    "result": _codex_extract_text(result),
                },
                call_id=call_id,
                sub_thread_id=sub_thread,
            )
        ]

    def _normalize_question(
        self, params: Dict[str, Any], ctx: NormalizeContext
    ) -> List[AgentStreamEvent]:
        """Emit a tool call + approval card for a blocking user question.

        The app-server blocks the turn on ``requestUserInput``; the card lets
        the user answer, and the tailer routes the answer back as the JSON-RPC
        response (see ``CodexNativeSession.answer_pending_question``).
        """
        events: List[AgentStreamEvent] = []
        questions = codex_normalize_questions(params.get("questions"))
        if not questions:
            return events
        item_id = params.get("itemId")
        call_id = str(item_id) if item_id is not None else "request_user_input"
        events.append(
            ctx.event(
                AgentStreamEventType.TOOL_CALL_STARTED,
                {"name": "request_user_input", "args": params},
                call_id=call_id,
            )
        )
        events.append(
            ctx.event(
                AgentStreamEventType.APPROVAL_REQUIRED,
                {
                    "tool_call_id": call_id,
                    "kind": "ask_question",
                    "title": questions[0].get("prompt"),
                    "questions": questions,
                },
                call_id=call_id,
            )
        )
        return events

    def _normalize_event_msg(
        self, payload: Dict[str, Any], payload_type: str, ctx: NormalizeContext
    ) -> List[AgentStreamEvent]:
        events: List[AgentStreamEvent] = []
        if payload_type == "user_message":
            text = payload.get("message")
            if isinstance(text, str):
                # Strip the sentinel-wrapped Hub Chat runtime guidance the
                # transport prepends on the first turn so it never reaches the
                # persisted timeline or the UI (no-op on later turns).
                text = strip_hub_runtime_guidance(text)
                # Strip the one-shot fork-seed history block likewise.
                text = strip_fork_seed_history(text)
            if isinstance(text, str) and text.strip():
                events.append(ctx.event(AgentStreamEventType.TURN_STARTED, {"summary": text}))
        elif payload_type == "agent_reasoning":
            text = payload.get("text")
            if isinstance(text, str) and text:
                events.append(ctx.event(AgentStreamEventType.THINKING_DELTA, {"text": text}))
        elif payload_type in {"error", "stream_error"}:
            message = payload.get("message")
            if not isinstance(message, str) or not message.strip():
                detail = payload.get("error")
                if isinstance(detail, dict):
                    message = detail.get("message")
                elif isinstance(detail, str):
                    message = detail
            if isinstance(message, str) and message.strip():
                events.append(ctx.event(AgentStreamEventType.ERROR, {"message": message}))
        elif payload_type == "task_complete":
            completed: Dict[str, Any] = {"status": "completed"}
            summary = payload.get("last_agent_message")
            if isinstance(summary, str) and summary.strip():
                completed["summary"] = summary
            usage = normalize_provider_usage(payload, "codex")
            if usage is not None:
                completed["usage"] = usage
            events.append(ctx.event(AgentStreamEventType.TURN_COMPLETED, completed))
        return events

    def _normalize_response_item(
        self, payload: Dict[str, Any], payload_type: str, ctx: NormalizeContext
    ) -> List[AgentStreamEvent]:
        events: List[AgentStreamEvent] = []
        if payload_type == "message":
            role = payload.get("role")
            if role == "assistant":
                text = _codex_extract_text(payload.get("content"))
                if text:
                    events.append(ctx.event(AgentStreamEventType.TEXT_DELTA, {"text": text}))
        elif payload_type == "function_call":
            name = payload.get("name") or "unknown"
            call_id = payload.get("call_id")
            tool_call_id = call_id if isinstance(call_id, str) else None
            events.append(
                ctx.event(
                    AgentStreamEventType.TOOL_CALL_STARTED,
                    {
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "args": _codex_parse_arguments(payload.get("arguments")),
                    },
                    call_id=tool_call_id,
                )
            )
        elif payload_type == "function_call_output":
            call_id = payload.get("call_id")
            events.append(
                ctx.event(
                    AgentStreamEventType.TOOL_CALL_COMPLETED,
                    {
                        "tool_call_id": call_id,
                        "status": "completed",
                        "result": _codex_extract_text(payload.get("output")),
                    },
                    call_id=call_id if isinstance(call_id, str) else None,
                )
            )
        elif payload_type == "custom_tool_call":
            name = payload.get("name") or "unknown"
            call_id = payload.get("call_id")
            tool_call_id = call_id if isinstance(call_id, str) else None
            events.append(
                ctx.event(
                    AgentStreamEventType.TOOL_CALL_STARTED,
                    {
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "args": _codex_tool_args(payload.get("input")),
                    },
                    call_id=tool_call_id,
                )
            )
        elif payload_type == "custom_tool_call_output":
            call_id = payload.get("call_id")
            events.append(
                ctx.event(
                    AgentStreamEventType.TOOL_CALL_COMPLETED,
                    {
                        "tool_call_id": call_id,
                        "status": "completed",
                        "result": _codex_extract_text(payload.get("output")),
                    },
                    call_id=call_id if isinstance(call_id, str) else None,
                )
            )
        return events


class TraexJsonlAdapter(CodexJsonlAdapter):
    """Normalize live TraeX notifications without discovering Codex rollouts.

    TraeX terminal transcripts and edit-resend are not supported yet.
    """

    adapter_id = "traex-jsonl"
    # Terminal transcript structured surface is not wired (see docstring); only
    # the native app-server powers the structured Chat view.
    supports_transcript_discovery = False

    def _normalize_notification(
        self, method: str, params: Any, ctx: NormalizeContext
    ) -> List[AgentStreamEvent]:
        if isinstance(params, dict):
            text = self._traex_status_text(method, params)
            if text:
                return [
                    ctx.event(
                        AgentStreamEventType.STATUS,
                        {
                            "text": text,
                            "provider_status": method,
                            "snapshot": True,
                        },
                        message_id=f"traex-status:{method}",
                    )
                ]
        return super()._normalize_notification(method, params, ctx)

    @staticmethod
    def _traex_status_text(method: str, params: Dict[str, Any]) -> Optional[str]:
        """Translate TraeX-only lifecycle notices into visible timeline text."""
        provider_message = params.get("message")
        message = provider_message.strip() if isinstance(provider_message, str) else ""
        if method == "queue/status":
            state = params.get("state")
            if state == "queued":
                position = params.get("position")
                fallback = (
                    f"Queued for model capacity (position {position})."
                    if isinstance(position, int) and not isinstance(position, bool)
                    else "Queued for model capacity."
                )
                return message or fallback
            if state == "waiting":
                return message or "Waiting for model capacity."
            if state == "ready":
                return message or "Model is ready; starting the response."
            return message or "Model queue status updated."
        if method == "model/loopDetectedRecovering":
            attempt = params.get("attempt")
            maximum = params.get("maxAttempts")
            suffix = (
                f" (attempt {attempt}/{maximum})"
                if isinstance(attempt, int) and isinstance(maximum, int)
                else ""
            )
            reason = params.get("reason")
            detail = reason.strip() if isinstance(reason, str) else ""
            punctuation = f": {detail}" if detail else "."
            return message or f"Response loop detected; recovering{suffix}{punctuation}"
        if method in {"model/rerouted", "model/fallback"}:
            return message or (
                "The request was rerouted to another model."
                if method == "model/rerouted"
                else "The requested model was unavailable; using a fallback."
            )
        if method == "warning":
            return message or "TraeX reported a warning."
        return None

    def discover_source(self, session: ManagedSession) -> Optional[Path]:
        return None
