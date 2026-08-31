"""Worktree backends must not resolve to the live Hub home or default tmux."""

from __future__ import annotations

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


def test_worktree_tmux_command_uses_named_socket(tmp_path: Path) -> None:
    repo = _linked_worktree(tmp_path)
    cmd = tmux_command("ls", repo_root=repo, environ={})
    assert cmd[:3] == ["tmux", "-L", "ch-claude_hub-feature"]
    assert cmd[3:] == ["ls"]
