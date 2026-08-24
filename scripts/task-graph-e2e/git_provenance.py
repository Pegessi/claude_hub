"""Git provenance helpers for Task Graph strict E2E exact-artifact gates."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

FORBIDDEN_GIT_ENV = (
    "CLAUDE_HUB_E2E_GIT_SHA",
    "CLAUDE_HUB_E2E_GIT_BRANCH",
    "CLAUDE_HUB_E2E_GIT_DIRTY",
)

REQUIRED_TOP_LEVEL_EVIDENCE_KEYS = (
    "branch",
    "git_sha",
    "dirty",
    "source_root",
    "git_provenance_pre",
    "git_provenance_post",
    "git_provenance_unchanged",
)


def delivery_source_root() -> Path:
    """Return the delivery repository root under test."""

    default = Path(__file__).resolve().parents[2]
    return Path(os.environ.get("CLAUDE_HUB_E2E_SOURCE_ROOT", str(default)))


def forbid_git_provenance_env_overrides() -> None:
    """Reject env-based git provenance spoofing."""

    present = [name for name in FORBIDDEN_GIT_ENV if os.environ.get(name)]
    if present:
        joined = ", ".join(present)
        raise RuntimeError(
            "forbidden git provenance env override(s): "
            f"{joined}. Harness must read real git state."
        )


def _git(source_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(source_root),
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def read_git_provenance(source_root: Path | None = None) -> dict[str, Any]:
    """Read branch, full HEAD SHA, and dirty flag from ``source_root``."""

    root = source_root or delivery_source_root()
    dirty_output = _git(root, "status", "--porcelain")
    return {
        "branch": _git(root, "branch", "--show-current"),
        "git_sha": _git(root, "rev-parse", "HEAD"),
        "dirty": bool(dirty_output),
        "source_root": str(root.resolve()),
    }


def require_clean_provenance(provenance: dict[str, Any]) -> None:
    """Fail closed when the delivery worktree is dirty."""

    if provenance.get("dirty"):
        raise RuntimeError(
            "delivery worktree must be clean before Task Graph E2E; "
            f"source_root={provenance.get('source_root')!r}"
        )


def assert_provenance_unchanged(pre: dict[str, Any], post: dict[str, Any]) -> None:
    """Ensure branch/SHA/dirty did not move during the harness run."""

    for key in ("branch", "git_sha", "dirty"):
        if pre.get(key) != post.get(key):
            raise RuntimeError(
                "git provenance moved during E2E: "
                f"{key} pre={pre.get(key)!r} post={post.get(key)!r}"
            )


def finalize_git_provenance_evidence(
    pre: dict[str, Any], post: dict[str, Any]
) -> dict[str, Any]:
    """Validate and flatten pre/post git provenance for evidence.json."""

    assert_provenance_unchanged(pre, post)
    return {
        "branch": pre["branch"],
        "git_sha": pre["git_sha"],
        "dirty": pre["dirty"],
        "source_root": pre["source_root"],
        "git_provenance_pre": pre,
        "git_provenance_post": post,
        "git_provenance_unchanged": True,
    }
