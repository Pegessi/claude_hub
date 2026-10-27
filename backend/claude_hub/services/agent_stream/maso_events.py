"""Normalize MASO's HTTP events without treating its mutable context as history.

MASO's ``post_llm_response`` carries the authoritative response snapshot.
``turn_end`` ends one LLM call, not the user run. Condensation archives and
rewrites its trajectory; only root ``agent_end`` completes a Hub turn.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...models import AgentStreamEvent, AgentStreamEventType, AgentType, ManagedSession
from .base import AgentStreamAdapter, NormalizeContext

# Fields a provider failure frame uses for its human-readable description, most
# specific first. MASO reports ``agent_error`` as ``str(exception)``; when the
# failure came from a provider frame its exception argument is that frame's
# mapping, so the string Hub receives is a Python repr of it.
_ERROR_DESCRIPTION_KEYS = ("message", "error", "detail", "reason", "description")

# Nesting bound for the description search. Provider frames wrap the real
# failure one or two levels deep ({"error": {"message": ...}}); deeper is
# payload structure, not prose.
_ERROR_DESCRIPTION_DEPTH = 3


@dataclass
class _Message:
    index: int = 0
    content: str = ""
    reasoning: str = ""


def _as_json(value: Any) -> Optional[str]:
    """Render upstream evidence as JSON so no Python repr can reach the user."""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        # Unreachable for SSE frames (always json.loads output), but a raise
        # here would make the tailer drop the whole error record.
        return None


def _error_mapping(error: Any) -> Optional[Dict[str, Any]]:
    """Return ``error`` as a mapping, recovering one MASO stringified away."""
    if isinstance(error, dict):
        return error
    if isinstance(error, str):
        text = error.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = ast.literal_eval(text)
            except (SyntaxError, ValueError, TypeError):
                return None  # ordinary prose that merely looks like a frame
            if isinstance(parsed, dict):
                return parsed  # a set literal parses too, and is not a frame
    return None


def _mapping_description(mapping: Dict[str, Any], depth: int) -> str:
    for key in _ERROR_DESCRIPTION_KEYS:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict) and depth > 1:
            nested = _mapping_description(value, depth - 1)
            if nested:
                return nested
    return ""


def _describe_error(error: Any) -> Tuple[str, Optional[str]]:
    """Split MASO's ``error`` field into (description, verbatim evidence).

    The description is empty when the provider reported no prose — a failure
    frame such as ``{'sequence_number': 1, 'type': 'error'}`` carries none. Hub
    cannot invent the cause, so the caller says so instead of printing the raw
    frame as if it were the error. The evidence is kept alongside, as JSON,
    because the adapter must not decide which upstream field a reader needs.
    """
    mapping = _error_mapping(error)
    if mapping is not None:
        return _mapping_description(mapping, _ERROR_DESCRIPTION_DEPTH), _as_json(mapping)
    if isinstance(error, str):
        return error.strip(), None
    if error is None:
        return "", None
    return "", _as_json(error)


class MasoEventAdapter(AgentStreamAdapter):
    adapter_id = "maso_http"
    schema_version = 1
    spec_agent_type = AgentType.MASO
    supports_transcript_discovery = False

    def __init__(self) -> None:
        super().__init__()
        self._messages: Dict[Tuple[str, Optional[int], str], _Message] = {}

    def discover_source(self, session: ManagedSession) -> Optional[Path]:
        return None

    def normalize_line(self, raw: Dict[str, Any], ctx: NormalizeContext) -> List[AgentStreamEvent]:
        kind = raw.get("type")
        data = raw.get("data")
        if not isinstance(data, dict):
            return []
        root = raw.get("root_agent_id")
        agent = data.get("agent_id", root)
        if not isinstance(agent, str) or not agent or not isinstance(root, str):
            return []
        child = agent if agent != root else None
        key = (ctx.session_id, ctx.run_epoch, agent)
        # Stop fences SSE before a terminal record can clean up this adapter.
        # Retain only the current epoch when the next turn begins.
        for item in list(self._messages):
            if item[0] == ctx.session_id and item[1] != ctx.run_epoch:
                self._messages.pop(item, None)
        state = self._messages.setdefault(key, _Message())

        def event(
            event_type: AgentStreamEventType,
            payload: Dict[str, Any],
            *,
            message_id: Optional[str] = None,
            call_id: Optional[str] = None,
        ) -> AgentStreamEvent:
            return ctx.event(
                event_type,
                payload,
                message_id=message_id,
                call_id=call_id,
                sub_thread_id=child,
            )

        def text_delta(field: str, text: Any, offset: Any = None) -> List[AgentStreamEvent]:
            if not isinstance(text, str) or not text:
                return []
            previous = getattr(state, field)
            if offset is None:
                # Authoritative response snapshot. A gap in volatile tokens is
                # repaired by its suffix; a rewritten prefix cannot be appended.
                if not text.startswith(previous):
                    return []
                delta = text[len(previous) :]
            else:
                if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                    return []
                if offset > len(previous):
                    return []  # await the authoritative response snapshot
                overlap = min(len(text), len(previous) - offset)
                if previous[offset : offset + overlap] != text[:overlap]:
                    return []
                delta = text[overlap:]
            if not delta:
                return []
            setattr(state, field, previous + delta)
            role = "assistant" if field == "content" else "thinking"
            return [
                event(
                    (
                        AgentStreamEventType.TEXT_DELTA
                        if field == "content"
                        else AgentStreamEventType.THINKING_DELTA
                    ),
                    {"text": delta},
                    message_id=f"{ctx.turn_id}:maso:{agent}:{state.index}:{role}",
                )
            ]

        if kind == "turn_start":
            self._messages[key] = _Message(index=state.index + 1)
            return []
        if kind == "llm_token":
            return text_delta(
                "reasoning", data.get("reasoning"), data.get("reasoning_offset")
            ) + text_delta("content", data.get("content"), data.get("content_offset"))
        if kind in {"post_llm_response", "turn_end"}:
            # Current MASO sends content in post_llm_response, even when no
            # llm_token was emitted. Retain content-bearing turn_end support
            # for older services; both snapshots share suffix deduplication.
            if data.get("dropped") is True:
                return []
            return text_delta("reasoning", data.get("reasoning_content")) + text_delta(
                "content", data.get("content")
            )
        if kind in {"tool_use", "tool_result"}:
            call = data.get("tool_call_id")
            if not isinstance(call, str) or not call:
                return []
            payload: Dict[str, Any] = {"tool_call_id": call, "name": data.get("tool", "unknown")}
            if kind == "tool_use":
                args = data.get("args", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {"input": args}
                payload["args"] = args if isinstance(args, dict) else {"input": args}
                return [event(AgentStreamEventType.TOOL_CALL_STARTED, payload, call_id=call)]
            payload.update(
                {
                    "result": data.get("output", ""),
                    "status": "failed" if data.get("success") is False else "completed",
                }
            )
            return [event(AgentStreamEventType.TOOL_CALL_COMPLETED, payload, call_id=call)]
        if kind in {"context_condensing", "context_condensed"}:
            # The summary is model context, never another assistant message.
            # context_condensed even precedes the actual in-place splice in
            # MASO 0.7.3, so re-fetching history here would race the mutation.
            text = "Condensing context…" if kind == "context_condensing" else "Context condensed"
            if data.get("error"):
                text = "Context condensation failed"
            elif data.get("info"):
                text = str(data["info"])
            return [
                event(
                    AgentStreamEventType.STATUS,
                    {
                        "text": text,
                        "provider_status": kind,
                        "snapshot": True,
                    },
                    message_id=f"{ctx.turn_id}:maso:{agent}:condense",
                )
            ]
        if kind in {"confirmation_required", "confirmation_resolved"}:
            call_id = data.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                return []
            if kind == "confirmation_resolved":
                return [
                    event(
                        AgentStreamEventType.APPROVAL_RESOLVED,
                        {"tool_call_id": call_id},
                        call_id=call_id,
                    )
                ]
            args = data.get("args", "")
            details = args if isinstance(args, str) else json.dumps(args)
            prompt = f"Allow {data.get('tool', 'this tool')}?\n{details}"
            return [
                event(
                    AgentStreamEventType.APPROVAL_REQUIRED,
                    {
                        "tool_call_id": call_id,
                        "kind": "ask_question",
                        "title": "MASO tool confirmation",
                        "questions": [
                            {
                                "id": f"maso-confirm:{call_id}",
                                "prompt": prompt,
                                "options": [
                                    {"id": "Approve", "label": "Approve"},
                                    {"id": "Reject", "label": "Reject"},
                                ],
                                "allow_multiple": False,
                            }
                        ],
                    },
                    call_id=call_id,
                )
            ]
        if kind == "agent_error":
            description, evidence = _describe_error(data.get("error"))
            stop_reason = data.get("stop_reason")
            # MASO pins stop_reason to "error" whenever it fires this hook, so
            # naming it would only repeat the sentence. Anything else means the
            # contract moved and the reader should see it.
            qualifier = (
                f" (stop reason: {stop_reason})"
                if isinstance(stop_reason, str) and stop_reason not in ("", "error")
                else ""
            )
            subject = "MASO sub-agent" if child else "MASO run"
            if description:
                text = f"{subject} failed{qualifier}: {description}"
            else:
                text = f"{subject} failed{qualifier} and reported no error description."
                if not child:
                    # The only honest advice left: Hub knows nothing the user
                    # can act on, and the cause is in MASO's own logs.
                    text += " Retry the turn; if it repeats, check the MASO service logs."
            # A child failure is context inside the child's timeline, never a
            # terminal error for the parent Chat.
            failure: Dict[str, Any] = (
                {"text": text, "provider_status": "error"} if child else {"message": text}
            )
            if evidence is not None:
                failure["detail"] = evidence
            if isinstance(stop_reason, str) and stop_reason:
                failure["stop_reason"] = stop_reason
            return [
                event(
                    AgentStreamEventType.STATUS if child else AgentStreamEventType.ERROR,
                    failure,
                )
            ]
        if kind == "hub_transport_error":
            for item in list(self._messages):
                if item[:2] == key[:2]:
                    self._messages.pop(item, None)
            return [
                event(
                    AgentStreamEventType.ERROR, {"message": data.get("error", "MASO stream failed")}
                ),
                event(AgentStreamEventType.TURN_COMPLETED, {"status": "failed"}),
            ]
        if kind in {"agent_start", "agent_end"} and child:
            reason = data.get("stop_reason")
            lifecycle = (
                "active"
                if kind == "agent_start"
                else (
                    "cancelled"
                    if reason == "aborted"
                    else "failed" if reason == "error" else "completed"
                )
            )
            return [
                event(
                    AgentStreamEventType.STATUS,
                    {
                        "provider_status": "subagent_lifecycle",
                        "subagent_status": lifecycle,
                        "text": f"MASO sub-agent {lifecycle}",
                    },
                )
            ]
        if kind == "agent_end" and not child:
            reason = data.get("stop_reason")
            status = (
                "interrupted"
                if reason == "aborted"
                else "failed" if reason == "error" else "completed"
            )
            for item in list(self._messages):
                if item[:2] == key[:2]:
                    self._messages.pop(item, None)
            return [
                event(
                    AgentStreamEventType.TURN_COMPLETED, {"status": status, "stop_reason": reason}
                )
            ]
        return []
