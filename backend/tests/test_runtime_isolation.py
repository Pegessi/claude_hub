"""Worktree backends must not resolve to the live Hub home or default tmux."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from claude_hub.services.runtime_isolation import (
    detect_linked_worktree_slug,
    resolve_runtime_home,
    resolve_state_root,
    resolve_tmux_socket_name,
    tmux_command,
)


def _linked_worktree(tmp_path: Path) -> Path:
    repo = tmp_path / "claude_hub-feature"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /tmp/fake/worktrees/feature\n", encoding="utf-8")
    return repo


def _primary_checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "claude_hub"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


def test_detect_linked_worktree_slug(tmp_path: Path) -> None:
    assert detect_linked_worktree_slug(_linked_worktree(tmp_path)) == "claude_hub-feature"
    assert detect_linked_worktree_slug(_primary_checkout(tmp_path)) is None


def test_worktree_home_is_not_live(tmp_path: Path) -> None:
    repo = _linked_worktree(tmp_path)
    home = resolve_runtime_home(repo_root=repo, environ={})
    assert home == Path.home() / ".claude_hub" / "worktrees" / "claude_hub-feature"
    assert home != Path.home() / ".claude_hub"
    state = resolve_state_root(repo_root=repo, environ={})
    assert state == home / "workspaces"
    assert state != Path.home() / ".claude_hub" / "workspaces"


def test_primary_checkout_keeps_live_paths(tmp_path: Path) -> None:
    repo = _primary_checkout(tmp_path)
    assert resolve_runtime_home(repo_root=repo, environ={}) == Path.home() / ".claude_hub"
    assert (
        resolve_state_root(repo_root=repo, environ={}) == Path.home() / ".claude_hub" / "workspaces"
    )
    assert resolve_tmux_socket_name(repo_root=repo, environ={}) is None


def test_worktree_refuses_explicit_live_state_root(tmp_path: Path) -> None:
    repo = _linked_worktree(tmp_path)
    live = str(Path.home() / ".claude_hub" / "workspaces")
    with pytest.raises(RuntimeError, match="refusing live STATE_ROOT"):
        resolve_state_root(repo_root=repo, environ={"CLAUDE_HUB_STATE_ROOT": live})


def test_worktree_refuses_empty_tmux_socket(tmp_path: Path) -> None:
    repo = _linked_worktree(tmp_path)
    with pytest.raises(RuntimeError, match="refusing default tmux server"):
        resolve_tmux_socket_name(repo_root=repo, environ={"CLAUDE_HUB_TMUX_SOCKET": ""})


def test_worktree_lock_and_logs_are_not_live(tmp_path: Path) -> None:
    repo = _linked_worktree(tmp_path)
    home = resolve_runtime_home(repo_root=repo, environ={})
    assert home / "backend.lock" != Path.home() / ".claude_hub" / "backend.lock"
    assert home / "logs" / "backend.log" != Path.home() / ".claude_hub" / "logs" / "backend.log"


def test_backend_file_log_rotates_an_existing_oversized_file(tmp_path: Path) -> None:
    from claude_hub import main

    log_file = tmp_path / "backend.log"
    previous_log = "previous backend output\n"
    log_file.write_text(previous_log, encoding="utf-8")

    with main.backend_file_logging(log_file, max_bytes=8, backup_count=2) as handler:
        logging.getLogger("rotation-test").info("new backend output")
        assert handler.maxBytes == 8
        assert handler.backupCount == 2

    assert (tmp_path / "backend.log.1").read_text(encoding="utf-8") == previous_log
    assert "new backend output" in log_file.read_text(encoding="utf-8")


def test_backend_log_settings_defaults_overrides_and_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from claude_hub.config import Settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BACKEND_LOG_MAX_BYTES", raising=False)
    monkeypatch.delenv("BACKEND_LOG_BACKUP_COUNT", raising=False)

    defaults = Settings(_env_file=None)
    assert defaults.backend_log_max_bytes == 10 * 1024 * 1024
    assert defaults.backend_log_backup_count == 5

    monkeypatch.setenv("BACKEND_LOG_MAX_BYTES", "2048")
    monkeypatch.setenv("BACKEND_LOG_BACKUP_COUNT", "3")
    overridden = Settings(_env_file=None)
    assert overridden.backend_log_max_bytes == 2048
    assert overridden.backend_log_backup_count == 3

    for invalid in (0, -1):
        with pytest.raises(ValueError, match="must be positive"):
            Settings(_env_file=None, backend_log_max_bytes=invalid)
        with pytest.raises(ValueError, match="must be positive"):
            Settings(_env_file=None, backend_log_backup_count=invalid)


def test_worktree_tmux_command_uses_named_socket(tmp_path: Path) -> None:
    repo = _linked_worktree(tmp_path)
    cmd = tmux_command("ls", repo_root=repo, environ={})
    assert cmd[:3] == ["tmux", "-L", "ch-claude_hub-feature"]
    assert cmd[3:] == ["ls"]
