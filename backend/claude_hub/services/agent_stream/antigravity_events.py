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

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...models import (
    AgentStreamEvent,
    AgentStreamEventType,
    AgentType,
    ManagedSession,
)
from ...models.agent_stream import normalize_provider_usage
from .base import AgentStreamAdapter, NormalizeContext, is_substantive_answer

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "thinking_tokens",
    "cache_read_tokens",
    "total_tokens",
)

# agy records retryable model/SSE errors in the conversation trajectory as
# ``step_type=17`` ("API error (attempt N): ...") and retries in place.
# However, ``steps.BuildJSONOutput`` scans the entire conversation's steps via
# ``LastStepError`` without scoping to the current turn or checking whether a
# final ``agent_response`` succeeded after the retry, contaminating ``result``
# on completed turns with ``status="ERROR"`` and the stale retry message.
_TRANSIENT_RETRY_ERROR_RE = re.compile(r"^API error \(attempt \d+\):\s", re.IGNORECASE)
_STREAM_GENERATE_EOF_RE = re.compile(
    r"streamGenerateContent(?:\?alt=sse)?[\"']?:\s*(?:unexpected\s+)?EOF\b",
    re.IGNORECASE,
)
# ``printmode.postResponseFailure`` appends this suffix when an error step
# occurred AFTER the last response-bearing step in the turn.
_TRUNCATED_RESPONSE_SUFFIX = "(response may be truncated)"


@dataclass
class _UsageState:
    turn_key: Optional[Tuple[Optional[int], Optional[str]]] = None
    steps: Dict[int, Dict[str, int]] = field(default_factory=dict)
    conversation_id: Optional[str] = None
    previous_num_turns: Optional[int] = None
    previous_usage: Optional[Dict[str, int]] = None
    saw_tool_step: bool = False
    post_tool_text: str = ""
    saw_error_step: bool = False
    saw_error_after_response: bool = False


class AntigravityEventAdapter(AgentStreamAdapter):
    adapter_id = "antigravity-native"
    schema_version = 1
    spec_agent_type = AgentType.ANTIGRAVITY
    supports_transcript_discovery = False

    def __init__(self) -> None:
        super().__init__()
        self._usage_states: Dict[str, _UsageState] = {}

    def discover_source(self, session: ManagedSession) -> Optional[Path]:
        return None

    def normalize_line(self, raw: Dict[str, Any], ctx: NormalizeContext) -> List[AgentStreamEvent]:
        if not isinstance(raw, dict):
            return []
        kind = raw.get("event")
        if kind == "init":
            state = self._usage_state(ctx)
            state.steps.clear()
            state.previous_num_turns = None
            state.previous_usage = None
            state.saw_tool_step = False
            state.post_tool_text = ""
            state.saw_error_step = False
            state.saw_error_after_response = False
            conversation_id = raw.get("conversation_id")
            state.conversation_id = (
                conversation_id if isinstance(conversation_id, str) and conversation_id else None
            )
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
        if not isinstance(step_index, int) or isinstance(step_index, bool) or step_index < 0:
            return []
        turn_state = self._usage_state(ctx)
        usage = _usage_counters(raw.get("usage"))
        if usage is not None:
            # ACTIVE and DONE may both carry the same step's snapshot. Replacing
            # it also handles a final snapshot that differs from an early one.
            turn_state.steps[step_index] = usage
        call_id = f"agy:{ctx.turn_id or 'turn'}:{step_index}"

        if step_type == "agent_response":
            # ``text_delta`` fragments stream on both ACTIVE (incremental
            # tokens) and the final DONE (last fragment); append each in
            # arrival order. The terminal ``result.response`` then reconciles
            # to an empty suffix instead of duplicating the answer.
            text = raw.get("text_delta")
            if isinstance(text, str) and text:
                self._append_text_delta(ctx, text)
                turn_state.post_tool_text += text
                if is_substantive_answer(turn_state.post_tool_text):
                    turn_state.saw_error_after_response = False
                events.append(ctx.event(AgentStreamEventType.TEXT_DELTA, {"text": text}))
        elif step_type == "tool":
            raw_info = raw.get("tool_info")
            tool_info: Dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
            name = raw.get("tool_name") or tool_info.get("name") or "tool"
            if state in {"ACTIVE", "DONE"}:
                turn_state.saw_tool_step = True
                turn_state.post_tool_text = ""
                turn_state.saw_error_after_response = False
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
        elif step_type == "error_message" or state == "ERROR":
            turn_state.saw_error_step = True
            turn_state.saw_error_after_response = True
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

        turn_state = self._usage_state(ctx)
        if response_text:
            suffix = self._reconcile_text(ctx, response_text)
            if suffix:
                self._append_text_delta(ctx, suffix)
                turn_state.post_tool_text += suffix
                events.append(ctx.event(AgentStreamEventType.TEXT_DELTA, {"text": suffix}))

        has_substantive_final_reply = is_substantive_answer(
            turn_state.post_tool_text
            if turn_state.saw_tool_step
            else (turn_state.post_tool_text or response_text)
        )
        raw_error = result.get("error")
        error_text = raw_error.strip() if isinstance(raw_error, str) else ""

        status_value = str(result.get("status", "")).upper()
        if (
            status_value == "ERROR"
            and not headless_denied
            and _is_recovered_turn_result_error(
                error_text,
                has_substantive_final_reply=has_substantive_final_reply,
                saw_error_step=turn_state.saw_error_step,
                saw_error_after_response=turn_state.saw_error_after_response,
            )
        ):
            status = "completed"
        elif status_value == "ERROR" or headless_denied:
            if headless_denied:
                tools = ", ".join(dict.fromkeys(denied_names)) or "a tool"
                message = (
                    f"Antigravity could not run {tools}: headless Chat cannot prompt for "
                    "permission. Enable Solo Mode for this Chat (auto-approves tools) and retry."
                )
            else:
                message = error_text or "Antigravity turn failed"
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
        usage = self._turn_usage(result, ctx)
        if usage is not None:
            completed["usage"] = usage
        events.append(ctx.event(AgentStreamEventType.TURN_COMPLETED, completed))
        self._clear_turn_state(ctx)
        return events

    def _usage_state(self, ctx: NormalizeContext) -> _UsageState:
        state = self._usage_states.get(ctx.session_id)
        if state is None:
            state = _UsageState()
            self._usage_states[ctx.session_id] = state
        turn_key = (ctx.run_epoch, ctx.turn_id)
        if state.turn_key != turn_key:
            # A cancelled turn may have no result; its steps must not leak into
            # the next turn. Conversation totals survive ordinary turn changes.
            state.turn_key = turn_key
            state.steps.clear()
            state.saw_tool_step = False
            state.post_tool_text = ""
            state.saw_error_step = False
            state.saw_error_after_response = False
        return state

    def _turn_usage(
        self, result: Dict[str, Any], ctx: NormalizeContext
    ) -> Optional[Dict[str, Any]]:
        state = self._usage_state(ctx)
        cumulative = _usage_counters(result.get("usage"))
        raw_turns = result.get("num_turns")
        num_turns = (
            raw_turns
            if isinstance(raw_turns, int) and not isinstance(raw_turns, bool) and raw_turns > 0
            else None
        )
        conversation_id = result.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id:
            conversation_id = state.conversation_id
        usage: Optional[Dict[str, int]] = None
        if state.steps:
            usage = {key: sum(step[key] for step in state.steps.values()) for key in _USAGE_FIELDS}
        elif cumulative is not None:
            if num_turns == 1 and (
                state.previous_usage is None or conversation_id != state.conversation_id
            ):
                usage = cumulative
            elif (
                conversation_id is not None
                and conversation_id == state.conversation_id
                and num_turns is not None
                and state.previous_num_turns == num_turns - 1
                and state.previous_usage is not None
                and all(cumulative[key] >= state.previous_usage[key] for key in _USAGE_FIELDS)
            ):
                usage = {key: cumulative[key] - state.previous_usage[key] for key in _USAGE_FIELDS}
        # result.usage is conversation-cumulative, including after --conversation
        # resume. Without per-step usage or a contiguous baseline it is unknown,
        # not a new charge for the entire earlier conversation.
        state.conversation_id = conversation_id
        state.previous_num_turns = num_turns
        state.previous_usage = cumulative
        state.steps.clear()
        state.saw_tool_step = False
        state.post_tool_text = ""
        state.saw_error_step = False
        state.saw_error_after_response = False
        return _normalize_usage(usage) if usage is not None else None


def _is_recovered_turn_result_error(
    error_message: str,
    *,
    has_substantive_final_reply: bool,
    saw_error_step: bool,
    saw_error_after_response: bool,
) -> bool:
    """Whether ``result`` carries a stale/recovered error after a completed reply.

    ``agy``'s ``steps.BuildJSONOutput`` scans the entire conversation trajectory
    via ``LastStepError`` even when ``runTurn`` succeeded and called
    ``EmitFinal("SUCCESS")``. Any earlier ``step_type=17`` retry step (such as
    ``API error (attempt 1): ... streamGenerateContent?alt=sse: EOF`` or
    ``API error (attempt 1): ... stream idle timeout``) therefore overwrites
    ``result.status`` to ``"ERROR"`` on that turn and every subsequent turn in
    the conversation.

    We suppress the false trailing error only when:
    1. The turn delivered a substantive final reply (post-tool when tools ran),
    2. ``agy`` did not mark the reply as truncated (``(response may be truncated)``),
    3. No unrecovered ``error_message`` step occurred after the final
       ``agent_response`` step, and
    4. The error is a transient ``API error (attempt N): ...`` retry notice, a
       trailing ``streamGenerateContent`` EOF after the full reply, or a
       historical trajectory error from an earlier step/turn
       (``not saw_error_step or not saw_error_after_response``) with a
       recoverable retry/stream error signature.
    """
    if not has_substantive_final_reply or not error_message:
        return False
    if error_message.endswith(_TRUNCATED_RESPONSE_SUFFIX):
        return False
    if saw_error_after_response:
        return False
    if _TRANSIENT_RETRY_ERROR_RE.match(error_message):
        return True
    if _STREAM_GENERATE_EOF_RE.search(error_message):
        return True
    return False


def _usage_counters(raw: Any) -> Optional[Dict[str, int]]:
    if not isinstance(raw, dict) or not any(key in raw for key in _USAGE_FIELDS):
        return None
    counters: Dict[str, int] = {}
    for key in _USAGE_FIELDS:
        value = raw.get(key, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        counters[key] = value
    return counters


def _normalize_usage(usage: Dict[str, int]) -> Optional[Dict[str, Any]]:
    # agy input_tokens excludes cache hits; Hub's shared normalizer expects
    # input to include them and subtracts cached tokens when computing total.
    return normalize_provider_usage(
        {
            "input_tokens": usage["input_tokens"] + usage["cache_read_tokens"],
            "cache_read_input_tokens": usage["cache_read_tokens"],
            "output_tokens": usage["output_tokens"],
            "reasoning_tokens": usage["thinking_tokens"],
            "total_tokens": usage["total_tokens"],
        },
        "antigravity",
    )


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
