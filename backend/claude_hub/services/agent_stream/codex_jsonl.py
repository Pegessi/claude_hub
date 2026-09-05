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
from typing import Any, Dict, List, Optional

from ...models import (
    AgentStreamEvent,
    AgentStreamEventType,
    ManagedSession,
    StreamCapabilities,
)
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
from .native import _CODEX_QUESTION_METHODS

_FLAT_OBJ_RE = re.compile(r"\{[^{}]*\}")
_CMD_RE = re.compile(r'"cmd"\s*:\s*"((?:[^"\\]|\\.)*)"')


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

    def _normalize_notification(
        self, method: str, params: Any, ctx: NormalizeContext
    ) -> List[AgentStreamEvent]:
        """Normalize a Codex app-server JSON-RPC notification."""
        events: List[AgentStreamEvent] = []
        if not isinstance(params, dict):
            return events
        if method == "turn/started":
            events.append(ctx.event(AgentStreamEventType.TURN_STARTED, {"summary": ""}))
        elif method == "turn/completed":
            turn = params.get("turn")
            status = "completed"
            if isinstance(turn, dict):
                turn_status = turn.get("status")
                if turn_status in ("failed", "cancelled", "completed"):
                    status = turn_status
            events.append(ctx.event(AgentStreamEventType.TURN_COMPLETED, {"status": status}))
        elif method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str) and delta:
                events.append(ctx.event(AgentStreamEventType.TEXT_DELTA, {"text": delta}))
        elif method == "item/reasoning/textDelta":
            delta = params.get("delta")
            if isinstance(delta, str) and delta:
                events.append(ctx.event(AgentStreamEventType.THINKING_DELTA, {"text": delta}))
        elif method == "item/plan/delta":
            delta = params.get("delta")
            if isinstance(delta, str) and delta:
                events.append(ctx.event(AgentStreamEventType.TEXT_DELTA, {"text": delta}))
        elif method in _CODEX_QUESTION_METHODS:
            events.extend(self._normalize_question(params, ctx))
        return events

    def _normalize_question(
        self, params: Dict[str, Any], ctx: NormalizeContext
    ) -> List[AgentStreamEvent]:
        """Emit a tool call + approval card for a blocking user question.

        The app-server blocks the turn on ``requestUserInput``; the card lets
        the user answer, and the tailer routes the answer back as the JSON-RPC
        response (see ``CodexNativeSession.answer_pending_question``).
        """
        events: List[AgentStreamEvent] = []
        questions = self._codex_normalize_questions(params.get("questions"))
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

    @staticmethod
    def _codex_normalize_questions(raw: Any) -> List[Dict[str, Any]]:
        """Map Codex ``requestUserInput`` questions to the shared card shape.

        Codex sends ``{id, header, question, options: [{label, ...}],
        multiSelect}``; the approval card expects ``{id, prompt,
        options: [{id, label}], allow_multiple}``. Option ids are the labels
        themselves, so a selected label is also the answer value.
        """
        if not isinstance(raw, list):
            return []
        questions: List[Dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            question_id = item.get("id")
            if not isinstance(question_id, str) or not question_id:
                continue
            prompt = item.get("question")
            if not isinstance(prompt, str) or not prompt:
                prompt = item.get("header")
            if not isinstance(prompt, str) or not prompt:
                continue
            raw_options = item.get("options")
            options: List[Dict[str, str]] = []
            if isinstance(raw_options, list):
                for opt in raw_options:
                    if not isinstance(opt, dict):
                        continue
                    label = opt.get("label")
                    if not isinstance(label, str) or not label:
                        continue
                    options.append({"id": label, "label": label})
            if not options:
                continue
            questions.append(
                {
                    "id": question_id,
                    "prompt": prompt,
                    "options": options,
                    "allow_multiple": item.get("multiSelect") is True,
                }
            )
        return questions

    def _normalize_event_msg(
        self, payload: Dict[str, Any], payload_type: str, ctx: NormalizeContext
    ) -> List[AgentStreamEvent]:
        events: List[AgentStreamEvent] = []
        if payload_type == "user_message":
            text = payload.get("message")
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
