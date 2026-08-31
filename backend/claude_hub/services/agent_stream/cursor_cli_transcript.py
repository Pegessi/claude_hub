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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...models import (
    AgentStreamEvent,
    AgentStreamEventType,
    AgentType,
    ManagedSession,
    StreamCapabilities,
)
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
    supports_approval_ui = False
    supports_tool_timeline = True

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

        # Native stream-json stdout records carry a top-level ``type`` field
        # (``system``, ``thinking``, ``assistant``, ``result``). Transcript
        # file rows use ``role`` (``user``/``assistant``) or ``type:
        # turn_ended``. Dispatch on the native shape first.
        top_type = raw.get("type")
        if top_type in ("system", "thinking", "assistant", "result"):
            return self._normalize_stream_json(raw, ctx)

        role = raw.get("role")
        if role == "user":
            text = self._extract_text(raw.get("message"))
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
                            name = block.get("name") or "unknown"
                            args = block.get("input")
                            if not isinstance(args, dict):
                                args = {"input": args} if args is not None else {}
                            call_id = block.get("id")
                            events.append(
                                ctx.event(
                                    AgentStreamEventType.TOOL_CALL_STARTED,
                                    {"name": name, "args": args},
                                    call_id=call_id if isinstance(call_id, str) else None,
                                )
                            )
        elif raw.get("type") == "turn_ended":
            status = raw.get("status")
            if status == "success":
                events.append(
                    ctx.event(AgentStreamEventType.TURN_COMPLETED, {"status": "completed"})
                )
            elif status == "error":
                message = raw.get("error")
                if isinstance(message, str) and message:
                    events.append(ctx.event(AgentStreamEventType.ERROR, {"message": message}))
                events.append(ctx.event(AgentStreamEventType.TURN_COMPLETED, {"status": "failed"}))
            elif status == "aborted":
                events.append(
                    ctx.event(AgentStreamEventType.TURN_COMPLETED, {"status": "cancelled"})
                )
            self._clear_turn_state(ctx)
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
                    # Cursor distinguishes streaming chunks (timestamp_ms
                    # present) from the final full-text snapshot (no
                    # timestamp_ms). Streaming chunks are emitted and
                    # accumulated; the final snapshot is reconciled.
                    is_streaming_chunk = "timestamp_ms" in raw
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            text = block.get("text")
                            if isinstance(text, str) and text:
                                if is_streaming_chunk:
                                    state = self._get_turn_state(ctx)
                                    state.text += text
                                    events.append(
                                        ctx.event(
                                            AgentStreamEventType.TEXT_DELTA,
                                            {"text": text},
                                        )
                                    )
                                else:
                                    suffix = self._reconcile_text(ctx, text)
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
            return events
        if top_type == "result":
            is_error = raw.get("is_error", False)
            if is_error:
                err = raw.get("result") or raw.get("error") or "turn failed"
                if isinstance(err, str) and err:
                    events.append(ctx.event(AgentStreamEventType.ERROR, {"message": err}))
                events.append(ctx.event(AgentStreamEventType.TURN_COMPLETED, {"status": "failed"}))
            else:
                events.append(
                    ctx.event(AgentStreamEventType.TURN_COMPLETED, {"status": "completed"})
                )
            self._clear_turn_state(ctx)
            return events
        return events

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
