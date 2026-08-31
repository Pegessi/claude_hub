"""Tests for the Cursor CLI transcript structured adapter.

Covers:
- Provenance validation (fail-closed to raw when any binding is wrong)
- Snapshot/rewrite-safe reconciliation with stable dedupe identifiers
- Normalization of the known same-pane JSONL events into AgentStreamEvent
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from claude_hub.models import (
    AgentStreamEventType,
    AgentType,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream.base import NormalizeContext
from claude_hub.services.agent_stream.cursor_cli_transcript import (
    CURSOR_CLI_TRANSCRIPT_SCHEMA,
    SUPPORTED_CURSOR_CLI_VERSIONS,
    CursorCliTranscriptAdapter,
    CursorTranscriptInvalid,
)


def _project_component(cwd: str) -> str:
    projected = re.sub(r"[^a-zA-Z0-9]", "-", str(Path(cwd).resolve(strict=False)))
    return re.sub(r"-+", "-", projected).strip("-")


def _make_session(**overrides: Any) -> ManagedSession:
    base: Dict[str, Any] = dict(
        id=f"sess-cursor-{uuid.uuid4().hex[:8]}",
        workspace_id="ws-1",
        tab_id=f"tab-{uuid.uuid4().hex[:8]}",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CURSOR,
        status=ManagedSessionStatus.WORKING,
        title="cursor",
        workspace_path="/tmp/cursor-proj",
        tmux_session="tmux-1",
        cursor_transport="terminal_transcript",
        cursor_data_dir="/tmp/cursor-data",
        cursor_cli_version=next(iter(SUPPORTED_CURSOR_CLI_VERSIONS)),
        cursor_transcript_schema=CURSOR_CLI_TRANSCRIPT_SCHEMA,
        agent_session_id=str(uuid.uuid4()),
        created_at=datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return ManagedSession(**base)


def _write_transcript(
    data_root: Path, cwd: str, session_id: str, rows: List[Dict[str, Any]]
) -> Path:
    comp = _project_component(cwd)
    transcript_dir = data_root / "projects" / comp / "agent-transcripts" / session_id
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_dir / f"{session_id}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return path


# ── provenance / path binding (fail-closed) ──────────────────────────────────


def test_discover_source_returns_pinned_path_when_provenance_valid(tmp_path: Path) -> None:
    cwd = str(tmp_path / "proj")
    data_root = tmp_path / "cursor-data"
    session = _make_session(workspace_path=cwd, cursor_data_dir=str(data_root))
    rows = [
        {"role": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"role": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
        {"type": "turn_ended", "status": "success"},
    ]
    path = _write_transcript(data_root, cwd, session.agent_session_id, rows)
    session = session.model_copy(update={"cursor_transcript_path": str(path)})

    adapter = CursorCliTranscriptAdapter()
    found = adapter.discover_source(session)
    assert found is not None
    assert found.resolve() == path.resolve()


def test_discover_source_canonicalizes_equivalent_workspace_path(tmp_path: Path) -> None:
    canonical_cwd = tmp_path / "proj"
    canonical_cwd.mkdir()
    data_root = tmp_path / "cursor-data"
    # This is intentionally a noncanonical spelling of the same cwd that the
    # ttyd launch path resolves before Cursor chooses its project directory.
    session = _make_session(
        workspace_path=str(canonical_cwd / ".." / "proj"),
        cursor_data_dir=str(data_root),
    )
    path = _write_transcript(
        data_root,
        str(canonical_cwd),
        session.agent_session_id,
        [{"role": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}],
    )
    session = session.model_copy(update={"cursor_transcript_path": str(path)})

    assert CursorCliTranscriptAdapter().discover_source(session) == path


def test_discover_source_none_when_transport_not_terminal_transcript(tmp_path: Path) -> None:
    cwd = str(tmp_path / "proj")
    data_root = tmp_path / "cursor-data"
    session = _make_session(
        workspace_path=cwd, cursor_data_dir=str(data_root), cursor_transport="terminal"
    )
    rows = [{"role": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}]
    path = _write_transcript(data_root, cwd, session.agent_session_id, rows)
    session = session.model_copy(update={"cursor_transcript_path": str(path)})

    adapter = CursorCliTranscriptAdapter()
    assert adapter.discover_source(session) is None


def test_discover_source_none_when_data_dir_missing(tmp_path: Path) -> None:
    cwd = str(tmp_path / "proj")
    session = _make_session(workspace_path=cwd, cursor_data_dir=None)
    rows = [{"role": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}]
    data_root = tmp_path / "cursor-data"
    path = _write_transcript(data_root, cwd, session.agent_session_id, rows)
    session = session.model_copy(update={"cursor_transcript_path": str(path)})

    adapter = CursorCliTranscriptAdapter()
    assert adapter.discover_source(session) is None


def test_discover_source_none_when_cli_version_unsupported(tmp_path: Path) -> None:
    cwd = str(tmp_path / "proj")
    data_root = tmp_path / "cursor-data"
    session = _make_session(
        workspace_path=cwd,
        cursor_data_dir=str(data_root),
        cursor_cli_version="0.0.0-unsupported",
    )
    rows = [{"role": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}]
    path = _write_transcript(data_root, cwd, session.agent_session_id, rows)
    session = session.model_copy(update={"cursor_transcript_path": str(path)})

    adapter = CursorCliTranscriptAdapter()
    assert adapter.discover_source(session) is None


def test_discover_source_none_when_schema_mismatch(tmp_path: Path) -> None:
    cwd = str(tmp_path / "proj")
    data_root = tmp_path / "cursor-data"
    session = _make_session(
        workspace_path=cwd,
        cursor_data_dir=str(data_root),
        cursor_transcript_schema="wrong-schema",
    )
    rows = [{"role": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}]
    path = _write_transcript(data_root, cwd, session.agent_session_id, rows)
    session = session.model_copy(update={"cursor_transcript_path": str(path)})

    adapter = CursorCliTranscriptAdapter()
    assert adapter.discover_source(session) is None


def test_discover_source_none_when_session_id_missing(tmp_path: Path) -> None:
    cwd = str(tmp_path / "proj")
    data_root = tmp_path / "cursor-data"
    session = _make_session(
        workspace_path=cwd, cursor_data_dir=str(data_root), agent_session_id=None
    )
    rows = [{"role": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}]
    # write under a dummy session id since agent_session_id is None
    dummy_sid = str(uuid.uuid4())
    path = _write_transcript(data_root, cwd, dummy_sid, rows)
    session = session.model_copy(update={"cursor_transcript_path": str(path)})

    adapter = CursorCliTranscriptAdapter()
    assert adapter.discover_source(session) is None


def test_discover_source_none_when_path_not_absolute(tmp_path: Path) -> None:
    cwd = str(tmp_path / "proj")
    data_root = tmp_path / "cursor-data"
    session = _make_session(workspace_path=cwd, cursor_data_dir=str(data_root))
    rows = [{"role": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}]
    _write_transcript(data_root, cwd, session.agent_session_id, rows)
    session = session.model_copy(update={"cursor_transcript_path": "relative/path.jsonl"})

    adapter = CursorCliTranscriptAdapter()
    assert adapter.discover_source(session) is None


def test_discover_source_none_when_path_does_not_match_data_dir_cwd_session_binding(
    tmp_path: Path,
) -> None:
    cwd = str(tmp_path / "proj")
    data_root = tmp_path / "cursor-data"
    session = _make_session(workspace_path=cwd, cursor_data_dir=str(data_root))
    rows = [{"role": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}]
    # write to a path that does NOT match the data_dir/cwd/session binding
    other_dir = data_root / "projects" / "x" / "agent-transcripts" / "y"
    other_dir.mkdir(parents=True, exist_ok=True)
    bad_path = other_dir / "y.jsonl"
    bad_path.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    session = session.model_copy(update={"cursor_transcript_path": str(bad_path)})

    adapter = CursorCliTranscriptAdapter()
    assert adapter.discover_source(session) is None


def test_discover_source_none_when_file_missing(tmp_path: Path) -> None:
    cwd = str(tmp_path / "proj")
    data_root = tmp_path / "cursor-data"
    session = _make_session(workspace_path=cwd, cursor_data_dir=str(data_root))
    comp = _project_component(cwd)
    missing = (
        data_root
        / "projects"
        / comp
        / "agent-transcripts"
        / session.agent_session_id
        / f"{session.agent_session_id}.jsonl"
    )
    session = session.model_copy(update={"cursor_transcript_path": str(missing)})

    adapter = CursorCliTranscriptAdapter()
    assert adapter.discover_source(session) is None


def test_capabilities_structured_false_when_provenance_invalid(tmp_path: Path) -> None:
    cwd = str(tmp_path / "proj")
    data_root = tmp_path / "cursor-data"
    session = _make_session(
        workspace_path=cwd, cursor_data_dir=str(data_root), cursor_transport="terminal"
    )
    adapter = CursorCliTranscriptAdapter()
    caps = adapter.capabilities(session)
    assert caps.structured is False


def test_capabilities_structured_true_when_provenance_valid(tmp_path: Path) -> None:
    cwd = str(tmp_path / "proj")
    data_root = tmp_path / "cursor-data"
    session = _make_session(workspace_path=cwd, cursor_data_dir=str(data_root))
    rows = [{"role": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}]
    path = _write_transcript(data_root, cwd, session.agent_session_id, rows)
    session = session.model_copy(update={"cursor_transcript_path": str(path)})

    adapter = CursorCliTranscriptAdapter()
    caps = adapter.capabilities(session)
    assert caps.structured is True
    assert caps.adapter_id == adapter.adapter_id


# ── snapshot / rewrite-safe reconciliation + stable dedupe ───────────────────


def test_read_snapshot_returns_digest_and_stable_source_ids(tmp_path: Path) -> None:
    cwd = str(tmp_path / "proj")
    data_root = tmp_path / "cursor-data"
    session = _make_session(workspace_path=cwd, cursor_data_dir=str(data_root))
    rows = [
        {"role": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"role": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
        {"type": "turn_ended", "status": "success"},
    ]
    path = _write_transcript(data_root, cwd, session.agent_session_id, rows)
    session = session.model_copy(update={"cursor_transcript_path": str(path)})

    adapter = CursorCliTranscriptAdapter()
    snap = adapter.read_snapshot(path, session)

    assert snap.digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(snap.records) == 3
    # source_ids must be unique and stable
    ids = [r.source_id for r in snap.records]
    assert len(ids) == len(set(ids))
    # re-reading the same file yields identical source_ids
    snap2 = adapter.read_snapshot(path, session)
    assert [r.source_id for r in snap2.records] == ids


def test_read_snapshot_deduplicates_identical_rows_with_occurrence_ordinals(tmp_path: Path) -> None:
    cwd = str(tmp_path / "proj")
    data_root = tmp_path / "cursor-data"
    session = _make_session(workspace_path=cwd, cursor_data_dir=str(data_root))
    row = {"role": "user", "message": {"content": [{"type": "text", "text": "dup"}]}}
    rows = [row, row, row]
    path = _write_transcript(data_root, cwd, session.agent_session_id, rows)
    session = session.model_copy(update={"cursor_transcript_path": str(path)})

    adapter = CursorCliTranscriptAdapter()
    snap = adapter.read_snapshot(path, session)

    ids = [r.source_id for r in snap.records]
    assert len(ids) == 3
    # identical rows get distinct occurrence ordinals, so ids are unique
    assert len(set(ids)) == 3
    # the shared row digest is the same; only the occurrence suffix differs
    assert ids[0].endswith(":0")
    assert ids[1].endswith(":1")
    assert ids[2].endswith(":2")


def test_read_snapshot_rejects_partial_final_line(tmp_path: Path) -> None:
    cwd = str(tmp_path / "proj")
    data_root = tmp_path / "cursor-data"
    session = _make_session(workspace_path=cwd, cursor_data_dir=str(data_root))
    rows = [{"role": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}]
    path = _write_transcript(data_root, cwd, session.agent_session_id, rows)
    # append a partial line without trailing newline
    with path.open("a", encoding="utf-8") as f:
        f.write('{"role": "assistant"')
    session = session.model_copy(update={"cursor_transcript_path": str(path)})

    adapter = CursorCliTranscriptAdapter()
    with pytest.raises(CursorTranscriptInvalid):
        adapter.read_snapshot(path, session)


def test_read_snapshot_rejects_unknown_row_type(tmp_path: Path) -> None:
    cwd = str(tmp_path / "proj")
    data_root = tmp_path / "cursor-data"
    session = _make_session(workspace_path=cwd, cursor_data_dir=str(data_root))
    rows = [{"weird": "row"}]
    path = _write_transcript(data_root, cwd, session.agent_session_id, rows)
    session = session.model_copy(update={"cursor_transcript_path": str(path)})

    adapter = CursorCliTranscriptAdapter()
    with pytest.raises(CursorTranscriptInvalid):
        adapter.read_snapshot(path, session)


# ── normalization ────────────────────────────────────────────────────────────


def _ctx(session: ManagedSession) -> NormalizeContext:
    return NormalizeContext(
        session_id=session.id,
        tab_id=session.tab_id,
        agent_type=session.agent_type,
        run_epoch=0,
    )


def test_normalize_user_text_emits_turn_started() -> None:
    session = _make_session()
    adapter = CursorCliTranscriptAdapter()
    raw = {"role": "user", "message": {"content": [{"type": "text", "text": "hello"}]}}
    events = adapter.normalize_line(raw, _ctx(session))
    assert len(events) == 1
    assert events[0].type == AgentStreamEventType.TURN_STARTED
    assert events[0].payload["summary"] == "hello"


def test_normalize_assistant_text_emits_text_delta() -> None:
    session = _make_session()
    adapter = CursorCliTranscriptAdapter()
    raw = {"role": "assistant", "message": {"content": [{"type": "text", "text": "hi there"}]}}
    events = adapter.normalize_line(raw, _ctx(session))
    assert len(events) == 1
    assert events[0].type == AgentStreamEventType.TEXT_DELTA
    assert events[0].payload["text"] == "hi there"


def test_normalize_assistant_tool_use_emits_tool_call_started() -> None:
    session = _make_session()
    adapter = CursorCliTranscriptAdapter()
    raw = {
        "role": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "running"},
                {"type": "tool_use", "name": "bash", "input": {"cmd": "ls"}},
            ]
        },
    }
    events = adapter.normalize_line(raw, _ctx(session))
    types = [e.type for e in events]
    assert AgentStreamEventType.TEXT_DELTA in types
    assert AgentStreamEventType.TOOL_CALL_STARTED in types
    tool_ev = next(e for e in events if e.type == AgentStreamEventType.TOOL_CALL_STARTED)
    assert tool_ev.payload["name"] == "bash"
    assert tool_ev.payload["args"] == {"cmd": "ls"}


def test_normalize_turn_ended_success_emits_turn_completed() -> None:
    session = _make_session()
    adapter = CursorCliTranscriptAdapter()
    raw = {"type": "turn_ended", "status": "success"}
    events = adapter.normalize_line(raw, _ctx(session))
    assert len(events) == 1
    assert events[0].type == AgentStreamEventType.TURN_COMPLETED
    assert events[0].payload["status"] == "completed"


def test_normalize_turn_ended_error_emits_error_then_turn_completed() -> None:
    session = _make_session()
    adapter = CursorCliTranscriptAdapter()
    raw = {"type": "turn_ended", "status": "error", "error": "boom"}
    events = adapter.normalize_line(raw, _ctx(session))
    types = [e.type for e in events]
    assert types == [AgentStreamEventType.ERROR, AgentStreamEventType.TURN_COMPLETED]
    assert events[0].payload["message"] == "boom"
    assert events[1].payload["status"] == "failed"


def test_normalize_turn_ended_aborted_emits_cancelled() -> None:
    session = _make_session()
    adapter = CursorCliTranscriptAdapter()
    raw = {"type": "turn_ended", "status": "aborted"}
    events = adapter.normalize_line(raw, _ctx(session))
    assert len(events) == 1
    assert events[0].type == AgentStreamEventType.TURN_COMPLETED
    assert events[0].payload["status"] == "cancelled"


def test_normalize_skips_unknown_row_without_raising() -> None:
    session = _make_session()
    adapter = CursorCliTranscriptAdapter()
    events = adapter.normalize_line({"unknown": "row"}, _ctx(session))
    assert events == []
