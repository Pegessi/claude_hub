"""Real-tmux integration tests for the receipt-based at-most-once delivery.

These tests exercise the production ``_send_tmux_message_with_receipt`` and
``_query_tmux_receipt`` primitives against a *real* tmux server (the default
one, with a UUID-unique session name). No production helper is patched — the
only seams are the crash-boundary wrappers that raise before/after the real
send, plus a temp state root for the ``WorkspaceManager``.

An "effect file" records every byte pasted into the session (the session runs
``cat >> effect_file``). Counting occurrences of the call_id marker in that
file tells us exactly how many times the message was actually pasted — the
receipt must keep that count at 1 even across sequential duplicates and 10
concurrent same-call_id sends.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import uuid
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Generator

import pytest

from claude_hub.models import (
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceCreate,
    WorkspaceSessionRole,
)
from claude_hub.services.workspace_manager import WorkspaceManager

_wm = import_module("claude_hub.services.workspace_manager")


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


pytestmark = pytest.mark.skipif(not _tmux_available(), reason="tmux not installed")


@pytest.fixture()
def state_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolated state root so the WorkspaceManager does not touch real state."""
    root = tmp_path / "workspaces"
    root.mkdir(parents=True, exist_ok=True)
    index_file = root / "index.json"
    monkeypatch.setattr(_wm, "STATE_ROOT", root)
    monkeypatch.setattr(_wm, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._persistence, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._state, "INDEX_FILE", index_file)
    return root


@pytest.fixture()
def manager(state_root: Path) -> WorkspaceManager:
    return WorkspaceManager()


@pytest.fixture()
def tmux_session(tmp_path: Path) -> Generator[tuple[str, Path], None, None]:
    """Create a UUID-unique tmux session on the default server.

    The session runs ``cat >> effect_file`` so every pasted byte is appended
    to ``effect_file``. Counting call_id marker occurrences in that file
    measures how many times the message was actually pasted.
    """
    session_name = f"test-receipt-{uuid.uuid4().hex[:12]}"
    effect_file = tmp_path / "effect.log"
    effect_file.write_text("")

    # Run cat in append mode; all pasted input lands in effect_file.
    # Use `exec` so the shell is replaced by cat (no extra prompt lines).
    cmd = f"exec cat >> {effect_file}"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "bash", "-c", cmd],
        check=True,
    )
    # Give the session a moment to start.
    subprocess.run(["tmux", "has-session", "-t", session_name], check=True)

    try:
        yield session_name, effect_file
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session_name], check=False)
        # Best-effort buffer cleanup (named buffers are server-global).
        # The receipt mechanism makes this safe even if a buffer lingers.
        for buf_prefix in ("buf_",):
            pass  # named buffers are per (call_id, session); session kill is enough


def _marker(call_id: str) -> str:
    return f"[call_id:{call_id}]"


def _count_pastes(effect_file: Path, call_id: str) -> int:
    if not effect_file.exists():
        return 0
    text = effect_file.read_text(errors="ignore")
    return text.count(_marker(call_id))


async def test_sequential_duplicate_same_call_id_pastes_once(
    manager: WorkspaceManager,
    tmux_session: tuple[str, Path],
) -> None:
    """Two sequential sends with the same call_id paste exactly once."""
    session_name, effect_file = tmux_session
    call_id = "seq-dup-" + uuid.uuid4().hex[:8]
    message = f"{_marker(call_id)}\nhello from sequential test"

    # First send: must paste and set receipt.
    await manager._send_tmux_message_with_receipt(session_name, message, call_id)
    assert await manager._query_tmux_receipt(session_name, call_id) is True
    assert _count_pastes(effect_file, call_id) == 1

    # Second send with the same call_id: receipt is present → no-op, no repaste.
    await manager._send_tmux_message_with_receipt(session_name, message, call_id)
    assert await manager._query_tmux_receipt(session_name, call_id) is True
    assert _count_pastes(effect_file, call_id) == 1


async def test_ten_concurrent_same_call_id_pastes_once(
    manager: WorkspaceManager,
    tmux_session: tuple[str, Path],
) -> None:
    """10 concurrent sends with the same call_id paste exactly once."""
    session_name, effect_file = tmux_session
    call_id = "conc-" + uuid.uuid4().hex[:8]
    message = f"{_marker(call_id)}\nhello from concurrent test"

    await asyncio.gather(
        *[
            manager._send_tmux_message_with_receipt(session_name, message, call_id)
            for _ in range(10)
        ]
    )

    assert await manager._query_tmux_receipt(session_name, call_id) is True
    assert _count_pastes(effect_file, call_id) == 1


async def test_pre_send_failure_sets_no_receipt_and_no_paste(
    manager: WorkspaceManager,
    tmux_session: tuple[str, Path],
) -> None:
    """If the Hub raises *before* the production send runs, no paste happens
    and no receipt is set. The call_id stays safe to retry."""
    session_name, effect_file = tmux_session
    call_id = "pre-fail-" + uuid.uuid4().hex[:8]
    message = f"{_marker(call_id)}\nshould never be pasted"

    real_send = manager._send_tmux_message_with_receipt

    async def raise_before_send(self, tmux_session, message, call_id):
        # Simulate a Hub-side failure before the tmux command runs.
        raise RuntimeError("simulated pre-send failure")

    manager._send_tmux_message_with_receipt = raise_before_send.__get__(  # type: ignore[method-assign]
        manager, WorkspaceManager
    )
    try:
        with pytest.raises(RuntimeError, match="pre-send failure"):
            await manager._send_tmux_message_with_receipt(session_name, message, call_id)
    finally:
        manager._send_tmux_message_with_receipt = real_send  # type: ignore[method-assign]

    # No receipt, no paste: the tmux side never saw the call.
    assert await manager._query_tmux_receipt(session_name, call_id) is False
    assert _count_pastes(effect_file, call_id) == 0


async def test_post_send_failure_receipt_present_no_repaste_on_recovery(
    manager: WorkspaceManager,
    tmux_session: tuple[str, Path],
    tmp_path: Path,
) -> None:
    """If the production send succeeds (paste + receipt set) but the Hub
    raises *after* it returns, the Hub thinks the send failed. Cold recovery
    must see the receipt and NOT repaste.

    This is the "tmux accepted but Hub returned before failure" boundary.

    The recovery is exercised through the *production* cold-start path: we
    persist a LIVE ``ManagedSession`` whose call_id sits in
    ``processing_call_ids`` (with the stored envelope + fingerprint), spin up
    a brand-new ``WorkspaceManager`` that reloads state, and call
    ``_recover_processing_via_receipt`` (which queries the real tmux receipt).
    The call_id must stay in ``processing`` (not pending, not uncertain) and
    the effect count must remain 1. A subsequent ``resume_existing_call`` /
    pump must also not repaste.
    """
    session_name, effect_file = tmux_session
    call_id = "post-fail-" + uuid.uuid4().hex[:8]
    message = f"{_marker(call_id)}\npasted once, never again"

    real_send = manager._send_tmux_message_with_receipt

    async def raise_after_send(self, tmux_session, message, call_id):
        # Let the real send run (paste + receipt), then crash.
        await real_send(tmux_session, message, call_id)
        raise RuntimeError("simulated post-send failure")

    manager._send_tmux_message_with_receipt = raise_after_send.__get__(  # type: ignore[method-assign]
        manager, WorkspaceManager
    )
    try:
        with pytest.raises(RuntimeError, match="post-send failure"):
            await manager._send_tmux_message_with_receipt(session_name, message, call_id)
    finally:
        manager._send_tmux_message_with_receipt = real_send  # type: ignore[method-assign]

    # The paste happened and the receipt is set, even though Hub raised.
    assert await manager._query_tmux_receipt(session_name, call_id) is True
    assert _count_pastes(effect_file, call_id) == 1

    # --- True cold recovery -------------------------------------------------
    # Build a LIVE ManagedSession that reflects the post-crash state: the
    # call_id is in processing_call_ids (the pump had moved it there before
    # sending), the message body + fingerprint are persisted, and the tmux
    # session is the real one we just pasted into.
    ws = manager.create_workspace(
        WorkspaceCreate(
            name="receipt-cold-recovery",
            path=str(tmp_path),
            target=ExecutionTarget.LOCAL,
        )
    )
    session_id = "sess-cold-" + uuid.uuid4().hex[:8]
    fp = manager._compute_payload_fingerprint(message, [])
    now = datetime.utcnow()
    session = ManagedSession(
        id=session_id,
        workspace_id=ws.id,
        tab_id=f"tab-{session_id}",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.WORKING,  # LIVE: not STOPPED
        title="cold-recovery worker",
        workspace_path=str(tmp_path),
        tmux_session=session_name,
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
        processing_call_ids=[call_id],
        pending_messages={call_id: message},
        call_payload_fingerprints={call_id: fp},
    )
    manager.sessions[session_id] = session
    manager._save_state()

    # Reload state in a fresh manager — this is the cold-start path.
    fresh = WorkspaceManager()
    assert session_id in fresh.sessions
    reloaded = fresh.sessions[session_id]
    # LIVE session: _recover_uncertain_deliveries must NOT move processing
    # call_ids to uncertain (only STOPPED sessions get that).
    assert call_id in reloaded.processing_call_ids
    assert call_id not in reloaded.uncertain_call_ids

    # Production receipt-based reconciliation against the real tmux session.
    changed = await fresh._recover_processing_via_receipt(session_id)
    assert changed == 0  # receipt present → no state change

    recovered = fresh.sessions[session_id]
    # Receipt present → keep processing, do NOT move to pending or uncertain.
    assert call_id in recovered.processing_call_ids
    assert call_id not in recovered.pending_call_ids
    assert call_id not in recovered.uncertain_call_ids
    # No repaste: the receipt made the atomic check-and-paste a no-op, and
    # the submit-nudge only sends C-m (not the message body).
    assert _count_pastes(effect_file, call_id) == 1

    # resume_existing_call for a processing call_id is a no-op (already
    # in-flight); it must not repaste.
    resumed = await fresh.resume_existing_call(session_id, call_id)
    assert resumed is True
    assert _count_pastes(effect_file, call_id) == 1

    # The pump must also skip the already-processing call_id.
    await fresh._pump_session_messages(session_id)
    assert _count_pastes(effect_file, call_id) == 1
    assert call_id in fresh.sessions[session_id].processing_call_ids
