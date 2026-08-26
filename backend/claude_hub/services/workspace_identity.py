"""Canonical workspace identity for local Git repos and remote targets."""

from __future__ import annotations

import posixpath
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..models import ExecutionTarget, Workspace


@dataclass(frozen=True)
class GitRepoIdentity:
    """Resolved Git repository identity for a path."""

    common_dir: str
    primary_worktree: str
    is_git: bool


@dataclass(frozen=True)
class WorkspaceIdentity:
    """Composite identity key for workspace deduplication."""

    local_repo_key: str
    target: ExecutionTarget
    remote_profile_id: Optional[str]
    remote_cwd: Optional[str]

    @property
    def key(self) -> str:
        if self.target == ExecutionTarget.REMOTE:
            profile = self.remote_profile_id or ""
            remote_cwd = normalize_remote_cwd(self.remote_cwd)
            return f"remote:{self.local_repo_key}:{profile}:{remote_cwd}"
        return f"local:{self.local_repo_key}"


class WorkspaceIdentityError(ValueError):
    """Base error for workspace identity resolution."""


class DuplicateWorkspaceError(WorkspaceIdentityError):
    """Raised when create would duplicate an existing workspace identity."""

    def __init__(self, identity_key: str, candidates: list[Workspace]) -> None:
        self.identity_key = identity_key
        self.candidates = candidates
        ids = ", ".join(f"{ws.id} ({ws.name})" for ws in candidates)
        super().__init__(
            f"Workspace identity already exists ({identity_key}). "
            f"Existing: {ids}. Use workspace ensure to reuse, or pass allow_duplicate=true "
            f"to create an intentional duplicate."
        )


class AmbiguousWorkspaceError(WorkspaceIdentityError):
    """Raised when multiple workspaces match and no deterministic winner exists."""

    def __init__(self, identity_key: str, candidates: list[Workspace]) -> None:
        self.identity_key = identity_key
        self.candidates = candidates
        ids = ", ".join(f"{ws.id} ({ws.name}, path={ws.path})" for ws in candidates)
        super().__init__(
            f"Ambiguous workspace identity ({identity_key}). "
            f"Multiple matches with no primary-worktree preference: {ids}. "
            f"Resolve duplicates manually or pass allow_duplicate on create."
        )


class InvalidLocalAgentCwdError(WorkspaceIdentityError):
    """Raised when a local agent cwd is outside the workspace repo/path boundary."""


def resolve_path(path: str | Path) -> Path:
    """Expand user, resolve symlinks, and normalize to an absolute path."""
    return Path(path).expanduser().resolve()


def reject_relative_local_path(path: str, *, field: str = "path") -> None:
    """Fail closed when API callers pass relative local paths (CLI must resolve first)."""
    if not Path(path).expanduser().is_absolute():
        raise WorkspaceIdentityError(
            f"{field} must be an absolute local path; resolve relative paths in the CLI before POST"
        )


def _parse_worktree_paths(porcelain: str) -> list[str]:
    paths: list[str] = []
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            paths.append(line.split(" ", 1)[1])
    return paths


def _resolve_git_dir(path: str | Path) -> Path:
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
        check=True,
    )
    git_dir_raw = proc.stdout.strip()
    if not git_dir_raw:
        raise subprocess.CalledProcessError(1, "git rev-parse --git-dir")
    git_dir = Path(git_dir_raw)
    if not git_dir.is_absolute():
        git_dir = (Path(path) / git_dir).resolve()
    return git_dir.resolve()


def _primary_worktree_for_common_dir(
    *,
    resolved: Path,
    common_path: Path,
) -> str:
    """Return the main worktree path whose git-dir equals ``common_path``."""
    common_resolved = common_path.resolve()
    wt_proc = subprocess.run(
        ["git", "-C", str(resolved), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    for worktree_path in _parse_worktree_paths(wt_proc.stdout):
        if _resolve_git_dir(worktree_path) == common_resolved:
            return str(resolve_path(worktree_path))
    if common_resolved.name == ".git":
        return str(common_resolved.parent.resolve())
    return str(resolved)


def _normalize_tilde_rest(rest: str) -> str:
    """Normalize path segments under ~ while preserving leading .. that escape home."""
    if not rest or rest == ".":
        return ""
    stack: list[str] = []
    leading_dots: list[str] = []
    for part in rest.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if stack:
                stack.pop()
            else:
                leading_dots.append("..")
            continue
        stack.append(part)
    segments = leading_dots + stack
    if not segments:
        return ""
    return "/".join(segments)


def normalize_remote_cwd(remote_cwd: Optional[str]) -> str:
    """Normalize a remote cwd for identity comparison (POSIX semantics)."""
    if remote_cwd is None:
        return "~"
    value = remote_cwd.strip()
    if not value or value == "~":
        return "~"
    if not value.startswith("~"):
        normalized = posixpath.normpath(value)
        return normalized or "."

    if value in ("~/", "~/.", "~"):
        return "~"

    if value.startswith("~/"):
        rest = value[2:]
        normalized_rest = _normalize_tilde_rest(rest)
        if not normalized_rest:
            return "~"
        return f"~/{normalized_rest}"

    # ~user or ~user/path — preserve anchor; never collapse to bare ~.
    suffix = value[1:]
    slash = suffix.find("/")
    if slash == -1:
        anchor = suffix
        rest = ""
    else:
        anchor = suffix[:slash]
        rest = suffix[slash + 1 :]

    if not anchor or anchor in (".", ".."):
        return normalize_remote_cwd(f"~/{suffix}")

    if not rest or rest == ".":
        return f"~{anchor}"

    normalized_rest = _normalize_tilde_rest(rest)
    if not normalized_rest:
        return f"~{anchor}"
    return f"~{anchor}/{normalized_rest}"


def effective_local_agent_cwd(path: str | Path) -> str:
    """Resolved absolute cwd for strict local agent reuse (not primary-worktree canonicalization)."""
    return str(resolve_path(path))


def _git_identity_for_path_or_ancestor(path: str | Path) -> Optional[GitRepoIdentity]:
    """Return git identity for ``path``, walking up through non-existent suffixes."""
    raw = Path(path).expanduser()
    try:
        resolved = raw.resolve(strict=False)
    except OSError:
        resolved = raw.absolute()

    for candidate in (resolved, *resolved.parents):
        if not candidate.exists():
            continue
        identity = git_repo_identity(candidate)
        if identity.is_git:
            return identity
    return None


def local_agent_cwd_allowed_for_workspace(
    workspace_path: str | Path,
    cwd: str | Path,
) -> bool:
    """True when ``cwd`` is a valid local agent cwd for ``workspace_path``."""
    try:
        validate_local_agent_cwd_for_workspace(workspace_path, cwd)
        return True
    except InvalidLocalAgentCwdError:
        return False


def validate_local_agent_cwd_for_workspace(
    workspace_path: str | Path,
    cwd: str | Path,
) -> str:
    """Validate local agent cwd and return the effective absolute path."""
    reject_relative_local_path(str(cwd), field="cwd")
    ws_path = resolve_path(workspace_path)
    effective = effective_local_agent_cwd(cwd)
    effective_path = Path(effective)
    ws_identity = git_repo_identity(ws_path)

    if ws_identity.is_git:
        cwd_identity = _git_identity_for_path_or_ancestor(cwd)
        if cwd_identity is None or not cwd_identity.is_git:
            raise InvalidLocalAgentCwdError(
                "Local agent cwd must be inside the workspace Git repository "
                f"({ws_identity.primary_worktree}). From a feature worktree run "
                "agent create with --cwd ."
            )
        if cwd_identity.common_dir != ws_identity.common_dir:
            raise InvalidLocalAgentCwdError(
                "Local agent cwd belongs to a different Git repository than this "
                f"workspace ({ws_identity.primary_worktree}). Run workspace ensure "
                "for that repository first."
            )
        return effective

    if effective_path != ws_path and ws_path not in effective_path.parents:
        raise InvalidLocalAgentCwdError(
            f"Local agent cwd must be the workspace path or a subdirectory ({ws_path})."
        )
    return effective


def git_repo_identity(path: str | Path) -> GitRepoIdentity:
    """Return canonical git common-dir identity for ``path``, or path-only fallback."""
    resolved = resolve_path(path)
    if not resolved.exists():
        fallback = str(resolved)
        return GitRepoIdentity(common_dir=fallback, primary_worktree=fallback, is_git=False)

    try:
        common_proc = subprocess.run(
            ["git", "-C", str(resolved), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        )
        common_raw = common_proc.stdout.strip()
        if not common_raw:
            raise subprocess.CalledProcessError(1, "git rev-parse --git-common-dir")
        common_path = Path(common_raw)
        if not common_path.is_absolute():
            common_path = (resolved / common_path).resolve()

        primary = _primary_worktree_for_common_dir(
            resolved=resolved,
            common_path=common_path,
        )

        return GitRepoIdentity(
            common_dir=str(common_path),
            primary_worktree=primary,
            is_git=True,
        )
    except (subprocess.CalledProcessError, OSError):
        fallback = str(resolved)
        return GitRepoIdentity(common_dir=fallback, primary_worktree=fallback, is_git=False)


def local_repo_key(path: str | Path) -> str:
    return git_repo_identity(path).common_dir


def same_local_repo(path_a: str | Path, path_b: str | Path) -> bool:
    return local_repo_key(path_a) == local_repo_key(path_b)


def workspace_identity_for_fields(
    *,
    path: str,
    target: ExecutionTarget,
    remote_profile_id: Optional[str] = None,
    remote_cwd: Optional[str] = None,
) -> WorkspaceIdentity:
    repo = git_repo_identity(path)
    if target == ExecutionTarget.REMOTE and not remote_profile_id:
        raise WorkspaceIdentityError("Remote workspace requires remote_profile_id")
    return WorkspaceIdentity(
        local_repo_key=repo.common_dir,
        target=target,
        remote_profile_id=remote_profile_id,
        remote_cwd=normalize_remote_cwd(remote_cwd) if target == ExecutionTarget.REMOTE else None,
    )


def workspace_identity(workspace: Workspace) -> WorkspaceIdentity:
    return workspace_identity_for_fields(
        path=workspace.path,
        target=workspace.target,
        remote_profile_id=workspace.remote_profile_id,
        remote_cwd=workspace.remote_cwd,
    )


def canonical_workspace_path(path: str) -> str:
    """Normalize a workspace path; prefer primary worktree for git repos."""
    repo = git_repo_identity(path)
    return repo.primary_worktree if repo.is_git else str(resolve_path(path))


def default_workspace_create_name(path: str | Path) -> str:
    """Stable create-time workspace name from canonical primary repo basename."""
    return Path(canonical_workspace_path(str(path))).name or "workspace"


def select_workspace_candidate(
    candidates: list[Workspace],
    *,
    requested_path: str,
) -> Workspace:
    """Pick one workspace deterministically; raise on ambiguity."""
    if not candidates:
        raise KeyError("no workspace candidates")
    if len(candidates) == 1:
        return candidates[0]

    repo = git_repo_identity(requested_path)
    primary_matches = [
        ws for ws in candidates if resolve_path(ws.path) == resolve_path(repo.primary_worktree)
    ]
    if len(primary_matches) == 1:
        return primary_matches[0]
    if len(primary_matches) > 1:
        ordered = sorted(primary_matches, key=lambda ws: (ws.created_at, ws.id))
        return ordered[0]

    ordered = sorted(candidates, key=lambda ws: (ws.created_at, ws.id))
    if len({ws.path for ws in ordered}) > 1 and repo.is_git:
        raise AmbiguousWorkspaceError(
            workspace_identity(ordered[0]).key,
            ordered,
        )
    return ordered[0]
