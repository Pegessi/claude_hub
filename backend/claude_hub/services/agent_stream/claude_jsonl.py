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

import json
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


#: Tool name Claude Code uses to ask the user a structured question.
_ASK_USER_QUESTION_TOOL = "AskUserQuestion"


def _normalize_ask_user_question_args(
    args: Dict[str, Any],
) -> Optional[List[Dict[str, Any]]]:
    """Normalize Claude ``AskUserQuestion`` input to the approval-card shape.

    Claude's tool input is::

        {"questions": [{"question", "header", "multiSelect",
                        "options": [{"label", "description"}]}]}

    The structured timeline renders ``approval_required`` questions as
    ``{id, prompt, options: [{id, label}], allow_multiple}``. Option ids are
    set to the option label so the answer the frontend submits
    (``ask_question_response``) is self-describing: Claude's own tool call
    carries no option ids, and the next one-shot turn maps the labels back to
    its ``AskUserQuestion`` options.
    """
    raw_questions = args.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        return None
    normalized: List[Dict[str, Any]] = []
    for index, raw_question in enumerate(raw_questions):
        if not isinstance(raw_question, dict):
            continue
        prompt = raw_question.get("question")
        if not isinstance(prompt, str) or not prompt.strip():
            header = raw_question.get("header")
            prompt = header if isinstance(header, str) else ""
        if not prompt.strip():
            continue
        raw_options = raw_question.get("options")
        if not isinstance(raw_options, list):
            continue
        options: List[Dict[str, str]] = []
        for raw_option in raw_options:
            if not isinstance(raw_option, dict):
                continue
            label = raw_option.get("label")
            if not isinstance(label, str) or not label.strip():
                continue
            options.append({"id": label, "label": label})
        if not options:
            continue
        normalized.append(
            {
                "id": str(index),
                "prompt": prompt,
                "allow_multiple": bool(raw_question.get("multiSelect")),
                "options": options,
            }
        )
    return normalized or None


class ClaudeJsonlAdapter(AgentStreamAdapter):
    """Adapter for Claude Code's ``~/.claude/projects/**/*.jsonl`` logs."""

    adapter_id = "claude-jsonl"
    schema_version = 1
    supports_approval_ui = True
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
            # Each assistant message starts its own text/thinking accumulation
            # scope. A single turn can contain multiple assistant messages
            # (pre-tool thinking+tool_use, then post-tool thinking+text), and
            # the final snapshot of the second message must reconcile against
            # its own deltas rather than the first message's accumulated text.
            # Keying the scope by the provider message id covers both the live
            # native path (here) and the final-only/backfill path
            # (``_normalize_assistant``).
            msg = event.get("message")
            raw_message_id = msg.get("id") if isinstance(msg, dict) else None
            message_id = raw_message_id if isinstance(raw_message_id, str) else None
            self._begin_provider_message(ctx, message_id)
            events.append(ctx.event(AgentStreamEventType.TURN_STARTED, {"summary": ""}))
        elif etype == "content_block_start":
            block = event.get("content_block")
            if isinstance(block, dict) and block.get("type") == "tool_use":
                self._handle_tool_use_start(event, block, ctx)
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
                elif dtype == "input_json_delta":
                    self._handle_tool_input_delta(event, delta, ctx)
        elif etype == "content_block_stop":
            self._handle_tool_use_stop(event, ctx, events)
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

    def _handle_tool_use_start(
        self, event: Dict[str, Any], block: Dict[str, Any], ctx: NormalizeContext
    ) -> None:
        """Record a streaming tool_use block's identity and initial input."""

        index = event.get("index")
        if not isinstance(index, int):
            return
        tool_id = block.get("id")
        raw_tool_name = block.get("name")
        tool_name = raw_tool_name if isinstance(raw_tool_name, str) and raw_tool_name else "unknown"
        initial_input = block.get("input")
        if not isinstance(initial_input, dict):
            initial_input = {}
        state = self._get_turn_state(ctx)
        state.pending_tool_meta[index] = {
            "id": tool_id if isinstance(tool_id, str) else None,
            "name": tool_name,
            "input": initial_input,
        }
        state.pending_tool_inputs[index] = ""

    def _handle_tool_input_delta(
        self, event: Dict[str, Any], delta: Dict[str, Any], ctx: NormalizeContext
    ) -> None:
        """Accumulate a streaming tool argument JSON fragment."""

        index = event.get("index")
        if not isinstance(index, int):
            return
        partial = delta.get("partial_json")
        if not isinstance(partial, str):
            return
        state = self._get_turn_state(ctx)
        if index in state.pending_tool_inputs:
            state.pending_tool_inputs[index] += partial

    def _handle_tool_use_stop(
        self, event: Dict[str, Any], ctx: NormalizeContext, events: List[AgentStreamEvent]
    ) -> None:
        """Emit ``TOOL_CALL_STARTED`` once a tool_use block's input is complete."""

        index = event.get("index")
        if not isinstance(index, int):
            return
        state = self._get_turn_state(ctx)
        meta = state.pending_tool_meta.pop(index, None)
        raw_input = state.pending_tool_inputs.pop(index, "")
        if not meta:
            return
        tool_id = meta["id"]
        tool_name = meta["name"]
        args: Dict[str, Any] = dict(meta["input"])
        if raw_input:
            try:
                parsed = json.loads(raw_input)
                if not isinstance(parsed, dict):
                    # A tool input must be an object. Do not publish an
                    # untrustworthy streamed card or suppress the later final
                    # assistant snapshot, which may carry authoritative args.
                    return
                args.update(parsed)
            except json.JSONDecodeError:
                # A truncated native stream can leave partial JSON behind.
                # Wait for the final assistant snapshot instead of displaying
                # a raw fragment and then suppressing the complete snapshot.
                return
        if not tool_id:
            # Without a stable identity the final snapshot cannot be
            # deduplicated safely. Let it be the authoritative source.
            return
        if tool_id in state.emitted_tool_call_ids:
            # Claude 2.1.x can publish the authoritative top-level assistant
            # tool_use row before content_block_stop. In that ordering the
            # assistant row already announced the call; block stop only
            # closes the streamed input and must not announce it again.
            return
        state.emitted_tool_call_ids.add(tool_id)
        events.append(
            ctx.event(
                AgentStreamEventType.TOOL_CALL_STARTED,
                {
                    "tool_call_id": tool_id,
                    "name": tool_name,
                    "args": args,
                },
                call_id=tool_id,
            )
        )
        if tool_name == _ASK_USER_QUESTION_TOOL:
            self._emit_ask_user_question_approval(events, tool_id, args, ctx)

    def _emit_ask_user_question_approval(
        self,
        events: List[AgentStreamEvent],
        tool_call_id: Optional[str],
        args: Dict[str, Any],
        ctx: NormalizeContext,
    ) -> None:
        """Append an ``approval_required`` event for an ``AskUserQuestion`` call.

        No-op when the input carries no renderable question (the ordinary tool
        row stays the only surface). Mirrors the Cursor ``AskQuestion`` path so
        the frontend renders the same interactive card and submits the answer
        as a follow-up message.
        """
        questions = _normalize_ask_user_question_args(args)
        if questions is None:
            return
        title: Optional[str] = None
        raw_questions = args.get("questions")
        if isinstance(raw_questions, list):
            for raw_question in raw_questions:
                if isinstance(raw_question, dict):
                    header = raw_question.get("header")
                    if isinstance(header, str) and header.strip():
                        title = header
                        break
        events.append(
            ctx.event(
                AgentStreamEventType.APPROVAL_REQUIRED,
                {
                    "tool_call_id": tool_call_id,
                    "kind": "ask_question",
                    "title": title,
                    "questions": questions,
                },
                call_id=tool_call_id,
            )
        )

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
        # Final-only transcripts and backfill emit top-level ``assistant`` rows
        # directly (no ``stream_event`` wrappers). Scope text/thinking
        # accumulation to the provider message id so two assistant messages in
        # one turn (e.g. thinking+tool_use then thinking+text) each reconcile
        # against their own content. Consecutive rows sharing the same id keep
        # accumulating.
        raw_message_id = msg.get("id")
        message_id = raw_message_id if isinstance(raw_message_id, str) else None
        self._begin_provider_message(ctx, message_id)
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
                # When the provider streamed this tool_use block, we already
                # emitted TOOL_CALL_STARTED from content_block_stop with the
                # assembled arguments. Skip the duplicate here so the tool
                # card appears once, in its correct interleaved position.
                if tool_call_id and tool_call_id in self._get_turn_state(ctx).emitted_tool_call_ids:
                    continue
                if tool_call_id:
                    # Claude 2.1.x normally emits this authoritative
                    # assistant row before content_block_stop. Record the id
                    # here so the later block stop is deduplicated. If block
                    # stop arrived first, the guard above handled the inverse
                    # ordering.
                    self._get_turn_state(ctx).emitted_tool_call_ids.add(tool_call_id)
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
                if tool_name == _ASK_USER_QUESTION_TOOL:
                    self._emit_ask_user_question_approval(events, tool_call_id, args, ctx)
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
