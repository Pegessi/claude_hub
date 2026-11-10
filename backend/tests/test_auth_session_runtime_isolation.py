"""Real auth-module startup must honor runtime isolation before opening storage."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_auth_storage_import_and_create_use_isolated_runtime(tmp_path):
    fake_home = tmp_path / "user"
    live = fake_home / ".claude_hub"
    live.mkdir(parents=True)
    original = live / "sessions.json"
    original.write_text('{"untouched":"legacy"}')
    isolated = tmp_path / "feature-runtime"
    environment = dict(
        os.environ,
        HOME=str(fake_home),
        CLAUDE_HUB_HOME=str(isolated),
        CLAUDE_HUB_STATE_ROOT=str(isolated / "workspaces"),
        CLAUDE_HUB_TMUX_SOCKET="auth-isolation-regression",
    )
    environment.pop("CLAUDE_HUB_ALLOW_LIVE_RUNTIME", None)
    code = """
import os
from pathlib import Path
from claude_hub.auth.session import SESSIONS_FILE, _load_sessions, create_session
from claude_hub.models import User
from claude_hub.services import runtime_isolation
from claude_hub.services.runtime_isolation import resolve_runtime_home
from claude_hub.services.local_storage import storage_location
assert SESSIONS_FILE == resolve_runtime_home() / "sessions.json"
assert _load_sessions() == {}
created = create_session(User(open_id="test", name="Test", email=""), "test-token", "test-refresh")
assert created.session_id in _load_sessions()
assert (resolve_runtime_home() / "storage.sqlite3").exists()
assert not (Path.home() / ".claude_hub" / "storage.sqlite3").exists()
linked_repo = Path(os.environ["CLAUDE_HUB_HOME"]) / "linked-worktree-probe"
linked_repo.mkdir(parents=True, exist_ok=True)
(linked_repo / ".git").write_text("gitdir: /tmp/fake-gitdir\\n")
runtime_isolation.infer_package_repo_root = lambda: linked_repo
try:
    storage_location(Path.home() / ".claude_hub" / "sessions.json")
except RuntimeError:
    pass
else:
    raise AssertionError("explicit legacy path escaped runtime isolation")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=environment,
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert original.read_text() == '{"untouched":"legacy"}'
    assert not (live / "storage.sqlite3").exists()
