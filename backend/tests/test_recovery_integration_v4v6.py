"""V4/V6 integration tests for cold-restart session attribution.

These tests construct realistic on-disk codex-rollout / cursor-store state in a
temporary HOME, then exercise ``TTYDManager.start_all_tabs()`` with tmux/ttyd
subprocess calls stubbed out. They verify that:

- V4-A: ``archived_sessions/`` flat layout is correctly scanned.
- V4-C: same-cwd multi-codex launches attribute to distinct sids (no cross-wiring).
- V4-B: verified-resume appends to the target rollout.
- V4-E: quarantined tab does not issue resume; fresh pin after quarantine.
- V6: single-tab cold launch discovers the new sid correctly.
- V4-D: cursor always pins a uuid4 and issues ``agent --resume <sid>``.
- V4-F: unexpected extra new-sid in same cwd triggers R8 quarantine.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from claude_hub.models.schemas import AgentType, ExecutionTarget

# Use importlib to get the actual module (not the singleton exported from
# services/__init__.py which shadows the module name).
tm = importlib.import_module("claude_hub.services.ttyd_manager")
from claude_hub.services._cursor_verify import (  # noqa: E402
    _cursor_id_exists as _cursor_id_exists_fn,
)

TTYDManager = tm.TTYDManager
TTYDProcess = tm.TTYDProcess
ScanEntry = tm.ScanEntry


# ─── helpers to build realistic on-disk state ────────────────────────────


def _write_codex_rollout(
    sessions_dir: Path,
    sid: str,
    cwd: str,
    start_epoch: float,
    *,
    archived: bool = False,
    extra_messages: int = 2,
) -> Path:
    """Write a minimal codex rollout jsonl matching V0 empirical format.

    ``sessions_dir`` should be ``<codex_home>/sessions``. Archived rolls go to
    ``<codex_home>/archived_sessions/`` (flat, no date partitioning — V0 finding).
    Active rolls go to ``<codex_home>/sessions/YYYY/MM/DD/`` date-partitioned.
    Returns the path to the written file.
    """
    ts_iso = datetime.fromtimestamp(start_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    codex_home = sessions_dir.parent  # sessions_dir == codex_home/sessions
    if archived:
        day_dir = codex_home / "archived_sessions"
        day_dir.mkdir(parents=True, exist_ok=True)
    else:
        dt = datetime.fromtimestamp(start_epoch, tz=timezone.utc)
        day_dir = sessions_dir / f"{dt.year:04d}" / f"{dt.month:02d}" / f"{dt.day:02d}"
        day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-{sid}.jsonl"
    lines = [
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": sid,
                    "session_id": str(uuid.uuid4()),  # V0: fork-thread id; differs
                    "cwd": cwd,
                    "timestamp": ts_iso,
                },
            }
        )
    ]
    for i in range(extra_messages):
        lines.append(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "role": "user" if i % 2 == 0 else "assistant",
                        "content": [{"type": "input_text", "text": f"message {i} for {sid}"}],
                    },
                }
            )
        )
    path.write_text("\n".join(lines) + "\n")
    return path


def _append_codex_rollout(path: Path, extra_messages: int = 1) -> None:
    """Append new messages to an existing rollout (simulates resume)."""
    existing = path.read_text()
    lines: List[str] = []
    for i in range(extra_messages):
        lines.append(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"appended message {i} at {time.time()}",
                            }
                        ],
                    },
                }
            )
        )
    # Advance mtime so mtime_ns changes
    path.write_text(existing + "\n".join(lines) + "\n")
    # Force mtime forward
    now = time.time() + 0.05
    os.utime(path, (now, now))


def _write_cursor_store(cursor_home: Path, cwd: str, sid: str) -> Path:
    """Create a minimal cursor store.db with meta key=0 hex-JSON pointing at sid."""
    import hashlib

    h = hashlib.md5(os.path.realpath(cwd).encode("utf-8")).hexdigest()
    chat_dir = cursor_home / "chats" / h / sid
    chat_dir.mkdir(parents=True, exist_ok=True)
    db_path = chat_dir / "store.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key INTEGER PRIMARY KEY, value TEXT)")
        payload = json.dumps({"agentId": sid, "chatId": sid, "cwd": cwd})
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (0, ?)",
            (payload.encode("utf-8").hex(),),
        )
        conn.commit()
    return db_path


# ─── subprocess stubbing ─────────────────────────────────────────────────


class _SubprocessStub:
    """Stubs out ``asyncio.create_subprocess_exec`` to fake tmux/ttyd launches.

    Instead of running real commands, the stub:
    - Recognises ``tmux new-session -d`` or ``tmux has-session`` calls.
    - For has-session: returns 1 (no session exists) unless explicitly seeded.
    - When the codex launch command is observed, triggers the creation of a new
      rollout file (simulating a fresh codex start) after a short async yield,
      allowing the fence-poll logic to observe it.
    """

    def __init__(
        self,
        codex_home: Path,
        existing_sessions: Dict[str, bool] | None = None,
        on_fresh_codex_start: Any = None,
        on_codex_resume: Any = None,
    ) -> None:
        self.codex_home = codex_home
        self.sessions_dir = codex_home / "sessions"
        self.existing_sessions = dict(existing_sessions or {})
        self.calls: List[Tuple[str, ...]] = []
        self._on_fresh = on_fresh_codex_start
        self._on_resume = on_codex_resume
        self._fresh_sids: List[str] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        cmd = tuple(str(a) for a in args)
        self.calls.append(cmd)
        prog = cmd[0] if cmd else ""

        if prog == "tmux":
            return await self._handle_tmux(cmd, **kwargs)
        if "ttyd" in prog or prog.endswith("ttyd"):
            return _FakeProc(returncode=0)
        # Default: pretend command ran and exited 0
        return _FakeProc(returncode=0)

    async def _handle_tmux(self, cmd: Tuple[str, ...], **kwargs: Any) -> _FakeProc:
        # tmux has-session -t <name>
        if "has-session" in cmd:
            # Find -t argument
            target = None
            for i, a in enumerate(cmd):
                if a == "-t" and i + 1 < len(cmd):
                    target = cmd[i + 1]
                    break
            if target and self.existing_sessions.get(target, False):
                return _FakeProc(returncode=0)
            return _FakeProc(returncode=1)

        # tmux new-session -d -s <name> <shell command>
        if "new-session" in cmd and "-d" in cmd:
            # Find -s value
            target = None
            for i, a in enumerate(cmd):
                if a == "-s" and i + 1 < len(cmd):
                    target = cmd[i + 1]
                    break
            # Find the shell command — it is the last argument (after the flags)
            # Our code calls new-session like:
            #   tmux new-session -d -s <name> <cmd>
            # where <cmd> is a long shell string containing 'codex' or 'agent'.
            shell_cmd = cmd[-1] if cmd else ""
            if target:
                self.existing_sessions[target] = True
            # Detect whether this is a codex launch and what kind
            if "codex" in shell_cmd:
                await self._handle_codex(shell_cmd)
            return _FakeProc(returncode=0)

        # tmux kill-session -t <name>
        if "kill-session" in cmd:
            target = None
            for i, a in enumerate(cmd):
                if a == "-t" and i + 1 < len(cmd):
                    target = cmd[i + 1]
                    break
            if target:
                self.existing_sessions[target] = False
            return _FakeProc(returncode=0)

        return _FakeProc(returncode=0)

    async def _handle_codex(self, shell_cmd: str) -> None:
        """Simulate codex starting.

        If the shell command is ``codex resume <sid>`` (verified resume) we
        append to that sid's rollout. Otherwise (fresh start) we create a new
        rollout with a fresh uuid. Hooks fire so tests can inject additional
        behaviour (e.g. R8 extra-sid injection).
        """
        # Detect resume: shell command has "codex resume <sid>"
        resumed_sid = None
        parts = shell_cmd.split()
        for i, p in enumerate(parts):
            if p == "resume" and i + 1 < len(parts):
                # Strip any shell quoting
                tok = parts[i + 1].strip("'\"")
                if len(tok) == 36 and tok.count("-") == 4:
                    resumed_sid = tok
                    break
        # Yield once so the fence-poll sees at least one async sleep
        await asyncio.sleep(0.05)
        if resumed_sid:
            # Find the existing rollout and append to it
            found = None
            for root, _dirs, files in os.walk(self.codex_home):
                for fn in files:
                    if fn.endswith(".jsonl") and resumed_sid in fn:
                        found = Path(root) / fn
                        break
                if found:
                    break
            if found:
                _append_codex_rollout(found, extra_messages=2)
                if self._on_resume:
                    await self._on_resume(resumed_sid, found, self)
            else:
                # Resume target doesn't exist on disk — codex falls back to fresh
                # (mirroring the ``|| codex`` chain).
                self._create_fresh()
        else:
            self._create_fresh()

    def _create_fresh(self) -> None:
        new_sid = str(uuid.uuid4())
        self._fresh_sids.append(new_sid)
        cwd = "/"  # default; callers override via on_fresh hook
        epoch = time.time()
        # Default to putting rollout in today's partition. The on_fresh hook may
        # override this.
        _write_codex_rollout(self.sessions_dir, new_sid, cwd, epoch)
        if self._on_fresh:
            self._on_fresh(new_sid, self.sessions_dir)


class _FakeStream:
    async def read(self, n: int = -1) -> bytes:
        return b""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _FakeProc:
    def __init__(self, returncode: int | None = None) -> None:
        # returncode=None means "still running" (matches asyncio subprocess)
        self.returncode = returncode
        self.pid = 1
        self.stdin = None
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        if self.returncode is None:
            self.returncode = 0
        return b"", b""

    def kill(self) -> None:
        self.returncode = -9

    def terminate(self) -> None:
        self.returncode = -15

    def send_signal(self, sig: int) -> None:
        pass


# ─── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Path]:
    """Set up a fake HOME with empty codex/cursor dirs."""
    home = tmp_path / "home"
    home.mkdir()
    codex_home = home / ".codex"
    (codex_home / "sessions").mkdir(parents=True)
    (codex_home / "archived_sessions").mkdir(parents=True)
    cursor_home = home / ".cursor"
    cursor_home.mkdir()
    # Monkeypatch HOME
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CURSOR_HOME", str(cursor_home))
    return {"home": home, "codex": codex_home, "cursor": cursor_home}


def _make_manager(monkeypatch: pytest.MonkeyPatch, stub: _SubprocessStub) -> TTYDManager:
    """Create a TTYDManager with subprocess stubbed and no real state file."""
    # Patch STATE_FILE before construction so _load_state reads a non-existent
    # file (returns empty processes dict).
    import tempfile

    empty_state = Path(tempfile.mkdtemp()) / "tabs.json"
    monkeypatch.setattr(tm, "STATE_FILE", empty_state)

    mgr = TTYDManager()
    assert len(mgr.processes) == 0, "_load_state should have found no tabs"
    # Override _save_state to no-op
    monkeypatch.setattr(mgr, "_save_state", lambda: None)
    # Patch asyncio.create_subprocess_exec (used by ensure_tmux_session / start / _tmux_session_exists_async)
    monkeypatch.setattr(tm.asyncio, "create_subprocess_exec", stub)

    # Patch sync subprocess.run (used by _ensure_tmux_server and _backfill_codex_session_ids)
    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=args, returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(tm.subprocess, "run", _fake_run)

    # Patch os.popen("tmux ls") to return empty (no sessions)
    class _FakePopen:
        def read(self) -> str:
            return "(no tmux sessions)"

        def close(self) -> None:
            pass

    monkeypatch.setattr(tm.os, "popen", lambda cmd: _FakePopen())

    # Patch _ensure_tmux_server to no-op
    monkeypatch.setattr(tm, "_ensure_tmux_server", lambda: None)

    # Patch _backfill_codex_session_ids and _backfill_agent_session_ids (they
    # hit live tmux via subprocess; they're best-effort and covered separately).
    monkeypatch.setattr(mgr, "_backfill_codex_session_ids", lambda: None)
    monkeypatch.setattr(mgr, "_backfill_agent_session_ids", lambda: None)

    # Patch _configure_tmux (it issues sync tmux calls which we don't care about
    # for attribution tests).
    async def _fake_configure_tmux(self) -> None:
        return None

    monkeypatch.setattr(TTYDProcess, "_configure_tmux", _fake_configure_tmux)

    # Override process.start() so we don't actually launch ttyd; instead we mark
    # active and schedule nothing. ensure_tmux_session is already called by the
    # codex/non-codex launch paths before start, which is what our stub intercepts.
    async def _fake_start(self) -> None:
        self.is_active = True
        # Simulate ttyd binding (the codex command was already spawned inside
        # tmux by ensure_tmux_session; ttyd just binds the port).
        self.process = _FakeProc(returncode=None)  # still running
        return None

    monkeypatch.setattr(TTYDProcess, "start", _fake_start)

    return mgr


def _tab(
    tab_id: str,
    port: int,
    agent_type: AgentType,
    cwd: str,
    agent_session_id: str | None = None,
    *,
    resume_quarantined: bool = False,
    from_persisted: bool = True,
) -> TTYDProcess:
    return TTYDProcess(
        tab_id=tab_id,
        port=port,
        name=f"tab-{tab_id}",
        agent_type=agent_type,
        cwd=cwd,
        agent_session_id=agent_session_id,
        from_persisted_state=from_persisted,
        resume_quarantined=resume_quarantined,
        target=ExecutionTarget.LOCAL,
        solo_mode=False,
    )


# ─── V6: single-tab fresh codex ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_v6_single_tab_fresh_codex_pins_new_sid(
    fake_home: Dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """V6: single cold codex tab without verified sid gets a fresh pin."""
    codex_home = fake_home["codex"]
    cwd = str(fake_home["home"] / "workspace-a")
    os.makedirs(cwd, exist_ok=True)

    stub = _SubprocessStub(codex_home)

    def _on_fresh(new_sid: str, sessions_dir: Path) -> None:
        # Rewrite the rollout with the correct cwd (stub default is '/').
        # The stub already wrote it; find and rewrite.
        for root, _d, files in os.walk(sessions_dir):
            for fn in files:
                if new_sid in fn:
                    p = Path(root) / fn
                    p.unlink()
                    break
        _write_codex_rollout(sessions_dir, new_sid, cwd, time.time() + 0.1)

    stub._on_fresh = _on_fresh
    mgr = _make_manager(monkeypatch, stub)

    tab = _tab("tab-v6", 13001, AgentType.CODEX, cwd, agent_session_id=None)
    # __init__ already assigns a uuid4 for codex
    pre_sid = tab.agent_session_id
    assert pre_sid  # should have been generated
    mgr.processes[tab.tab_id] = tab

    await mgr.start_all_tabs()

    # After start_all_tabs, the tab should be pinned to the newly-created sid,
    # which differs from the pre-launch placeholder (since there was no verified
    # sid on disk).
    assert tab.agent_session_id != pre_sid
    assert tab.agent_session_id is not None
    assert tab.resume_quarantined is False
    assert tab._is_new_pin is True
    # The rollout file should exist under codex_home
    found = tm._codex_id_exists(tab.agent_session_id, cwd)
    assert found, "newly pinned sid must be discoverable via _codex_id_exists"


# ─── V4-B: verified resume appends to existing rollout ──────────────────


@pytest.mark.asyncio
async def test_v4b_verified_resume_appends(
    fake_home: Dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = fake_home["codex"]
    cwd = str(fake_home["home"] / "workspace-b")
    os.makedirs(cwd, exist_ok=True)

    # Pre-existing rollout
    sid_a = str(uuid.uuid4())
    existing = _write_codex_rollout(codex_home / "sessions", sid_a, cwd, time.time() - 3600)
    pre_size = existing.stat().st_size

    stub = _SubprocessStub(codex_home)
    mgr = _make_manager(monkeypatch, stub)

    tab = _tab("tab-v4b", 13002, AgentType.CODEX, cwd, agent_session_id=sid_a)
    mgr.processes[tab.tab_id] = tab

    await mgr.start_all_tabs()

    assert tab.agent_session_id == sid_a
    assert tab._is_new_pin is False
    assert tab.resume_quarantined is False
    assert existing.stat().st_size > pre_size, "rollout should have been appended to"


# ─── V4-C: same-cwd multi-codex: distinct sids, no cross-wiring ─────────


@pytest.mark.asyncio
async def test_v4c_same_cwd_three_codex_no_crosswiring(
    fake_home: Dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """V4-C: three cold codex tabs in same cwd — each pins a distinct sid.

    Two are verified (pre-existing rollouts); one is fresh. All three should
    have distinct sids after start_all_tabs, and none should be quarantined.
    """
    codex_home = fake_home["codex"]
    cwd = str(fake_home["home"] / "workspace-c")
    os.makedirs(cwd, exist_ok=True)

    sid_v1 = str(uuid.uuid4())
    sid_v2 = str(uuid.uuid4())
    _write_codex_rollout(codex_home / "sessions", sid_v1, cwd, time.time() - 7200)
    _write_codex_rollout(codex_home / "sessions", sid_v2, cwd, time.time() - 3600)

    stub = _SubprocessStub(codex_home)
    fresh_sids_seen: List[str] = []

    def _on_fresh(new_sid: str, sessions_dir: Path) -> None:
        # Rewrite rollout with correct cwd
        for root, _d, files in os.walk(sessions_dir):
            for fn in files:
                if new_sid in fn:
                    (Path(root) / fn).unlink()
        _write_codex_rollout(sessions_dir, new_sid, cwd, time.time() + 0.05)
        fresh_sids_seen.append(new_sid)

    stub._on_fresh = _on_fresh

    mgr = _make_manager(monkeypatch, stub)

    t_verified1 = _tab("t-v1", 13010, AgentType.CODEX, cwd, agent_session_id=sid_v1)
    t_verified2 = _tab("t-v2", 13011, AgentType.CODEX, cwd, agent_session_id=sid_v2)
    t_fresh = _tab("t-fresh", 13012, AgentType.CODEX, cwd, agent_session_id=None)

    for t in (t_verified1, t_verified2, t_fresh):
        mgr.processes[t.tab_id] = t

    await mgr.start_all_tabs()

    sids = {t.agent_session_id for t in (t_verified1, t_verified2, t_fresh)}
    assert len(sids) == 3, f"expected 3 distinct sids, got {sids}"
    assert t_verified1.agent_session_id == sid_v1
    assert t_verified2.agent_session_id == sid_v2
    assert t_fresh.agent_session_id in fresh_sids_seen
    for t in (t_verified1, t_verified2, t_fresh):
        assert t.resume_quarantined is False, f"{t.tab_id} unexpectedly quarantined"


# ─── V4-A: archived_sessions flat layout scans correctly ────────────────


@pytest.mark.asyncio
async def test_v4a_archived_sessions_flat_is_discoverable(
    fake_home: Dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """V4-A: sessions in archived_sessions/ (flat layout) are found."""
    codex_home = fake_home["codex"]
    cwd = str(fake_home["home"] / "workspace-a")
    os.makedirs(cwd, exist_ok=True)

    # An archived (flat-layout) rollout from yesterday
    sid_old = str(uuid.uuid4())
    _write_codex_rollout(codex_home / "sessions", sid_old, cwd, time.time() - 86400, archived=True)
    assert (codex_home / "archived_sessions" / f"rollout-{sid_old}.jsonl").exists()

    # A scan must find it
    scan = tm._codex_scan_sessions()
    assert sid_old in scan, f"archived sid {sid_old} not in scan: {list(scan.keys())}"
    assert scan[sid_old].is_archived is True
    # _codex_id_exists must find it
    assert tm._codex_id_exists(sid_old, cwd) is True


# ─── V4-E: quarantined tab does NOT resume; gets fresh pin ──────────────


@pytest.mark.asyncio
async def test_v4e_quarantined_tab_starts_fresh(
    fake_home: Dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = fake_home["codex"]
    cwd = str(fake_home["home"] / "workspace-e")
    os.makedirs(cwd, exist_ok=True)

    sid_stale = str(uuid.uuid4())
    _write_codex_rollout(codex_home / "sessions", sid_stale, cwd, time.time() - 7200)

    stub = _SubprocessStub(codex_home)
    fresh_sids: List[str] = []

    def _on_fresh(new_sid: str, sessions_dir: Path) -> None:
        for root, _d, files in os.walk(sessions_dir):
            for fn in files:
                if new_sid in fn:
                    (Path(root) / fn).unlink()
        _write_codex_rollout(sessions_dir, new_sid, cwd, time.time() + 0.05)
        fresh_sids.append(new_sid)

    stub._on_fresh = _on_fresh
    mgr = _make_manager(monkeypatch, stub)

    # resume_quarantined=True: must NOT issue resume; start fresh
    tab = _tab(
        "t-quar",
        13020,
        AgentType.CODEX,
        cwd,
        agent_session_id=sid_stale,
        resume_quarantined=True,
    )
    mgr.processes[tab.tab_id] = tab

    await mgr.start_all_tabs()

    assert tab.agent_session_id != sid_stale
    assert tab.agent_session_id in fresh_sids
    assert tab._is_new_pin is True
    assert tab.resume_quarantined is False  # FRESH_PIN clears quarantine


# ─── V4-D: cursor always issues --resume <uuid> ─────────────────────────


@pytest.mark.asyncio
async def test_v4d_cursor_pins_and_uses_resume(
    fake_home: Dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """V4-D: an unverified Cursor id rotates to a constructive fresh pin."""
    cwd = str(fake_home["home"] / "workspace-d")
    os.makedirs(cwd, exist_ok=True)

    sid_cur = str(uuid.uuid4())
    stub = _SubprocessStub(fake_home["codex"])
    recorded_new_session_cmds: List[str] = []

    # Wrap the stub to capture the shell command passed to tmux new-session.
    original_handle_tmux = stub._handle_tmux

    async def wrapped_tmux(cmd: Tuple[str, ...], **kwargs: Any) -> _FakeProc:
        if "new-session" in cmd and "-d" in cmd:
            recorded_new_session_cmds.append(cmd[-1])
        return await original_handle_tmux(cmd, **kwargs)

    stub._handle_tmux = wrapped_tmux  # type: ignore[method-assign]

    mgr = _make_manager(monkeypatch, stub)
    tab = _tab("t-cur", 13030, AgentType.CURSOR, cwd, agent_session_id=sid_cur)
    mgr.processes[tab.tab_id] = tab

    await mgr.start_all_tabs()

    # The original sid has no cwd-scoped store, so recovery must rotate it and
    # constructively pin the replacement via ``agent --resume <new-sid>``.
    assert recorded_new_session_cmds, "expected a tmux new-session call"
    new_session_cmd = recorded_new_session_cmds[-1]
    assert tab.agent_session_id != sid_cur
    assert sid_cur not in new_session_cmd
    assert tab.agent_session_id is not None
    assert (
        f"--resume {tab.agent_session_id}" in new_session_cmd
        or f"--resume '{tab.agent_session_id}'" in new_session_cmd
        or f'--resume "{tab.agent_session_id}"' in new_session_cmd
    ), f"cursor launch did not issue --resume for pinned sid: {new_session_cmd[:300]}"


# ─── V4-F: R8 extra same-cwd sid triggers quarantine ────────────────────


@pytest.mark.asyncio
async def test_v4f_r8_extra_same_cwd_sid_quarantines_unpinned(
    fake_home: Dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """V4-F: if a fresh codex launch creates TWO new rollouts in the same cwd
    (simulating two rapid starts), the R8 reconciler must quarantine the
    unpinned fresh tab rather than risk misattribution."""
    codex_home = fake_home["codex"]
    cwd = str(fake_home["home"] / "workspace-f")
    os.makedirs(cwd, exist_ok=True)

    stub = _SubprocessStub(codex_home)

    def _on_fresh_with_extra(new_sid: str, sessions_dir: Path) -> None:
        # Write the expected rollout for new_sid AND a second "stray" rollout
        # in the same cwd, as if some external agent also launched codex.
        for root, _d, files in os.walk(sessions_dir):
            for fn in files:
                if new_sid in fn:
                    (Path(root) / fn).unlink()
        _write_codex_rollout(sessions_dir, new_sid, cwd, time.time() + 0.05)
        stray = str(uuid.uuid4())
        _write_codex_rollout(sessions_dir, stray, cwd, time.time() + 0.06)

    stub._on_fresh = _on_fresh_with_extra
    mgr = _make_manager(monkeypatch, stub)

    tab = _tab("t-r8", 13040, AgentType.CODEX, cwd, agent_session_id=None)
    mgr.processes[tab.tab_id] = tab

    await mgr.start_all_tabs()

    # R8 should have detected extras and quarantined the unpinned tab
    assert tab.resume_quarantined is True, "expected quarantine when extra same-cwd sids appear"


# ─── V4-G: resume_quarantined flag round-trips through state ────────────
# (covered by test_resume_quarantined_round_trips_through_state in
# test_ttyd_manager.py)


# ─── V4 cursor DB verification: _cursor_id_exists matches stored agentId ─


def test_v4_cursor_db_verification(fake_home: Dict[str, Path]) -> None:
    """Verify _cursor_id_exists finds a sid we just wrote to store.db."""
    cwd = str(fake_home["home"] / "workspace-cur")
    os.makedirs(cwd, exist_ok=True)
    sid = str(uuid.uuid4())
    _write_cursor_store(fake_home["cursor"], cwd, sid)
    assert _cursor_id_exists_fn(sid, cwd, home=str(fake_home["home"])) is True
    # A random other sid should NOT match
    assert _cursor_id_exists_fn(str(uuid.uuid4()), cwd, home=str(fake_home["home"])) is False
