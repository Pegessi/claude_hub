"""Isolate worktree/dev backends from the live Hub runtime.

Official backend on the primary checkout keeps writing
``~/.claude_hub/workspaces`` and the default tmux server. A linked git
worktree (``.git`` is a file) defaults to its own runtime home and tmux
socket so feature-branch verification cannot rewrite live ``state.json``
or rename live panes.

A worktree process that would otherwise resolve to the live home, live
workspaces, or the default tmux server raises instead of silently sharing
them. Tests pass an isolated ``repo_root`` / ``environ``.

Env overrides (resolved on every call so tests can monkeypatch):

- ``CLAUDE_HUB_HOME`` — runtime home (workspaces/tabs/launch_env)
- ``CLAUDE_HUB_STATE_ROOT`` — workspace state root only
- ``CLAUDE_HUB_TMUX_SOCKET`` — tmux ``-L`` name
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping

CLAUDE_HUB_HOME_ENV = "CLAUDE_HUB_HOME"
CLAUDE_HUB_STATE_ROOT_ENV = "CLAUDE_HUB_STATE_ROOT"
CLAUDE_HUB_TMUX_SOCKET_ENV = "CLAUDE_HUB_TMUX_SOCKET"
_ALLOW_LIVE_ENV = "CLAUDE_HUB_ALLOW_LIVE_RUNTIME"

_DEFAULT_HOME = Path.home() / ".claude_hub"
_LIVE_WORKSPACES = _DEFAULT_HOME / "workspaces"
_SOCKET_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _resolved(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser()


def detect_linked_worktree_slug(repo_root: Path) -> str | None:
    """Return the checkout directory name when ``repo_root`` is a linked worktree."""

    git_path = repo_root / ".git"
    if git_path.is_file():
        slug = repo_root.name.strip()
        return slug or None
    return None


def infer_package_repo_root() -> Path:
    """Repo root that contains this installed/checked-out ``backend/`` package."""

    return Path(__file__).resolve().parents[3]


def _refuse_live_path(
    path: Path,
    *,
    repo_root: Path,
    environ: Mapping[str, str],
    kind: str,
) -> Path:
    if detect_linked_worktree_slug(repo_root) is None:
        return path
    if environ.get(_ALLOW_LIVE_ENV) == "1":
        return path
    resolved = _resolved(path)
    forbidden = {_resolved(_DEFAULT_HOME), _resolved(_LIVE_WORKSPACES)}
    if resolved in forbidden:
        raise RuntimeError(
            f"refusing live {kind} {path} from linked worktree {repo_root}; "
            "set CLAUDE_HUB_HOME or CLAUDE_HUB_STATE_ROOT to an isolated path"
        )
    return path


def resolve_runtime_home(
    *,
    repo_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    root = infer_package_repo_root() if repo_root is None else repo_root
    raw_home = env.get(CLAUDE_HUB_HOME_ENV)
    if raw_home:
        return _refuse_live_path(
            Path(raw_home).expanduser(),
            repo_root=root,
            environ=env,
            kind="runtime home",
        )
    slug = detect_linked_worktree_slug(root)
    if slug:
        return _DEFAULT_HOME / "worktrees" / slug
    return _DEFAULT_HOME


def resolve_state_root(
    *,
    repo_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    root = infer_package_repo_root() if repo_root is None else repo_root
    raw_state = env.get(CLAUDE_HUB_STATE_ROOT_ENV)
    if raw_state:
        return _refuse_live_path(
            Path(raw_state).expanduser(),
            repo_root=root,
            environ=env,
            kind="STATE_ROOT",
        )
    return resolve_runtime_home(repo_root=root, environ=env) / "workspaces"


def resolve_tmux_socket_name(
    *,
    repo_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    env = os.environ if environ is None else environ
    root = infer_package_repo_root() if repo_root is None else repo_root
    if CLAUDE_HUB_TMUX_SOCKET_ENV in env:
        raw = env[CLAUDE_HUB_TMUX_SOCKET_ENV].strip()
        if raw:
            return raw
        if detect_linked_worktree_slug(root) is not None and env.get(_ALLOW_LIVE_ENV) != "1":
            raise RuntimeError(
                "refusing default tmux server from linked worktree; "
                "unset CLAUDE_HUB_TMUX_SOCKET or set it to an isolated socket name"
            )
        return None
    slug = detect_linked_worktree_slug(root)
    if not slug:
        return None
    safe = _SOCKET_SAFE_RE.sub("-", slug).strip("-")
    return f"ch-{safe}" if safe else None


def tmux_socket_args(
    *,
    repo_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    name = resolve_tmux_socket_name(repo_root=repo_root, environ=environ)
    if name:
        return ["-L", name]
    return []


def tmux_command(
    *args: str,
    repo_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    return ["tmux", *tmux_socket_args(repo_root=repo_root, environ=environ), *args]
