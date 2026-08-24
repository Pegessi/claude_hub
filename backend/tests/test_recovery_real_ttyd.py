"""Real-process cold-restart integration tests for recovery session alignment.

These tests launch **actual** tmux + ttyd processes on a per-test isolated tmux
server (not the developer's shared server), using shell stand-ins for codex,
cursor, claude, and a plain terminal.

  1. Behave like the real CLIs:
       - ``codex [flags]`` (fresh) generates a new sid, writes a session_meta
         rollout, and stays alive.
       - ``codex resume <sid> [flags]`` looks up the rollout, appends a line
         (so fence-poll observes growth), and stays alive. If the sid is
         unknown, exits non-zero so the shell falls through to a fresh
         ``codex`` (just like the real ``|| codex`` fallback).
       - ``agent --resume <sid> [--yolo]`` is the constructive-pin Cursor CLI:
         creates store.db + meta.json at the expected path, prints a marker,
         and stays alive.
       - ``claude --session-id <sid> [...]`` prints a marker and stays alive.
Each stand-in emits a full identity marker containing agent kind, tab label,
canonical SID, cwd, and fresh/resume mode.  The terminal emits the same tuple
with ``sid=-`` and ``mode=shell``.

The test seeds the same durable state a prior process would have left behind,
then constructs one ``TTYDManager`` pointed at that ``tabs.json`` plus the
Codex/Cursor stores and performs exactly one cold recovery. The bijection
assertion: every pane shows its OWN marker and NO other tab's marker.

Layout (Goal Packet AC10/V7):
  - 7 tabs across 3 cwds: 3 codex + 2 cursor + 1 claude + 1 terminal
  - one owned Codex SID resumes from active storage and another from flat
    ``archived_sessions/``; a stray archived rollout remains a negative decoy
  - Real ports, unique per tab; no port conflicts.
  - Wall-clock oracle: the entire focused recovery test completes under 10 s.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import importlib
import json
import os
import shlex
import shutil
import socket
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _tmux(*args: str, check: bool = True, timeout: int = 10) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for v in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        env.pop(v, None)
    return subprocess.run(
        ["tmux", *args],
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _tmux_capture(session: str) -> str:
    for _ in range(15):
        try:
            r = _tmux("capture-pane", "-t", session, "-p", "-e", "-J", check=True, timeout=5)
            return r.stdout
        except subprocess.CalledProcessError:
            time.sleep(0.3)
    return ""


def _wait_port(port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def _wait_marker(session: str, marker: str, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = _tmux_capture(session)
        if marker in out:
            return True
        time.sleep(0.2)
    return False


def _identity_marker(kind: str, tab: str, sid: str, cwd: str, mode: str) -> str:
    return f"RECOVERY|kind={kind}|tab={tab}|sid={sid}|" f"cwd={os.path.realpath(cwd)}|mode={mode}"


# ---------------------------------------------------------------------------
# Stand-in scripts. Each accepts the real CLI argv shape and writes realistic
# side-effect artefacts (rollout JSONL / store.db) so ttyd_manager's parsers
# and scanners see authentic on-disk state.
# ---------------------------------------------------------------------------

_CODEX_STANDIN = r"""
import json, os, sys, time, uuid
from datetime import datetime, timezone
from pathlib import Path
args = sys.argv[1:]
filtered = []
i = 0
while i < len(args):
    a = args[i]
    if a in ("--ask-for-approval", "--sandbox"):
        i += 2; continue
    filtered.append(a); i += 1
args = filtered
sid = None; mode = "fresh"
if len(args) >= 2 and args[0] == "resume":
    sid = args[1]; mode = "resume"
if not sid:
    sid = str(uuid.uuid4())
cwd = os.getcwd()
tab = os.environ["CH_RECOVERY_TAB"]
home = Path(os.environ["HOME"])
sessions_dir = home / ".codex" / "sessions"
archive_dir = home / ".codex" / "archived_sessions"
sessions_dir.mkdir(parents=True, exist_ok=True)
archive_dir.mkdir(parents=True, exist_ok=True)
fnum = None
if mode == "resume":
    for root in (sessions_dir, archive_dir):
        for p in root.rglob(f"rollout-{sid}.jsonl"):
            fnum = p; break
        if fnum: break
    if fnum is None:
        sys.exit(1)
    with open(fnum, "a") as f:
        f.write(json.dumps({
            "type": "response_item",
            "payload": {"role": "assistant", "content": [{"type": "input_text", "text": "resumed-ok"}]},
        }) + "\n")
else:
    now = datetime.now(timezone.utc)
    date_part = now.strftime("%Y/%m/%d")
    day_dir = sessions_dir / date_part
    day_dir.mkdir(parents=True, exist_ok=True)
    fnum = day_dir / f"rollout-{sid}.jsonl"
    with open(fnum, "w") as f:
        f.write(json.dumps({
            "type": "session_meta",
            "payload": {"id": sid, "session_id": sid, "cwd": cwd,
                        "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.000Z")},
        }) + "\n")
sys.stdout.write(
    f"RECOVERY|kind=codex|tab={tab}|sid={sid}|cwd={os.path.realpath(cwd)}|mode={mode}\n"
)
sys.stdout.write(f"ROLLOUT={fnum}\n")
sys.stdout.flush()
while True:
    time.sleep(3600)
"""


_CURSOR_STANDIN = r"""
import hashlib, json, os, sqlite3, sys, time
from pathlib import Path
args = sys.argv[1:]
sid = None
i = 0
while i < len(args):
    a = args[i]
    if a == "--resume" and i + 1 < len(args):
        sid = args[i+1]; i += 2; continue
    if a == "--yolo":
        i += 1; continue
    i += 1
if not sid:
    sid = "unpinned-" + str(os.getpid())
cwd = os.getcwd()
tab = os.environ["CH_RECOVERY_TAB"]
home = Path(os.environ["HOME"])
h = hashlib.md5(os.path.realpath(cwd).encode()).hexdigest()
chat_dir = home / ".cursor" / "chats" / h / sid
mode = "resume" if (chat_dir / "store.db").exists() else "fresh"
chat_dir.mkdir(parents=True, exist_ok=True)
db = chat_dir / "store.db"
conn = sqlite3.connect(str(db))
conn.execute("CREATE TABLE IF NOT EXISTS meta (key INTEGER PRIMARY KEY, value BLOB)")
payload = json.dumps({"agentId": sid}).encode().hex()
conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (0, ?)", (payload,))
conn.commit()
conn.close()
(chat_dir / "meta.json").write_text(json.dumps({"chatId": sid, "cwd": os.path.realpath(cwd)}))
sys.stdout.write(
    f"RECOVERY|kind=cursor|tab={tab}|sid={sid}|cwd={os.path.realpath(cwd)}|mode={mode}\n"
)
sys.stdout.flush()
while True:
    time.sleep(3600)
"""


_CLAUDE_STANDIN = r"""
import os, sys, time
args = sys.argv[1:]
sid = None; mode = "fresh"; i = 0
while i < len(args):
    a = args[i]
    if a in ("--session-id", "--resume") and i + 1 < len(args):
        sid = args[i+1]
        mode = "resume" if a == "--resume" else "fresh"
        i += 2; continue
    if a == "--settings" and i + 1 < len(args):
        i += 2; continue
    if a == "--model" and i + 1 < len(args):
        i += 2; continue
    if a == "--dangerously-skip-permissions":
        i += 1; continue
    i += 1
if not sid:
    sid = "unpinned-" + str(os.getpid())
tab = os.environ["CH_RECOVERY_TAB"]
sys.stdout.write(
    f"RECOVERY|kind=claude|tab={tab}|sid={sid}|cwd={os.path.realpath(os.getcwd())}|mode={mode}\n"
)
sys.stdout.flush()
while True:
    time.sleep(3600)
"""


_TERMINAL_STANDIN = r"""
import os, sys, time
tab = os.environ["CH_RECOVERY_TAB"]
sys.stdout.write(
    f"RECOVERY|kind=terminal|tab={tab}|sid=-|cwd={os.path.realpath(os.getcwd())}|mode=shell\n"
)
sys.stdout.flush()
while True:
    time.sleep(3600)
"""


def _write_standin(tmp_home: Path, name: str, source: str) -> Path:
    bindir = tmp_home / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    p = bindir / name
    p.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    os.chmod(p, 0o755)
    return p


def _write_tmux_wrapper(tmp_home: Path, real_tmux: str, server_name: str) -> Path:
    """Route every production/test tmux call to this test's private server."""
    bindir = tmp_home / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    wrapper = bindir / "tmux"
    wrapper.write_text(
        "#!/bin/sh\nexec " + shlex.quote(real_tmux) + " -L " + shlex.quote(server_name) + ' "$@"\n',
        encoding="utf-8",
    )
    os.chmod(wrapper, 0o755)
    return wrapper


def _kill_isolated_tmux_server() -> None:
    """Kill only the server selected by the per-test PATH wrapper."""
    try:
        _tmux("kill-server", check=False, timeout=5)
    except Exception:
        pass


def _seed_codex_rollout(tmp_home: Path, sid: str, cwd: str, *, archived: bool) -> Path:
    if archived:
        parent = tmp_home / ".codex" / "archived_sessions"
    else:
        parent = tmp_home / ".codex" / "sessions" / "2026" / "08" / "08"
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / f"rollout-{sid}.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": sid,
                    "session_id": sid,
                    "cwd": os.path.realpath(cwd),
                    "timestamp": dt.datetime.now(dt.timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%S.000Z"
                    ),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _seed_cursor_store(tmp_home: Path, sid: str, cwd: str) -> Path:
    cwd_real = os.path.realpath(cwd)
    cwd_hash = hashlib.md5(cwd_real.encode()).hexdigest()
    chat_dir = tmp_home / ".cursor" / "chats" / cwd_hash / sid
    chat_dir.mkdir(parents=True, exist_ok=True)
    db_path = chat_dir / "store.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE meta (key INTEGER PRIMARY KEY, value BLOB)")
        payload = json.dumps({"agentId": sid}).encode().hex()
        conn.execute("INSERT INTO meta(key, value) VALUES (0, ?)", (payload,))
        conn.commit()
    finally:
        conn.close()
    (chat_dir / "meta.json").write_text(
        json.dumps({"chatId": sid, "cwd": cwd_real}), encoding="utf-8"
    )
    return db_path


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.flaky(reruns=2, reruns_delay=1)
async def test_real_cold_restart_7tab_bijection(tmp_path, monkeypatch):
    """Seven real ttyd/tmux tabs preserve exact identity in under 10 seconds."""
    real_ttyd = shutil.which("ttyd")
    real_tmux = shutil.which("tmux")
    if not real_ttyd or not real_tmux:
        pytest.skip("ttyd/tmux not available on PATH")
    if not shutil.which("python3"):
        pytest.skip("python3 not on PATH (stand-ins need it)")
    test_started = time.monotonic()

    tmp_home = tmp_path / "home"
    tmp_home.mkdir()
    _write_standin(tmp_home, "codex", _CODEX_STANDIN)
    _write_standin(tmp_home, "agent", _CURSOR_STANDIN)
    _write_standin(tmp_home, "claude", _CLAUDE_STANDIN)
    terminal_shell = _write_standin(tmp_home, "terminal-standin", _TERMINAL_STANDIN)
    _write_tmux_wrapper(tmp_home, real_tmux, f"ch-recovery-{uuid.uuid4().hex[:12]}")

    cwd_a = tmp_path / "cwdA"
    cwd_b = tmp_path / "cwdB"
    cwd_c = tmp_path / "cwdC"
    for cwd in (cwd_a, cwd_b, cwd_c):
        cwd.mkdir()

    monkeypatch.setenv("HOME", str(tmp_home))
    monkeypatch.setenv("PATH", f"{tmp_home/'bin'}{os.pathsep}{os.environ['PATH']}")
    for name in (
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "all_proxy",
        "no_proxy",
        "NO_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)

    tm = importlib.import_module("claude_hub.services.ttyd_manager")
    state_file = tmp_home / ".claude_hub" / "tabs.json"
    order_file = tmp_home / ".claude_hub" / "tab_order.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tm, "STATE_FILE", state_file)
    monkeypatch.setattr(tm, "ORDER_FILE", order_file)
    monkeypatch.setattr(tm.settings, "ttyd_path", real_ttyd)

    archive_dir = tmp_home / ".codex" / "archived_sessions"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stray_sid = "deadbeef-dead-dead-dead-deaddeadbeef"
    (archive_dir / f"rollout-{stray_sid}.jsonl").write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": stray_sid,
                    "session_id": stray_sid,
                    "cwd": str(cwd_a),
                    "timestamp": dt.datetime.now(dt.timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%S.000Z"
                    ),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    tab_specs: List[Tuple[str, Any, str, Any]] = [
        ("codex-A1", tm.AgentType.CODEX, str(cwd_a), None),
        ("codex-A2", tm.AgentType.CODEX, str(cwd_a), None),
        ("codex-B1", tm.AgentType.CODEX, str(cwd_b), None),
        ("cursor-C1", tm.AgentType.CURSOR, str(cwd_c), None),
        ("cursor-C2", tm.AgentType.CURSOR, str(cwd_c), None),
        ("claude-C", tm.AgentType.CLAUDE, str(cwd_c), None),
        ("terminal-B", tm.AgentType.TERMINAL, str(cwd_b), str(terminal_shell)),
    ]
    manager = None
    tab_metas: Dict[str, Dict[str, Any]] = {}

    def _assert_runtime(owner: Any, label: str) -> None:
        for tab_name, meta in tab_metas.items():
            proc = owner.processes[meta["id"]]
            assert proc.process is not None, f"{label} {tab_name}: ttyd process missing"
            assert proc.process.returncode is None, f"{label} {tab_name}: ttyd exited"
            assert _wait_port(proc.port, timeout=5.0), f"{label} {tab_name}: port not listening"

    def _assert_bijection(label: str, marker_key: str, tmux_key: str) -> None:
        all_markers = [meta[marker_key] for meta in tab_metas.values()]
        assert len(all_markers) == len(set(all_markers)), "identity markers must be unique"
        for tab_name, meta in tab_metas.items():
            content = _tmux_capture(meta[tmux_key])
            own = meta[marker_key]
            assert own in content, f"[{label}] {tab_name}: own marker missing\n{content[-1000:]}"
            for other in all_markers:
                if other != own:
                    assert (
                        other not in content
                    ), f"[{label}] {tab_name}: foreign marker present (CROSS-WIRING): {other}"

    try:
        used_ports: set[int] = set()
        persisted_rows: List[Dict[str, Any]] = []
        tab_order: List[str] = []
        codex_paths: Dict[str, Path] = {}
        for tab_name, agent_type, cwd, shell in tab_specs:
            port = _free_port()
            while port in used_ports:
                port = _free_port()
            used_ports.add(port)
            tab_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"claude-hub-recovery-{tab_name}"))
            sid = (
                str(uuid.uuid5(uuid.NAMESPACE_URL, f"claude-hub-recovery-sid-{tab_name}"))
                if agent_type != tm.AgentType.TERMINAL
                else None
            )
            proc = tm.TTYDProcess(
                tab_id=tab_id,
                port=port,
                name=tab_name,
                shell=shell,
                cwd=cwd,
                solo_mode=False,
                agent_type=agent_type,
                target=tm.ExecutionTarget.LOCAL,
                env={"CH_RECOVERY_TAB": tab_name},
                agent_session_id=sid,
                from_persisted_state=False,
            )
            persisted_rows.append(proc.to_dict())
            tab_order.append(tab_id)
            tab_metas[tab_name] = {
                "id": tab_id,
                "port": port,
                "cwd": cwd,
                "agent_type": agent_type,
                "sid": sid,
                "tmux": proc.tmux_session,
                "identity": (
                    tab_id,
                    agent_type.value,
                    os.path.realpath(cwd),
                    sid or "-",
                    proc.tmux_session,
                    port,
                ),
            }

            if agent_type == tm.AgentType.CODEX:
                assert sid is not None
                codex_paths[tab_name] = _seed_codex_rollout(
                    tmp_home,
                    sid,
                    cwd,
                    archived=tab_name == "codex-A1",
                )
            elif agent_type == tm.AgentType.CURSOR:
                assert sid is not None
                _seed_cursor_store(tmp_home, sid, cwd)

        assert len({meta["tmux"] for meta in tab_metas.values()}) == len(tab_specs)
        state_file.write_text(json.dumps(persisted_rows), encoding="utf-8")
        order_file.write_text(json.dumps(tab_order), encoding="utf-8")

        # Exercise a successful flat-archive resume and a sibling active resume
        # in the one cold-restart cycle measured by this test.
        archived_path = codex_paths["codex-A1"]
        active_path = codex_paths["codex-A2"]
        archived_pre = archived_path.stat()
        active_pre = active_path.stat()

        manager = tm.TTYDManager()
        assert all(proc.from_persisted_state for proc in manager.processes.values())
        cold_started = time.monotonic()
        await manager.start_all_tabs()
        cold_elapsed = time.monotonic() - cold_started
        assert cold_elapsed < 10.0, f"cold restart start_all_tabs took {cold_elapsed:.2f}s"
        _assert_runtime(manager, "post-restart")

        codex_sids: set[str] = set()
        for tab_name, meta in tab_metas.items():
            proc = manager.processes[meta["id"]]
            sid = proc.agent_session_id or "-"
            meta["marker"] = _identity_marker(
                proc.agent_type.value,
                tab_name,
                sid,
                meta["cwd"],
                "shell" if proc.agent_type == tm.AgentType.TERMINAL else "resume",
            )
            identity = (
                proc.tab_id,
                proc.agent_type.value,
                os.path.realpath(proc.cwd or ""),
                sid,
                proc.tmux_session,
                proc.port,
            )
            assert (
                identity == meta["identity"]
            ), f"{tab_name} identity changed across restart: {meta['identity']} -> {identity}"
            assert not proc.resume_quarantined, f"{tab_name} was quarantined"
            assert _wait_marker(proc.tmux_session, meta["marker"], timeout=5.0)
            if proc.agent_type == tm.AgentType.CODEX:
                assert sid != stray_sid
                assert sid not in codex_sids, f"duplicate Codex SID for {tab_name}"
                codex_sids.add(sid)

        _assert_bijection("post-restart", "marker", "tmux")
        archived_post = archived_path.stat()
        active_post = active_path.stat()
        assert archived_post.st_size > archived_pre.st_size
        assert archived_post.st_mtime_ns > archived_pre.st_mtime_ns
        assert active_post.st_size > active_pre.st_size
        assert active_post.st_mtime_ns > active_pre.st_mtime_ns
        overall_elapsed = time.monotonic() - test_started
        assert overall_elapsed < 10.0, f"full real recovery oracle took {overall_elapsed:.2f}s"

        print(
            f"\nreal-7tab PASSED: cold={cold_elapsed:.2f}s overall={overall_elapsed:.2f}s; "
            "active+archived resumes; exact full markers; 0 cross-wiring"
        )
        for tab_name, meta in tab_metas.items():
            print(f"  {tab_name}: identity={meta['identity']}")
    finally:
        if manager is not None:
            try:
                await manager.cleanup()
            except Exception:
                pass
        _kill_isolated_tmux_server()
