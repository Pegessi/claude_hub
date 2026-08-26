"""Tests for workspace identity resolution."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from claude_hub.models import ExecutionTarget
from claude_hub.services.workspace_identity import (
    DuplicateWorkspaceError,
    canonical_workspace_path,
    git_repo_identity,
    local_repo_key,
    same_local_repo,
    workspace_identity_for_fields,
)


def test_git_repo_identity_main_and_worktree_share_common_dir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e.com",
        },
    )
    wt = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", str(wt), "-b", "feat/wt"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    main_id = git_repo_identity(repo)
    wt_id = git_repo_identity(wt)
    assert main_id.is_git and wt_id.is_git
    assert main_id.common_dir == wt_id.common_dir
    assert same_local_repo(repo, wt)
    assert canonical_workspace_path(str(wt)) == str(repo.resolve())
    assert canonical_workspace_path(str(repo)) == str(repo.resolve())


def test_symlink_resolves_to_same_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    assert same_local_repo(repo, link)


def test_remote_identity_distinguishes_profiles() -> None:
    a = workspace_identity_for_fields(
        path="/Users/me/proj",
        target=ExecutionTarget.REMOTE,
        remote_profile_id="profile-a",
        remote_cwd=None,
    )
    b = workspace_identity_for_fields(
        path="/Users/me/proj",
        target=ExecutionTarget.REMOTE,
        remote_profile_id="profile-b",
        remote_cwd=None,
    )
    assert a.key != b.key


def test_remote_identity_distinguishes_remote_cwd() -> None:
    h20 = workspace_identity_for_fields(
        path="/Users/me/codex_workspace",
        target=ExecutionTarget.REMOTE,
        remote_profile_id="h20",
        remote_cwd="~/evals",
    )
    ff45d = workspace_identity_for_fields(
        path="/Users/me/codex_workspace",
        target=ExecutionTarget.REMOTE,
        remote_profile_id="h20",
        remote_cwd="~/ff45d",
    )
    assert h20.key != ff45d.key


def test_normalize_remote_cwd_posix() -> None:
    from claude_hub.services.workspace_identity import normalize_remote_cwd

    assert normalize_remote_cwd(None) == "~"
    assert normalize_remote_cwd("~/foo/../bar") == "~/bar"
    assert normalize_remote_cwd("/var/tmp//x/./y") == "/var/tmp/x/y"
    assert normalize_remote_cwd("  /a/b/  ") == "/a/b"
    assert normalize_remote_cwd("~/evals/") == "~/evals"
    assert normalize_remote_cwd("~//evals//ff45d/") == "~/evals/ff45d"
    assert normalize_remote_cwd("/a/b") == normalize_remote_cwd("/a/b/")
    assert normalize_remote_cwd("~h20") == "~h20"
    assert normalize_remote_cwd("~h20/evals") == "~h20/evals"
    assert normalize_remote_cwd("~h20/..") == "~h20/.."
    assert normalize_remote_cwd("~other") == "~other"
    assert normalize_remote_cwd("~h20/ff45d") != "~"
    # Preserve leading .. that escape ~ / ~user (distinct remote targets).
    assert normalize_remote_cwd("~/../other") == "~/../other"
    assert normalize_remote_cwd("~/a/../../other") == "~/../other"
    assert normalize_remote_cwd("~alice/../other") == "~alice/../other"


def test_effective_local_agent_cwd_distinguishes_linked_worktree(tmp_path: Path) -> None:
    from claude_hub.services.workspace_identity import effective_local_agent_cwd

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e.com",
        },
    )
    wt = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", str(wt), "-b", "feat/wt"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    assert same_local_repo(repo, wt)
    assert canonical_workspace_path(str(wt)) == canonical_workspace_path(str(repo))
    assert effective_local_agent_cwd(repo) != effective_local_agent_cwd(wt)


def test_non_git_path_uses_absolute_path(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    key = local_repo_key(plain)
    assert key == str(plain.resolve())


def test_validate_local_agent_cwd_git_repo_rules(tmp_path: Path) -> None:
    from claude_hub.services.workspace_identity import (
        InvalidLocalAgentCwdError,
        validate_local_agent_cwd_for_workspace,
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e.com",
        },
    )
    wt = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", str(wt), "-b", "feat/wt"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    repo_b = tmp_path / "repo-b"
    repo_b.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_b, check=True, capture_output=True)
    (repo_b / "README.md").write_text("other\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo_b, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo_b,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e.com",
        },
    )

    assert validate_local_agent_cwd_for_workspace(repo, wt) == str(wt.resolve())
    with pytest.raises(InvalidLocalAgentCwdError, match="different Git repository"):
        validate_local_agent_cwd_for_workspace(repo, repo_b)


def test_validate_local_agent_cwd_non_git_descendant(tmp_path: Path) -> None:
    from claude_hub.services.workspace_identity import (
        InvalidLocalAgentCwdError,
        validate_local_agent_cwd_for_workspace,
    )

    plain = tmp_path / "plain"
    plain.mkdir()
    sub = plain / "sub"
    sub.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    assert validate_local_agent_cwd_for_workspace(plain, plain) == str(plain.resolve())
    assert validate_local_agent_cwd_for_workspace(plain, sub) == str(sub.resolve())
    with pytest.raises(InvalidLocalAgentCwdError, match="subdirectory"):
        validate_local_agent_cwd_for_workspace(plain, outside)
