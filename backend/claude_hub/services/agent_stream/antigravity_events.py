"""Normalize the Antigravity CLI's (agy) stream-json events.

agy emits three top-level record kinds on stdout::

    {"event": "init", "conversation_id": "...", "init": {...}}
    {"event": "step_update", "step_index": N, "state": "ACTIVE|DONE",
     "step_type": "user_input|agent_response|tool",
     "text_delta"?: "...",                 # final assistant text (agent_response)
     "tool_name"?: "run_command",
     "tool_info"?: {"name", "parameters", "output"},
     "usage"?: {...}}
    {"event": "result", "result": {
       "status": "SUCCESS|ERROR", "response": "...", "error": "...",
       "num_turns": 1, "usage": {...}}}

There is no transcript file: this is a stdout-only adapter like MASO's SSE
adapter (``supports_transcript_discovery=False``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from ...models import (
    AgentStreamEvent,
    AgentStreamEventType,
    AgentType,
    ManagedSession,
)
from ...models.agent_stream import normalize_provider_usage
from .base import AgentStreamAdapter, NormalizeContext


class AntigravityEventAdapter(AgentStreamAdapter):
    adapter_id = "antigravity-native"
    schema_version = 1
    spec_agent_type = AgentType.ANTIGRAVITY
    supports_transcript_discovery = False

    def discover_source(self, session: ManagedSession) -> Optional[Path]:
        return None

    def normalize_line(self, raw: Dict[str, Any], ctx: NormalizeContext) -> List[AgentStreamEvent]:
        if not isinstance(raw, dict):
            return []
        kind = raw.get("event")
        if kind == "init":
            return []
        if kind == "step_update":
            step = raw.get("step_update")
            if isinstance(step, dict):
                return self._normalize_step(step, ctx)
            return []
        if kind == "result":
            return self._normalize_result(raw, ctx)
        return []

    # ── step_update ─────────────────────────────────────────────────────────

    def _normalize_step(self, raw: Dict[str, Any], ctx: NormalizeContext) -> List[AgentStreamEvent]:
        events: List[AgentStreamEvent] = []
        step_index = raw.get("step_index")
        step_type = raw.get("step_type")
        state = raw.get("state")
        if not isinstance(step_index, int) or isinstance(step_index, bool):
            return []
        call_id = f"agy:{ctx.turn_id or 'turn'}:{step_index}"

        if step_type == "agent_response":
            # ``text_delta`` fragments stream on both ACTIVE (incremental
            # tokens) and the final DONE (last fragment); append each in
            # arrival order. The terminal ``result.response`` then reconciles
            # to an empty suffix instead of duplicating the answer.
            text = raw.get("text_delta")
            if isinstance(text, str) and text:
                self._append_text_delta(ctx, text)
                events.append(ctx.event(AgentStreamEventType.TEXT_DELTA, {"text": text}))
        elif step_type == "tool":
            raw_info = raw.get("tool_info")
            tool_info: Dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
            name = raw.get("tool_name") or tool_info.get("name") or "tool"
            if state == "ACTIVE":
                args = tool_info.get("parameters", {})
                if not isinstance(args, dict):
                    args = {"input": args}
                events.append(
                    ctx.event(
                        AgentStreamEventType.TOOL_CALL_STARTED,
                        {"tool_call_id": call_id, "name": str(name), "args": args},
                        call_id=call_id,
                    )
                )
            elif state == "DONE":
                output = tool_info.get("output", "")
                if not isinstance(output, str):
                    output = str(output)
                # "Command failed to run" means the tool never executed; an
                # exited-with-code result is a completed tool call whose
                # output the model reacts to.
                status = "failed" if output.startswith("Command failed") else "completed"
                events.append(
                    ctx.event(
                        AgentStreamEventType.TOOL_CALL_COMPLETED,
                        {
                            "tool_call_id": call_id,
                            "name": str(name),
                            "result": output,
                            "status": status,
                        },
                        call_id=call_id,
                    )
                )
        return events

    # ── result ──────────────────────────────────────────────────────────────

    def _normalize_result(
        self, raw: Dict[str, Any], ctx: NormalizeContext
    ) -> List[AgentStreamEvent]:
        events: List[AgentStreamEvent] = []
        result = raw.get("result")
        if not isinstance(result, dict):
            result = {}

        # ``step_update.text_delta`` usually carries the final answer; if it
        # was absent for this turn, reconcile against ``result.response`` here
        # so the Chat still shows the model's reply exactly once.
        response = result.get("response")
        if isinstance(response, str) and response:
            suffix = self._reconcile_text(ctx, response)
            if suffix:
                self._append_text_delta(ctx, suffix)
                events.append(ctx.event(AgentStreamEventType.TEXT_DELTA, {"text": suffix}))

        status_value = str(result.get("status", "")).upper()
        if status_value == "ERROR":
            message = result.get("error") or "Antigravity turn failed"
            if not isinstance(message, str) or not message.strip():
                message = "Antigravity turn failed"
            events.append(ctx.event(AgentStreamEventType.ERROR, {"message": message}))
            status = "failed"
        elif status_value in {"CANCELLED", "CANCELED", "INTERRUPTED"}:
            status = "cancelled"
        else:
            status = "completed"

        completed: Dict[str, Any] = {"status": status}
        usage = normalize_provider_usage(result, "antigravity")
        if usage is not None:
            completed["usage"] = usage
        events.append(ctx.event(AgentStreamEventType.TURN_COMPLETED, completed))
        self._clear_turn_state(ctx)
        return events
