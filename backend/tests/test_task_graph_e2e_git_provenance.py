"""Unit tests for Task Graph E2E git provenance contract."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HARNESS_DIR = _REPO_ROOT / "scripts" / "task-graph-e2e"
sys.path.insert(0, str(_HARNESS_DIR))

import git_provenance as gp  # noqa: E402


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "e2e@test"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "e2e"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("e2e\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def test_read_git_provenance_clean_repo(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    provenance = gp.read_git_provenance(tmp_path)
    assert provenance["dirty"] is False
    assert len(provenance["git_sha"]) == 40
    assert provenance["branch"] in {"main", "master"}
    assert provenance["source_root"] == str(tmp_path.resolve())


def test_require_clean_provenance_rejects_dirty(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("x", encoding="utf-8")
    provenance = gp.read_git_provenance(tmp_path)
    assert provenance["dirty"] is True
    with pytest.raises(RuntimeError, match="must be clean"):
        gp.require_clean_provenance(provenance)


def test_assert_provenance_unchanged_detects_sha_move() -> None:
    pre = {
        "branch": "feat/x",
        "git_sha": "a" * 40,
        "dirty": False,
        "source_root": "/tmp/repo",
    }
    post = dict(pre)
    post["git_sha"] = "b" * 40
    with pytest.raises(RuntimeError, match="git provenance moved"):
        gp.assert_provenance_unchanged(pre, post)


def test_finalize_git_provenance_evidence_flattens_pre_post() -> None:
    pre = {
        "branch": "feat/x",
        "git_sha": "c" * 40,
        "dirty": False,
        "source_root": "/tmp/repo",
    }
    post = dict(pre)
    payload = gp.finalize_git_provenance_evidence(pre, post)
    assert payload["git_provenance_unchanged"] is True
    assert payload["branch"] == "feat/x"
    assert payload["git_sha"] == "c" * 40
    assert payload["dirty"] is False
    for key in gp.REQUIRED_TOP_LEVEL_EVIDENCE_KEYS:
        assert key in payload


def test_forbid_git_provenance_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HUB_E2E_GIT_SHA", "deadbeef")
    with pytest.raises(RuntimeError, match="forbidden git provenance env"):
        gp.forbid_git_provenance_env_overrides()


def test_run_e2e_writes_git_provenance_in_finally() -> None:
    source = (_HARNESS_DIR / "run_e2e.py").read_text(encoding="utf-8")
    assert "git_provenance" in source
    assert "finalize_git_provenance_evidence" in source
    assert "forbid_git_provenance_env_overrides" in source
    assert "require_clean_provenance" in source
