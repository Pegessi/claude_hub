"""Claude Code stream-json transcript adapter.

Tails ``~/.claude/projects/{slug}/{session-id}.jsonl`` — the append-only
stream-json conversation log Claude Code writes per session.

Source-location reuses the exact helpers from ``ttyd_manager`` (imported, not
duplicated): ``_claude_project_dir_for_cwd``, ``_jsonl_start_epoch``,
``_pick_backfill_session``.

Defensive: ``normalize_line`` skips any line it cannot parse (never raises);
``discover_source`` returns ``None`` when no confident match exists.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...models import AgentStreamEvent, AgentStreamEventType, ManagedSession
from ..ttyd_manager import (
    _claude_project_dir_for_cwd,
    _jsonl_start_epoch,
    _pick_backfill_session,
)
from .base import AgentStreamAdapter, NormalizeContext, resolve_cwd, resolve_process_hint


def _flatten_content(content: Any) -> str:
    """Flatten a Claude ``tool_result`` content value to a plain string."""
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


class ClaudeJsonlAdapter(AgentStreamAdapter):
    """Adapter for Claude Code's ``~/.claude/projects/**/*.jsonl`` logs."""

    adapter_id = "claude-jsonl"
    schema_version = 1
    supports_approval_ui = False
    supports_tool_timeline = True

    # ── source discovery ─────────────────────────────────────────────────────

    def discover_source(self, session: ManagedSession) -> Optional[Path]:
        cwd = resolve_cwd(session)
        _, agent_session_id = resolve_process_hint(session)
        project_dir = _claude_project_dir_for_cwd(cwd)
        if not project_dir.is_dir():
            return None

        if agent_session_id:
            exact = project_dir / f"{agent_session_id}.jsonl"
            if exact.is_file():
                return exact
            return None

        created = self._session_created_epoch(session)
        if created is None:
            return None
        candidates: List[tuple] = []
        for f in project_dir.glob("*.jsonl"):
            start = _jsonl_start_epoch(str(f))
            if start is None:
                continue
            candidates.append((abs(start - created), f.stem, str(f)))
        picked = _pick_backfill_session(created, candidates)
        if picked:
            return project_dir / f"{picked}.jsonl"
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
        if raw.get("isSidechain") or raw.get("isMeta") or raw.get("isCompactSummary"):
            return events

        top_type = raw.get("type")

        # Native stream-json emits ``stream_event`` wrappers when launched with
        # ``--include-partial-messages``. These carry real-time deltas.
        if top_type == "stream_event":
            return self._normalize_stream_event(raw.get("event"), ctx)

        # The top-level ``result`` record arrives after the final ``assistant``
        # snapshot. It is the provider's explicit turn-end marker: clear the
        # per-turn accumulator so the next turn starts from a clean state, and
        # emit ``TURN_COMPLETED`` here (not on ``message_stop``) so the final
        # assistant/tool records always precede the turn-completion event.
        #
        # The ``result`` record carries the turn's outcome: ``is_error``
        # (bool) or ``subtype`` ("success" | "error" | "cancelled"). Map these
        # to the completion status rather than unconditionally marking the
        # turn completed — a provider that errored or was cancelled must
        # surface that so the frontend can render the correct terminal state.
        if top_type == "result":
            self._clear_turn_state(ctx)
            is_error = bool(raw.get("is_error", False))
            subtype = raw.get("subtype")
            if is_error or subtype == "error":
                err = raw.get("result") or raw.get("error") or "turn failed"
                if isinstance(err, str) and err:
                    events.append(ctx.event(AgentStreamEventType.ERROR, {"message": err}))
                status = "failed"
            elif subtype == "cancelled":
                status = "cancelled"
            else:
                status = "completed"
            events.append(ctx.event(AgentStreamEventType.TURN_COMPLETED, {"status": status}))
            return events

        msg = raw.get("message")
        if not isinstance(msg, dict):
            return events

        if top_type == "assistant":
            events.extend(self._normalize_assistant(msg, ctx))
        elif top_type == "user":
            events.extend(self._normalize_user(msg, ctx))
        return events

    def _normalize_stream_event(self, event: Any, ctx: NormalizeContext) -> List[AgentStreamEvent]:
        events: List[AgentStreamEvent] = []
        if not isinstance(event, dict):
            return events
        etype = event.get("type")
        if etype == "message_start":
            events.append(ctx.event(AgentStreamEventType.TURN_STARTED, {"summary": ""}))
        elif etype == "content_block_delta":
            delta = event.get("delta")
            if isinstance(delta, dict):
                dtype = delta.get("type")
                if dtype == "thinking_delta":
                    text = delta.get("thinking")
                    if isinstance(text, str) and text:
                        state = self._get_turn_state(ctx)
                        state.thinking += text
                        events.append(
                            ctx.event(AgentStreamEventType.THINKING_DELTA, {"text": text})
                        )
                elif dtype == "text_delta":
                    text = delta.get("text")
                    if isinstance(text, str) and text:
                        state = self._get_turn_state(ctx)
                        state.text += text
                        events.append(ctx.event(AgentStreamEventType.TEXT_DELTA, {"text": text}))
        elif etype == "message_stop":
            # The assistant has finished generating. Do NOT emit
            # ``TURN_COMPLETED`` here: the provider may still emit a final
            # top-level ``assistant`` snapshot (and a ``result`` record) after
            # ``message_stop``. Emitting completion here would make the final
            # snapshot arrive after the turn is marked complete. The
            # accumulator is cleared and ``TURN_COMPLETED`` is emitted on the
            # top-level ``result`` record instead.
            pass
        return events

    def _normalize_assistant(
        self, msg: Dict[str, Any], ctx: NormalizeContext
    ) -> List[AgentStreamEvent]:
        """Normalize a final assistant SDK message.

        When Claude is launched with ``--include-partial-messages``, the
        streaming deltas (``content_block_delta``) are followed by a final
        ``assistant`` message that contains the complete text and thinking.
        We reconcile the text/thinking against the accumulated deltas so the
        final snapshot is never appended verbatim (which would double the
        visible output). Tool-use blocks are always emitted.
        """
        events: List[AgentStreamEvent] = []
        content = msg.get("content")
        if not isinstance(content, list):
            return events
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    suffix = self._reconcile_text(ctx, text)
                    if suffix is None:
                        events.append(
                            ctx.event(
                                AgentStreamEventType.ERROR,
                                {
                                    "message": (
                                        "assistant final text does not match streamed deltas; "
                                        "cannot safely reconcile"
                                    )
                                },
                            )
                        )
                    elif suffix:
                        events.append(ctx.event(AgentStreamEventType.TEXT_DELTA, {"text": suffix}))
            elif btype == "thinking":
                thinking = block.get("thinking")
                if isinstance(thinking, str) and thinking:
                    suffix = self._reconcile_thinking(ctx, thinking)
                    if suffix is None:
                        events.append(
                            ctx.event(
                                AgentStreamEventType.ERROR,
                                {
                                    "message": (
                                        "assistant final thinking does not match streamed deltas; "
                                        "cannot safely reconcile"
                                    )
                                },
                            )
                        )
                    elif suffix:
                        events.append(
                            ctx.event(AgentStreamEventType.THINKING_DELTA, {"text": suffix})
                        )
            elif btype == "tool_use":
                tool_name = block.get("name") or "unknown"
                tool_id = block.get("id")
                args = block.get("input")
                if not isinstance(args, dict):
                    args = {}
                tool_call_id = tool_id if isinstance(tool_id, str) else None
                events.append(
                    ctx.event(
                        AgentStreamEventType.TOOL_CALL_STARTED,
                        {
                            "tool_call_id": tool_call_id,
                            "name": tool_name,
                            "args": args,
                        },
                        call_id=tool_call_id,
                    )
                )
        return events

    def _normalize_user(self, msg: Dict[str, Any], ctx: NormalizeContext) -> List[AgentStreamEvent]:
        events: List[AgentStreamEvent] = []
        content = msg.get("content")
        if isinstance(content, str):
            if content.strip():
                events.append(ctx.event(AgentStreamEventType.TURN_STARTED, {"summary": content}))
            return events
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_result":
                    continue
                tool_id = block.get("tool_use_id")
                is_error = bool(block.get("is_error"))
                result = _flatten_content(block.get("content"))
                events.append(
                    ctx.event(
                        AgentStreamEventType.TOOL_CALL_COMPLETED,
                        {
                            "tool_call_id": tool_id,
                            "status": "failed" if is_error else "completed",
                            "result": result,
                        },
                        call_id=tool_id if isinstance(tool_id, str) else None,
                    )
                )
        return events
