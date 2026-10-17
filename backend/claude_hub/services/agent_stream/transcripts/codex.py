"""CodexTranscript with per-adapter state inherited from its typed owner."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ....models import (
    AgentStreamEvent,
    AgentStreamEventType,
    ManagedSession,
    StreamCapabilities,
)
from ....models.agent_stream import normalize_provider_usage
from ..base import NormalizeContext, discover_source_cached
from ..native import _CODEX_QUESTION_METHODS
from .codex_items import CodexItems


class CodexTranscript(CodexItems):
    """Normalize this protocol layer without duplicating adapter state."""

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

    def _normalize_notification(
        self, method: str, params: Any, ctx: NormalizeContext
    ) -> List[AgentStreamEvent]:
        """Normalize a Codex app-server JSON-RPC notification."""
        events: List[AgentStreamEvent] = []
        if not isinstance(params, dict):
            return events
        item = params.get("item")
        item_id = params.get("itemId")
        if not isinstance(item_id, str) or not item_id.strip():
            item_id = item.get("id") if isinstance(item, dict) else None
        sub_thread = self._resolve_sub_thread(params, ctx, item_id, item)
        # Late native child notifications cannot start a legacy Hub turn.
        # Transcript event_msg/response_item records take a separate path.
        if sub_thread is not None and not ctx.turn_id:
            return events
        if method == "turn/started":
            if sub_thread is not None:
                # Child lifecycle evidence must not open or complete a Hub turn.
                # Ignore late child starts after the parent has ended.
                if ctx.turn_id:
                    events.append(self._child_lifecycle_event(ctx, sub_thread, "active"))
                return events
            turn = params.get("turn")
            provider_turn_id = turn.get("id") if isinstance(turn, dict) else params.get("turnId")
            payload: Dict[str, Any] = {"summary": ""}
            if isinstance(provider_turn_id, str) and provider_turn_id:
                payload["provider_turn_id"] = provider_turn_id
            events.append(ctx.event(AgentStreamEventType.TURN_STARTED, payload))
        elif method == "thread/tokenUsage/updated":
            if sub_thread is not None:
                return events
            usage = normalize_provider_usage(params, "codex")
            if usage is not None:
                self._latest_usage[ctx.session_id] = usage
        elif method.startswith("thread/goal/") or method.startswith("goal/"):
            if sub_thread is not None:
                return events
            events.append(
                ctx.event(
                    AgentStreamEventType.STATUS,
                    {"provider_notification": method, "goal": dict(params)},
                )
            )
        elif method == "turn/completed":
            turn = params.get("turn")
            status = "completed"
            child_status: Optional[str] = None
            if isinstance(turn, dict):
                turn_status = turn.get("status")
                if turn_status == "interrupted":
                    turn_status = "cancelled"
                if turn_status in ("failed", "cancelled", "completed"):
                    status = turn_status
                    child_status = turn_status
                error = turn.get("error")
                if isinstance(error, dict) and error.get("message"):
                    if sub_thread is not None:
                        events.append(
                            self._error_or_status_event(ctx, error["message"], sub_thread)
                        )
                    else:
                        events.append(
                            ctx.event(AgentStreamEventType.ERROR, {"message": error["message"]})
                        )
            if sub_thread is not None and ctx.turn_id and child_status is not None:
                events.append(self._child_lifecycle_event(ctx, sub_thread, child_status))
            if sub_thread is None:
                completed: Dict[str, Any] = {"status": status}
                cached_usage = self._latest_usage.pop(ctx.session_id, None)
                usage = normalize_provider_usage(params, "codex") or cached_usage
                if usage is not None:
                    completed["usage"] = usage
                events.append(ctx.event(AgentStreamEventType.TURN_COMPLETED, completed))
            # Finalize any in-flight reasoning statuses so a cancelled/
            # interrupted turn (or any terminal without ``item/completed`` for
            # reasoning) never shows a stale "Thinking…" indicator. The final
            # status replaces the in-flight one in place (same message_id +
            # snapshot).
            turn_key = ctx.turn_id or ctx.session_id
            inflight = self._inflight_reasoning.get(turn_key, {})
            for rs_id, owner_thread in list(inflight.items()):
                # A child completion only finalizes its own reasoning; parent
                # completion still closes the entire user-turn display.
                if sub_thread is not None and owner_thread != sub_thread:
                    continue
                del inflight[rs_id]
                final_text = (
                    "Thinking interrupted" if status in ("cancelled", "failed") else "Done thinking"
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
                        sub_thread_id=owner_thread,
                    )
                )
            if not inflight:
                self._inflight_reasoning.pop(turn_key, None)
        elif method == "error":
            error = params.get("error")
            if isinstance(error, dict) and error.get("message"):
                events.append(self._error_or_status_event(ctx, error["message"], sub_thread))
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
