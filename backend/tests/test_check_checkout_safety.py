"""Check index and commit content with pytest-owned Git repositories."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HOST = "${CLAUDE_HUB_HOST:-0.0.0.0}"


def startup(host: str = '"' + HOST + '"', comment: str = "") -> bytes:
    return (
        "#!/bin/sh\nexec env SERVE_FRONTEND=true uv run python -m "
        f"claude_hub.service_launcher --host {host} --port 8173{comment}\n"
    ).encode()


@pytest.fixture
def safety_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    checker = importlib.import_module("check_checkout_safety")
    assert checker.__file__ is not None
    assert Path(checker.__file__).resolve() == scripts / "check_checkout_safety.py"
    for name in tuple(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    for kind in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{kind}_NAME", "Checker Test")
        monkeypatch.setenv(f"GIT_{kind}_EMAIL", "checker@example.invalid")
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*arguments: str, data: bytes | None = None) -> bytes:
        return subprocess.run(
            ["git", "-C", str(repo), *arguments],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=10,
        ).stdout

    git("init", "--template=")

    def stage(path: str, content: bytes) -> None:
        destination = repo / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        git("add", "-f", "--", path)

    def commit() -> str:
        tree = git("write-tree").decode().strip()
        return git("commit-tree", tree, data=b"checker fixture\n").decode().strip()

    return SimpleNamespace(checker=checker, repo=repo, git=git, stage=stage, commit=commit)


@pytest.mark.parametrize(
    ("host", "comment", "allowed"),
    [
        ('"' + HOST + '"', "", True),
        ('"${CLAUDE_HUB_HOST:-::}"', "", False),
        (HOST, ' # --host "' + HOST + '"', False),
        ("'" + HOST + "'", ' # --host "' + HOST + '"', False),
        ('"' + HOST + '"', " # CLAUDE_HUB_HOST=:: ./start.sh is an explicit override", True),
    ],
)
def test_startup_contract_checks_real_quoted_argument_not_comment(
    safety_case: SimpleNamespace, host: str, comment: str, allowed: bool
) -> None:
    errors = safety_case.checker.startup_errors(startup(host, comment))
    assert (not errors) is allowed


def test_index_bytes_win_over_unstaged_worktree_changes(safety_case: SimpleNamespace) -> None:
    case = safety_case
    good, bad = startup(), startup('"${CLAUDE_HUB_HOST:-::}"')
    case.stage("start.sh", good)
    (case.repo / "start.sh").write_bytes(bad)
    label, errors = case.checker.check(case.repo, None)
    assert label == "index" and not errors
    case.stage("start.sh", bad)
    (case.repo / "start.sh").write_bytes(good)
    assert case.checker.check(case.repo, None)[1]


def test_untracked_local_yaml_is_allowed_but_forced_index_add_is_rejected(
    safety_case: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = safety_case
    case.stage("start.sh", startup())
    (case.repo / ".gitignore").write_text("/frontend/pnpm-workspace.yaml\n")
    local = case.repo / "frontend/pnpm-workspace.yaml"
    local.parent.mkdir()
    local.write_text("onlyBuiltDependencies: []\n")
    monkeypatch.setenv("CLAUDE_HUB_HOST", "::")
    assert not case.checker.check(case.repo, None)[1]
    case.git("add", "-f", "--", "frontend/pnpm-workspace.yaml")
    assert any("pnpm-workspace.yaml" in error for error in case.checker.check(case.repo, None)[1])
    assert local.read_text() == "onlyBuiltDependencies: []\n"


def test_commit_snapshot_is_independent_of_index_and_worktree(safety_case: SimpleNamespace) -> None:
    case = safety_case
    case.stage("start.sh", startup())
    good_commit = case.commit()
    case.git("update-ref", "HEAD", good_commit)
    case.stage("start.sh", startup('"${CLAUDE_HUB_HOST:-::}"'))
    bad_commit = case.commit()
    (case.repo / "start.sh").write_bytes(startup())
    label, errors = case.checker.check(case.repo, "HEAD")
    assert label == good_commit and not errors
    assert case.checker.check(case.repo, bad_commit)[1]
    assert case.checker.check(case.repo, None)[1]
    case.stage("start.sh", startup())
    case.stage("frontend/pnpm-workspace.yaml", b"packages: []\n")
    yaml_commit = case.commit()
    assert any(
        "pnpm-workspace.yaml" in error for error in case.checker.check(case.repo, yaml_commit)[1]
    )


def test_unmerged_protected_index_entry_fails_closed(safety_case: SimpleNamespace) -> None:
    case = safety_case
    object_id = case.git("hash-object", "-w", "--stdin", data=startup()).decode().strip()
    records = "".join(f"100644 {object_id} {stage}\tstart.sh\n" for stage in (1, 2, 3))
    case.git("update-index", "--index-info", data=records.encode())
    with pytest.raises(ValueError, match="unmerged"):
        case.checker.check(case.repo, None)


def test_other_startup_edits_and_line_continuation_remain_allowed(
    safety_case: SimpleNamespace,
) -> None:
    content = startup().replace(b"--port 8173", b"--port 9010")
    content = content.replace(b" --host ", b" \\\n    --host ")
    content += b"echo 'additional status text'\n"
    assert not safety_case.checker.startup_errors(content)


def test_cli_returns_failure_for_the_actual_staged_bytes(safety_case: SimpleNamespace) -> None:
    case = safety_case
    for contents, expected in ((startup(), 0), (startup('"${CLAUDE_HUB_HOST:-::}"'), 1)):
        case.stage("start.sh", contents)
        before = (case.repo / ".git/index").read_bytes()
        result = subprocess.run(
            [sys.executable, "-I", case.checker.__file__, "--repo", str(case.repo), "--staged"],
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == expected, result.stderr
        assert (case.repo / ".git/index").read_bytes() == before


def test_commit_replacement_cannot_hide_an_unsafe_snapshot(safety_case: SimpleNamespace) -> None:
    case = safety_case
    case.stage("start.sh", startup('"${CLAUDE_HUB_HOST:-::}"'))
    bad = case.commit()
    case.stage("start.sh", startup())
    good = case.commit()
    case.git("replace", bad, good)
    label, errors = case.checker.check(case.repo, bad)
    assert label == bad and errors


def test_cli_rejects_a_redirected_clean_index(
    safety_case: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = safety_case
    case.stage("start.sh", startup())
    alternate = tmp_path / "alternate-index"
    alternate.write_bytes((case.repo / ".git/index").read_bytes())
    case.stage("start.sh", startup('"${CLAUDE_HUB_HOST:-::}"'))
    before = (case.repo / ".git/index").read_bytes()
    alternate_before = alternate.read_bytes()
    monkeypatch.setenv("GIT_INDEX_FILE", str(alternate))
    result = subprocess.run(
        [sys.executable, "-I", case.checker.__file__, "--repo", str(case.repo), "--staged"],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0, result.stdout
    assert b"GIT_INDEX_FILE" in result.stderr
    assert (case.repo / ".git/index").read_bytes() == before
    assert alternate.read_bytes() == alternate_before


@pytest.mark.parametrize(
    "name",
    [
        "GIT_DIR",
        "GIT_COMMON_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_SHALLOW_FILE",
    ],
)
def test_snapshot_redirect_variables_require_explicit_removal(
    safety_case: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "synthetic-redirect")
    with pytest.raises(ValueError, match=name):
        safety_case.checker.check(safety_case.repo, None)
