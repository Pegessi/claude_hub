"""Cursor CLI same-pane transcript adapter (Layer B, snapshot source).

Cursor's interactive agent writes a JSONL transcript under
``.cursor/projects/<project-component>/agent-transcripts/<session-id>/<session-id>.jsonl``.
The file is rewritten during checkpoint compaction, so this adapter opts into
snapshot reconciliation: each read returns a fully validated
:class:`TranscriptSnapshot` whose ``digest`` lets the tailer detect rewrites and
append only newly observed rows when the prior snapshot is a strict prefix.
History rewrites fail closed to the raw terminal because a connected structured
client must never observe a non-monotonic timeline.

Provenance is pinned on the :class:`ManagedSession` and validated exactly:

- ``cursor_transport`` must be ``terminal_transcript``
- ``cursor_cli_version`` must be in :data:`SUPPORTED_CURSOR_CLI_VERSIONS`
- ``cursor_transcript_schema`` must equal :data:`CURSOR_CLI_TRANSCRIPT_SCHEMA`
- ``agent_session_id`` must be present
- ``cursor_transcript_path`` must be absolute, match the cwd/session binding,
  and point at a regular file

Any mismatch fails closed (``discover_source`` returns ``None`` → raw terminal).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...models import (
    AgentStreamEvent,
    AgentStreamEventType,
    AgentType,
    ManagedSession,
    StreamCapabilities,
)
from ...models.agent_stream import normalize_provider_usage
from ..ttyd_manager import (
    CURSOR_TRANSCRIPT_SCHEMA,
    SUPPORTED_CURSOR_TRANSCRIPT_VERSIONS,
)
from .base import (
    AgentStreamAdapter,
    NormalizeContext,
    SnapshotRecord,
    TranscriptSnapshot,
    discover_source_cached,
    resolve_cwd,
)
from .fork_seed import strip_fork_seed_history
from .native import (
    strip_hub_runtime_guidance,
    strip_image_attachment_guidance,
    strip_question_protocol_guidance,
)

#: Schema identifier for the same-pane Cursor transcript format this adapter
#: understands. Bump when the row shape changes incompatibly.
CURSOR_CLI_TRANSCRIPT_SCHEMA = CURSOR_TRANSCRIPT_SCHEMA

#: Cursor CLI versions whose same-pane transcript row shape is known to match
#: :data:`CURSOR_CLI_TRANSCRIPT_SCHEMA`. Pinned so an unknown CLI version fails
#: closed rather than mis-normalizing rows.
SUPPORTED_CURSOR_CLI_VERSIONS = tuple(SUPPORTED_CURSOR_TRANSCRIPT_VERSIONS)

_PROJECT_COMPONENT_RE = re.compile(r"[^a-zA-Z0-9]+")


class CursorTranscriptInvalid(ValueError):
    """Raised when a transcript row cannot be validated or parsed."""


def _project_component(cwd: str) -> str:
    """Derive the ``.cursor/projects/<component>`` directory name from ``cwd``.

    Mirrors Cursor's own project-key derivation: non-alphanumeric runs become a
    single hyphen, leading/trailing hyphens are stripped.
    """
    return _PROJECT_COMPONENT_RE.sub("-", cwd).strip("-")


def _expected_transcript_path(data_dir: str, cwd: str, session_id: str) -> Path:
    """Return the canonical transcript path for ``data_dir`` + ``cwd`` + ``session_id``.

    Cursor stores transcripts under
    ``<data_dir>/projects/<sanitized-cwd>/agent-transcripts/<session-id>/<session-id>.jsonl``.
    """
    # Keep this derivation byte-for-byte aligned with
    # ``cursor_terminal_transcript_path`` in ttyd_manager. The launcher
    # canonicalizes cwd before Cursor derives its project key, so observing an
    # equivalent relative or symlinked workspace path must do the same.
    canonical_cwd = str(Path(cwd).resolve(strict=False))
    comp = _project_component(canonical_cwd)
    return (
        Path(data_dir).expanduser().resolve(strict=False)
        / "projects"
        / comp
        / "agent-transcripts"
        / session_id
        / f"{session_id}.jsonl"
    )


def _canonical_json(row: Dict[str, Any]) -> bytes:
    """Stable, key-sorted JSON encoding for row-digest computation."""
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


class CursorCliTranscriptAdapter(AgentStreamAdapter):
    """Adapter for Cursor CLI's same-pane ``agent-transcripts`` JSONL."""

    adapter_id = "cursor-cli-transcript"
    schema_version = 1
    spec_agent_type = AgentType.CURSOR

    # ── provenance / binding validation ──────────────────────────────────────

    def _validated_binding(self, session: ManagedSession) -> Optional[Path]:
        """Return the validated transcript path or ``None`` (fail-closed).

        Every provenance field must match exactly; any mismatch returns
        ``None`` so the session falls back to the raw terminal pane.
        """
        if session.agent_type != AgentType.CURSOR:
            return None
        if getattr(session, "cursor_transport", None) != "terminal_transcript":
            return None
        data_dir = session.cursor_data_dir
        if not data_dir:
            return None
        if session.cursor_cli_version not in SUPPORTED_CURSOR_CLI_VERSIONS:
            return None
        if session.cursor_transcript_schema != CURSOR_CLI_TRANSCRIPT_SCHEMA:
            return None
        sid = session.agent_session_id
        if not sid:
            return None
        raw_path = session.cursor_transcript_path
        if not raw_path:
            return None
        path = Path(raw_path)
        if not path.is_absolute():
            return None
        cwd = resolve_cwd(session)
        if not cwd:
            return None
        expected = _expected_transcript_path(data_dir, cwd, sid)
        try:
            if path.resolve() != expected.resolve():
                return None
        except OSError:
            return None
        if not path.is_file():
            return None
        return path

    # ── source discovery ─────────────────────────────────────────────────────

    def discover_source(self, session: ManagedSession) -> Optional[Path]:
        return self._validated_binding(session)

    def capabilities(self, session: ManagedSession) -> StreamCapabilities:
        source = discover_source_cached(self, session)
        return StreamCapabilities(
            structured=source is not None,
            adapter_id=self.adapter_id,
            schema_version=self.schema_version,
            sources=[str(source)] if source else [],
            supports_approval_ui=self.supports_approval_ui,
            supports_tool_timeline=self.supports_tool_timeline,
        )

    # ── snapshot support ─────────────────────────────────────────────────────

    def supports_snapshot(self, session: ManagedSession) -> bool:
        return getattr(session, "cursor_transport", None) == "terminal_transcript"

    def read_snapshot(self, path: Path, session: ManagedSession) -> TranscriptSnapshot:
        """Read and fully validate the transcript at ``path``.

        Raises :class:`CursorTranscriptInvalid` if any row is partial or
        unrecognized, so the tailer can fail closed. Returns a snapshot whose
        ``digest`` is the sha256 of the raw bytes and whose records carry
        stable ``source_id`` values (session id + canonical row digest +
        occurrence ordinal) for rewrite-safe dedup.
        """
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        text = data.decode("utf-8")
        lines = text.split("\n")
        # A well-formed JSONL file ends with a newline, so the final split
        # element is empty. A non-empty trailing element means the last line
        # was written without a newline (partial write) → reject.
        if lines and lines[-1] != "":
            raise CursorTranscriptInvalid("transcript ends with a partial (non-newline) line")
        non_empty = [ln for ln in lines if ln != ""]

        seen_digests: Dict[str, int] = {}
        records: List[SnapshotRecord] = []
        for ln in non_empty:
            try:
                row = json.loads(ln)
            except json.JSONDecodeError as exc:
                raise CursorTranscriptInvalid(f"invalid JSON line: {exc}") from exc
            if not isinstance(row, dict):
                raise CursorTranscriptInvalid("transcript row is not a JSON object")
            if not self._is_known_row(row):
                raise CursorTranscriptInvalid(f"unknown transcript row type: {row}")
            row_digest = hashlib.sha256(_canonical_json(row)).hexdigest()
            occurrence = seen_digests.get(row_digest, 0)
            seen_digests[row_digest] = occurrence + 1
            source_id = f"cursor-cli:{session.id}:{row_digest}:{occurrence}"
            source_kind = str(row.get("role") or row.get("type") or "")
            records.append(SnapshotRecord(source_id=source_id, raw=row, source_kind=source_kind))

        return TranscriptSnapshot(digest=digest, records=tuple(records))

    @staticmethod
    def _is_known_row(row: Dict[str, Any]) -> bool:
        """Return ``True`` for the row shapes this adapter can normalize."""
        role = row.get("role")
        if role == "user":
            return isinstance(row.get("message"), dict)
        if role == "assistant":
            return isinstance(row.get("message"), dict)
        if row.get("type") == "turn_ended":
            return True
        return False

    # ── normalization ────────────────────────────────────────────────────────

    def normalize_line(self, raw: Dict[str, Any], ctx: NormalizeContext) -> List[AgentStreamEvent]:
        events: List[AgentStreamEvent] = []
        if not isinstance(raw, dict):
            return events

        # Cursor ACP persistent JSON-RPC notifications carry ``method ==
        # "session/update"``; inline transport errors use ``type == "acp_error"``.
        if raw.get("method") == "session/update":
            return self._normalize_acp_update(raw.get("params"), ctx)
        if raw.get("type") == "acp_error":
            msg = raw.get("message")
            if isinstance(msg, str) and msg:
                self._get_turn_state(ctx).saw_turn_error = True
                events.append(ctx.event(AgentStreamEventType.ERROR, {"message": msg}))
            return events

        # Native stream-json stdout records carry a top-level ``type`` field
        # (``system``, ``thinking``, ``assistant``, ``result``). Transcript
        # file rows use ``role`` (``user``/``assistant``) or ``type:
        # turn_ended``. Dispatch on the native shape first.
        top_type = raw.get("type")
        if top_type in ("system", "thinking", "assistant", "tool_call", "result"):
            return self._normalize_stream_json(raw, ctx)

        role = raw.get("role")
        if role == "user":
            text = self._extract_text(raw.get("message"))
            # Strip the sentinel-wrapped question-protocol guidance that the
            # transport prepends to Cursor prompts (the CLI has no
            # system-prompt flag) so it never reaches the persisted timeline
            # or the UI. No-op for ordinary user messages.
            text = strip_question_protocol_guidance(text)
            # Strip the sentinel-wrapped image-reference block the transport
            # injects for attached images (Cursor has no structured image
            # flag; the model reads the staged files via its Read tool) so
            # the temp paths never reach the persisted timeline or the UI.
            text = strip_image_attachment_guidance(text)
            # Strip the sentinel-wrapped Hub Chat runtime/self-scheduling
            # guidance the transport injects on the first turn so it never
            # reaches the persisted timeline or the UI. No-op on later turns.
            text = strip_hub_runtime_guidance(text)
            # Strip the one-shot fork-seed history block the transport
            # prepends to a forked tab's first turn.
            text = strip_fork_seed_history(text)
            if text:
                events.append(ctx.event(AgentStreamEventType.TURN_STARTED, {"summary": text}))
        elif role == "assistant":
            message = raw.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            block_text = block.get("text")
                            if isinstance(block_text, str) and block_text:
                                # Transcript assistant rows are final messages.
                                # Reconcile against any accumulated streaming
                                # text (which would be empty for pure
                                # transcript reads).
                                suffix = self._reconcile_text(ctx, block_text)
                                if suffix is None:
                                    events.append(
                                        ctx.event(
                                            AgentStreamEventType.ERROR,
                                            {
                                                "message": (
                                                    "assistant final text does not match "
                                                    "streamed deltas; cannot safely reconcile"
                                                )
                                            },
                                        )
                                    )
                                elif suffix:
                                    events.append(
                                        ctx.event(
                                            AgentStreamEventType.TEXT_DELTA,
                                            {"text": suffix},
                                        )
                                    )
                        elif btype == "tool_use":
                            events.extend(self._tool_use_events(block, ctx))
        elif raw.get("type") == "turn_ended":
            status = raw.get("status")
            completed_payload: Dict[str, Any] = {"status": "completed"}
            usage = normalize_provider_usage(raw, "cursor")
            if usage is not None:
                completed_payload["usage"] = usage
            if status == "success":
                events.append(ctx.event(AgentStreamEventType.TURN_COMPLETED, completed_payload))
            elif status == "error":
                message = raw.get("error")
                if isinstance(message, str) and message:
                    events.append(ctx.event(AgentStreamEventType.ERROR, {"message": message}))
                completed_payload["status"] = "failed"
                events.append(ctx.event(AgentStreamEventType.TURN_COMPLETED, completed_payload))
            elif status == "aborted":
                completed_payload["status"] = "cancelled"
                events.append(ctx.event(AgentStreamEventType.TURN_COMPLETED, completed_payload))
            self._clear_turn_state(ctx)
        return events

    def _tool_use_events(
        self, block: Dict[str, Any], ctx: NormalizeContext
    ) -> List[AgentStreamEvent]:
        events: List[AgentStreamEvent] = []
        name = block.get("name") or "unknown"
        args = block.get("input")
        if not isinstance(args, dict):
            args = {"input": args} if args is not None else {}
        call_id = block.get("id")
        call_id_str = call_id if isinstance(call_id, str) else None
        state = self._get_turn_state(ctx)
        if not call_id_str or call_id_str not in state.emitted_tool_call_ids:
            if call_id_str:
                state.emitted_tool_call_ids.add(call_id_str)
            events.append(
                ctx.event(
                    AgentStreamEventType.TOOL_CALL_STARTED,
                    {"name": name, "args": args},
                    call_id=call_id_str,
                )
            )
        if name == "AskQuestion":
            questions = self._normalize_questions(args.get("questions"))
            if questions and (
                not call_id_str or call_id_str not in state.emitted_approval_call_ids
            ):
                if call_id_str:
                    state.emitted_approval_call_ids.add(call_id_str)
                events.append(
                    ctx.event(
                        AgentStreamEventType.APPROVAL_REQUIRED,
                        {
                            "tool_call_id": call_id_str,
                            "kind": "ask_question",
                            "title": args.get("title"),
                            "questions": questions,
                        },
                        call_id=call_id_str,
                    )
                )
        return events

    @staticmethod
    def _normalize_questions(raw: Any) -> List[Dict[str, Any]]:
        """Convert Cursor protobuf camelCase and legacy tool input to card fields."""
        if not isinstance(raw, list):
            return []
        questions = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            question_id, prompt = item.get("id"), item.get("prompt")
            if not isinstance(question_id, str) or not question_id:
                continue
            if not isinstance(prompt, str) or not prompt:
                continue
            raw_options = item.get("options")
            options = []
            if isinstance(raw_options, list):
                for option in raw_options:
                    if not isinstance(option, dict):
                        continue
                    option_id, label = option.get("id"), option.get("label")
                    if not isinstance(option_id, str) or not option_id:
                        continue
                    if not isinstance(label, str) or not label:
                        continue
                    normalized = {"id": option_id, "label": label}
                    description = option.get("description")
                    if isinstance(description, str) and description:
                        normalized["description"] = description
                    options.append(normalized)
            if raw_options is not None and (
                not isinstance(raw_options, list) or (raw_options and not options)
            ):
                continue
            questions.append(
                {
                    "id": question_id,
                    "prompt": prompt,
                    "options": options,
                    "allow_multiple": item.get("allowMultiple") is True
                    or item.get("allow_multiple") is True,
                }
            )
        return questions

    def _normalize_stream_tool_call(
        self, raw: Dict[str, Any], ctx: NormalizeContext
    ) -> List[AgentStreamEvent]:
        """Handle the top-level tool records emitted by current Cursor CLI.

        Its protobuf JSON uses ``tool_call.askQuestionToolCall.args``; these
        calls never appear in assistant ``content[].tool_use``. Completion
        records may contain arguments missing from the initial announcement.
        Headless rejection is a placeholder, so it must not resolve the card.
        """
        subtype, call_id = raw.get("subtype"), raw.get("call_id")
        tool_call = raw.get("tool_call")
        if subtype not in {"started", "completed"} or not isinstance(call_id, str):
            return []
        if not isinstance(tool_call, dict):
            return []
        for kind, tool in tool_call.items():
            if not kind.endswith("ToolCall") or not isinstance(tool, dict):
                continue
            name = kind[0].upper() + kind[1 : -len("ToolCall")]
            events = self._tool_use_events(
                {"id": call_id, "name": name, "input": tool.get("args")}, ctx
            )
            if subtype == "completed":
                state = self._get_turn_state(ctx)
                if call_id in state.completed_tool_call_ids:
                    return events
                state.completed_tool_call_ids.add(call_id)
                result = tool.get("result")
                failed = isinstance(result, dict) and any(
                    key in result for key in ("error", "rejected", "failure")
                )
                events.append(
                    ctx.event(
                        AgentStreamEventType.TOOL_CALL_COMPLETED,
                        {
                            "name": name,
                            "status": "failed" if failed else "completed",
                            "result": result,
                        },
                        call_id=call_id,
                    )
                )
            return events
        return []

    def _normalize_assistant_content_blocks(
        self, content: List[Any], ctx: NormalizeContext, *, is_streaming_chunk: bool
    ) -> List[AgentStreamEvent]:
        events: List[AgentStreamEvent] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    if is_streaming_chunk:
                        if not self._is_recent_text_snapshot(ctx, text):
                            self._append_text_delta(ctx, text)
                            events.append(
                                ctx.event(AgentStreamEventType.TEXT_DELTA, {"text": text})
                            )
                    else:
                        suffix = (
                            ""
                            if self._is_recent_text_snapshot(ctx, text)
                            else self._reconcile_text(ctx, text)
                        )
                        if suffix is None:
                            events.append(
                                ctx.event(
                                    AgentStreamEventType.ERROR,
                                    {
                                        "message": (
                                            "assistant final text does not match "
                                            "streamed deltas; cannot safely reconcile"
                                        )
                                    },
                                )
                            )
                        elif suffix:
                            events.append(
                                ctx.event(AgentStreamEventType.TEXT_DELTA, {"text": suffix})
                            )
            elif btype == "tool_use":
                events.extend(self._tool_use_events(block, ctx))
        return events

    def _normalize_stream_json(
        self, raw: Dict[str, Any], ctx: NormalizeContext
    ) -> List[AgentStreamEvent]:
        """Normalize a Cursor ``--print --output-format stream-json`` stdout record.

        Record shapes (verified):

        - ``{type:"system", subtype:"init", session_id, ...}`` — session init;
          the conversation id is captured by the transport, no event emitted.
        - ``{type:"thinking", subtype:"delta", text, session_id, timestamp_ms}``
          — incremental reasoning text.
        - ``{type:"assistant", message:{role:"assistant", content:[{type:"text",
          text:"Hi"}]}, session_id, timestamp_ms, ...}`` — assistant text chunk.
          Cursor emits timestamped streaming assistant records followed by a
          final duplicate assistant record (no ``timestamp_ms``) that carries
          the complete text. We accumulate the streaming chunks and reconcile
          the final snapshot so the visible text is never doubled.
        - ``{type:"result", subtype:"success", is_error:false, result:"Hi",
          session_id, request_id, ...}`` — turn completion.
        """
        events: List[AgentStreamEvent] = []
        top_type = raw.get("type")
        if top_type == "system":
            # Handled by the transport's maybe_capture_conversation_id.
            return events
        if top_type == "tool_call":
            return self._normalize_stream_tool_call(raw, ctx)
        if top_type == "thinking":
            if raw.get("subtype") == "delta":
                text = raw.get("text")
                if isinstance(text, str) and text:
                    state = self._get_turn_state(ctx)
                    state.thinking += text
                    events.append(ctx.event(AgentStreamEventType.THINKING_DELTA, {"text": text}))
            return events
        if top_type == "assistant":
            message = raw.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list):
                    is_streaming_chunk = "timestamp_ms" in raw
                    events.extend(
                        self._normalize_assistant_content_blocks(
                            content, ctx, is_streaming_chunk=is_streaming_chunk
                        )
                    )
            return events
        if top_type == "result":
            is_error = raw.get("is_error", False)
            completed_payload: Dict[str, Any] = {"status": "failed" if is_error else "completed"}
            usage = normalize_provider_usage(raw, "cursor")
            if usage is not None:
                completed_payload["usage"] = usage
            if is_error:
                state = self._get_turn_state(ctx)
                raw_err = raw.get("error")
                err = (
                    ""
                    if state.saw_turn_error and not raw_err and not raw.get("result")
                    else (raw.get("result") or raw_err or "turn failed")
                )
                if isinstance(err, str) and err:
                    events.append(ctx.event(AgentStreamEventType.ERROR, {"message": err}))
                events.append(ctx.event(AgentStreamEventType.TURN_COMPLETED, completed_payload))
            else:
                events.append(ctx.event(AgentStreamEventType.TURN_COMPLETED, completed_payload))
            self._clear_turn_state(ctx)
            return events
        return events

    def _normalize_acp_update(self, params: Any, ctx: NormalizeContext) -> List[AgentStreamEvent]:
        """Normalize a Cursor ``agent acp`` ``session/update`` notification."""
        events: List[AgentStreamEvent] = []
        if not isinstance(params, dict):
            return events
        update = params.get("update")
        if not isinstance(update, dict):
            return events
        session_id = params.get("sessionId")
        sub_thread = (
            session_id
            if isinstance(session_id, str)
            and session_id
            and ctx.main_thread_id
            and session_id != ctx.main_thread_id
            else None
        )
        if sub_thread is not None and not ctx.turn_id:
            return events
        record_ctx = replace(ctx, sub_thread_id=sub_thread) if sub_thread else ctx
        kind = update.get("sessionUpdate")

        if kind == "agent_message_chunk":
            content = update.get("content")
            text = content.get("text") if isinstance(content, dict) else None
            if isinstance(text, str) and text:
                self._append_text_delta(record_ctx, text)
                events.append(
                    ctx.event(
                        AgentStreamEventType.TEXT_DELTA,
                        {"text": text},
                        sub_thread_id=sub_thread,
                    )
                )
            return events

        if kind == "agent_thought_chunk":
            content = update.get("content")
            text = content.get("text") if isinstance(content, dict) else None
            if isinstance(text, str) and text:
                state = self._get_turn_state(record_ctx)
                state.thinking += text
                events.append(
                    ctx.event(
                        AgentStreamEventType.THINKING_DELTA,
                        {"text": text},
                        sub_thread_id=sub_thread,
                    )
                )
            return events

        if kind == "plan":
            entries = update.get("entries")
            if isinstance(entries, list):
                lines: List[str] = []
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    label = entry.get("content")
                    if not isinstance(label, str) or not label.strip():
                        continue
                    step_status = entry.get("status")
                    marker = "x" if step_status == "completed" else " "
                    suffix = " (in progress)" if step_status == "in_progress" else ""
                    lines.append(f"- [{marker}] {label.strip()}{suffix}")
                if lines:
                    events.append(
                        ctx.event(
                            AgentStreamEventType.TEXT_DELTA,
                            {
                                "text": "\n".join(lines),
                                "plan": True,
                                "plan_kind": "progress",
                                "snapshot": True,
                            },
                            message_id=f"plan-progress:{ctx.turn_id or session_id or ''}",
                            sub_thread_id=sub_thread,
                        )
                    )
            return events

        if kind in ("subagent_spawned", "subagent_state_update"):
            child_id = update.get("subagentSessionId")
            if not isinstance(child_id, str) or not child_id or not ctx.turn_id:
                return events
            if kind == "subagent_spawned":
                lifecycle = "active"
                label_name = update.get("name") or "sub-agent"
                status_text = f"Sub-agent {label_name} started"
            else:
                raw_state = update.get("state")
                if raw_state == "disconnected":
                    lifecycle = "cancelled"
                elif raw_state in ("active", "completed", "failed", "cancelled"):
                    lifecycle = raw_state
                else:
                    return events
                status_text = f"Sub-agent {lifecycle}"
            events.append(
                ctx.event(
                    AgentStreamEventType.STATUS,
                    {
                        "text": status_text,
                        "provider_status": "subagent_lifecycle",
                        "subagent_status": lifecycle,
                        "snapshot": True,
                    },
                    message_id=f"subagent:{child_id}",
                    sub_thread_id=child_id,
                )
            )
            return events

        if kind in ("tool_call", "tool_call_update"):
            return self._normalize_acp_tool_update(update, ctx, record_ctx, sub_thread)

        return events

    def _normalize_acp_tool_update(
        self,
        update: Dict[str, Any],
        ctx: NormalizeContext,
        record_ctx: NormalizeContext,
        sub_thread: Optional[str],
    ) -> List[AgentStreamEvent]:
        events: List[AgentStreamEvent] = []
        call_id = update.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            return events
        state = self._get_turn_state(record_ctx)
        meta = state.acp_tool_meta.setdefault(call_id, {"args": {}})
        if isinstance(update.get("kind"), str):
            meta["kind"] = update["kind"]
        if isinstance(update.get("title"), str):
            meta["title"] = update["title"]
        raw_input = update.get("rawInput")
        if isinstance(raw_input, dict):
            merged_args = dict(meta.get("args") or {})
            merged_args.update(raw_input)
            meta["args"] = merged_args
        if isinstance(update.get("locations"), list):
            meta["locations"] = update["locations"]
        if isinstance(update.get("content"), list):
            meta["content"] = update["content"]
        if "rawOutput" in update and update["rawOutput"] is not None:
            meta["raw_output"] = update["rawOutput"]

        tool_name, args = self._derive_acp_tool_identity(meta)
        status = update.get("status")

        # AskQuestion and permission cards are already emitted with full
        # options by the blocking server request handlers.
        if tool_name == "AskQuestion" or call_id in state.emitted_approval_call_ids:
            return events

        terminal_statuses = ("completed", "failed", "cancelled", "canceled", "rejected", "aborted")
        should_emit_start = status in ("in_progress", *terminal_statuses)
        if should_emit_start and call_id not in state.emitted_tool_call_ids:
            state.emitted_tool_call_ids.add(call_id)
            events.append(
                ctx.event(
                    AgentStreamEventType.TOOL_CALL_STARTED,
                    {"name": tool_name, "args": args},
                    call_id=call_id,
                    sub_thread_id=sub_thread,
                )
            )

        if status in terminal_statuses and call_id not in state.completed_tool_call_ids:
            state.completed_tool_call_ids.add(call_id)
            raw_out = meta.get("raw_output")
            cancelled = status in ("cancelled", "canceled", "aborted")
            failed = status in ("failed", "rejected") or (
                isinstance(raw_out, dict)
                and (
                    any(k in raw_out for k in ("error", "rejected", "permissionDenied"))
                    or raw_out.get("exitCode") not in (None, 0)
                )
            )
            result_val = self._format_acp_tool_result(
                raw_out, meta.get("content"), tool_name=tool_name
            )
            completed_status = "cancelled" if cancelled else ("failed" if failed else "completed")
            completed_payload: Dict[str, Any] = {
                "name": tool_name,
                "status": completed_status,
                "result": result_val,
            }
            if args:
                completed_payload["args"] = args
            events.append(
                ctx.event(
                    AgentStreamEventType.TOOL_CALL_COMPLETED,
                    completed_payload,
                    call_id=call_id,
                    sub_thread_id=sub_thread,
                )
            )
        return events

    @staticmethod
    def _derive_acp_tool_identity(meta: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        raw_args = dict(meta.get("args") or {})
        explicit = raw_args.pop("_toolName", None)
        kind = str(meta.get("kind") or "")
        title = str(meta.get("title") or "")

        explicit_map = {
            "task": "Task",
            "askQuestion": "AskQuestion",
            "updateTodos": "TodoWrite",
            "createPlan": "CreatePlan",
            "generateImage": "GenerateImage",
            "generateVideo": "GenerateVideo",
        }
        if isinstance(explicit, str) and explicit in explicit_map:
            name = explicit_map[explicit]
        elif "providerIdentifier" in raw_args or "toolName" in raw_args:
            prov = raw_args.get("providerIdentifier") or "mcp"
            tname = raw_args.get("toolName") or "tool"
            name = f"mcp__{prov}__{tname}"
        elif kind == "execute":
            name = "Shell"
        elif kind == "edit":
            name = "Edit"
        elif kind == "delete":
            name = "Delete"
        elif kind == "read":
            name = "Read"
        elif kind == "fetch":
            name = "WebSearch" if "searchTerm" in raw_args else "WebFetch"
        elif kind == "switch_mode":
            name = "SwitchMode"
        elif kind == "search":
            if "query" in raw_args:
                name = "SemSearch"
            elif title.startswith("Find"):
                name = "Glob"
            elif title.startswith("List "):
                name = "Ls"
            else:
                name = "Grep"
        else:
            name = title.split(":", 1)[0].strip() or "Tool"

        if "path" not in raw_args:
            locations = meta.get("locations")
            if isinstance(locations, list) and locations:
                first_loc = locations[0]
                if isinstance(first_loc, dict) and isinstance(first_loc.get("path"), str):
                    raw_args["path"] = first_loc["path"]

        if name == "Read" and isinstance(raw_args.get("path"), str):
            raw_args.setdefault("file_path", raw_args["path"])

        if name in ("Edit", "Delete"):
            content_items = meta.get("content")
            if isinstance(content_items, list):
                for c_item in content_items:
                    if isinstance(c_item, dict) and c_item.get("type") == "diff":
                        if isinstance(c_item.get("path"), str):
                            raw_args.setdefault("path", c_item["path"])
                        if c_item.get("oldText") is not None:
                            raw_args.setdefault("old_string", c_item["oldText"])
                        if isinstance(c_item.get("newText"), str):
                            raw_args.setdefault("new_string", c_item["newText"])
                        break

        if name == "WebSearch" and "searchTerm" in raw_args and "query" not in raw_args:
            raw_args["query"] = raw_args["searchTerm"]

        return name, raw_args

    @staticmethod
    def _format_acp_tool_result(
        raw_out: Any, content_items: Any, *, tool_name: Optional[str] = None
    ) -> Any:
        if isinstance(raw_out, dict):
            if tool_name == "Task" and any(
                key in raw_out for key in ("success", "error", "failure", "rejected")
            ):
                return raw_out
            if "stdout" in raw_out or "stderr" in raw_out:
                parts: List[str] = []
                for k in ("stdout", "stderr"):
                    val = raw_out.get(k)
                    if isinstance(val, str) and val.strip():
                        parts.append(val.strip())
                return "\n".join(parts)
            if isinstance(raw_out.get("content"), str):
                return raw_out["content"]
            if isinstance(raw_out.get("error"), str):
                return raw_out["error"]
            return json.dumps(raw_out, ensure_ascii=False)
        if isinstance(raw_out, str):
            return raw_out
        if isinstance(content_items, list):
            texts: List[str] = []
            for item in content_items:
                if isinstance(item, dict) and item.get("type") == "content":
                    inner = item.get("content")
                    if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                        texts.append(inner["text"])
            if texts:
                return "\n".join(texts)
        return ""

    @staticmethod
    def _extract_text(message: Any) -> str:
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(parts)
        return ""
