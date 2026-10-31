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
                # agy's own phrase when the tool never executed. A normal
                # command that merely prints this text reports
                # "Command exited with code N" and stays completed.
                status = "failed" if output.startswith("Command failed to run") else "completed"
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
        response_text = response if isinstance(response, str) else ""

        # agy is headless in print mode: a tool whose permission it cannot
        # prompt for is auto-denied and reported under ``denied_actions`` with
        # an empty response. A bare SUCCESS would render an empty, successful
        # turn; surface an actionable error pointing at Solo Mode instead.
        denied_names: List[str] = []
        denied = result.get("denied_actions")
        if isinstance(denied, list):
            for item in denied:
                if isinstance(item, dict):
                    label = item.get("display_name") or item.get("action")
                    if label:
                        denied_names.append(str(label))
        headless_denied = bool(denied_names) and not response_text.strip()

        if response_text:
            suffix = self._reconcile_text(ctx, response_text)
            if suffix:
                self._append_text_delta(ctx, suffix)
                events.append(ctx.event(AgentStreamEventType.TEXT_DELTA, {"text": suffix}))

        status_value = str(result.get("status", "")).upper()
        if status_value == "ERROR" or headless_denied:
            if headless_denied:
                tools = ", ".join(dict.fromkeys(denied_names)) or "a tool"
                message = (
                    f"Antigravity could not run {tools}: headless Chat cannot prompt for "
                    "permission. Enable Solo Mode for this Chat (auto-approves tools) and retry."
                )
            else:
                message = result.get("error") or "Antigravity turn failed"
                if not isinstance(message, str) or not message.strip():
                    message = "Antigravity turn failed"
                message = _actionable_headless_error(message)
            events.append(ctx.event(AgentStreamEventType.ERROR, {"message": message}))
            status = "failed"
        elif status_value in {"CANCELLED", "CANCELED", "INTERRUPTED"}:
            status = "cancelled"
        elif status_value == "SUCCESS":
            status = "completed"
        else:
            # Missing/unknown status with no terminal signal is a failure, not
            # a success — never let a malformed result close a turn cleanly.
            events.append(
                ctx.event(
                    AgentStreamEventType.ERROR,
                    {
                        "message": (
                            "Antigravity returned an unrecognized result status: "
                            f"{status_value or 'missing'!r}"
                        )
                    },
                )
            )
            status = "failed"

        completed: Dict[str, Any] = {"status": status}
        usage = normalize_provider_usage(_usage_for_normalizer(result), "antigravity")
        if usage is not None:
            completed["usage"] = usage
        events.append(ctx.event(AgentStreamEventType.TURN_COMPLETED, completed))
        self._clear_turn_state(ctx)
        return events


def _usage_for_normalizer(result: Dict[str, Any]) -> Dict[str, Any]:
    """Map agy's usage key names onto the shared normalizer's vocabulary."""
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return result
    mapped = dict(usage)
    # agy reports cached prompt tokens as cache_read_tokens and reasoning as
    # thinking_tokens; the shared helper keys off cache_read_input_tokens /
    # reasoning_tokens. Only fill when absent so real canonical keys win.
    if "cache_read_input_tokens" not in mapped and "cache_read_tokens" in mapped:
        mapped["cache_read_input_tokens"] = mapped["cache_read_tokens"]
    if "reasoning_tokens" not in mapped and "thinking_tokens" in mapped:
        mapped["reasoning_tokens"] = mapped["thinking_tokens"]
    return {**result, "usage": mapped}


def _actionable_headless_error(message: str) -> str:
    """Translate agy's headless permission/decode errors into Hub guidance."""
    lowered = message.lower()
    if (
        "headless mode cannot prompt" in lowered
        or "permission" in lowered
        or "unexpected eof" in lowered
        or "failed to decode stream input" in lowered
    ):
        return (
            f"{message} — Antigravity could not prompt for tool permission in headless Chat. "
            "Enable Solo Mode for this Chat (auto-approves tools) and retry."
        )
    return message
