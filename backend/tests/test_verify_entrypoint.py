"""Exercise the shared check entry point without real tools or services."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

_FAKE_TOOL = r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

name, args = Path(sys.argv[0]).name, sys.argv[1:]
keys = (
    "HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
    "CLAUDE_HUB_HOME", "CLAUDE_HUB_STATE_ROOT", "CLAUDE_HUB_TMUX_SOCKET",
    "CLAUDE_HUB_PROVIDER_NETWORK_MODE", "CLAUDE_HUB_ALLOW_LIVE_RUNTIME",
    "CLAUDE_HUB_TOKEN", "CLAUDE_HUB_BASE_URL", "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_HOME", "TRAE_HOME",
    "CLAUDE_CONFIG_DIR", "COREPACK_ENABLE_NETWORK", "COREPACK_ENABLE_AUTO_PIN",
    "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "COVERAGE_FILE",
)
with open(os.environ["VERIFY_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"tool": name, "args": args, "cwd": os.getcwd(),
                             "env": {key: os.environ.get(key) for key in keys}}) + "\n")
operation = name
if name == "uv":
    if args == ["--version"]:
        print("uv " + os.environ["TEST_UV_VERSION"])
        raise SystemExit(0)
    if args[:3] != ["run", "--offline", "--no-sync"]:
        raise SystemExit(90)
    operation = args[3]
    if operation == "python":
        prefix = "Python " if "--version" in args else ""
        print(prefix + os.environ["TEST_PYTHON_VERSION"])
        raise SystemExit(0)
elif name == "node":
    if args == ["--version"]:
        print("v" + os.environ["TEST_NODE_VERSION"])
    elif args and args[0] == "-p":
        print(os.environ["DECLARED_PNPM_VERSION"])
    else:
        raise SystemExit(91)
    raise SystemExit(0)
elif name == "pnpm":
    if args == ["--version"]:
        print(os.environ["TEST_PNPM_VERSION"])
        raise SystemExit(0)
    if len(args) != 2 or args[0] != "run":
        raise SystemExit(92)
    operation = args[1]
elif name == "git":
    if len(args) != 4 or args[0] != "-C" or args[2:] != ["rev-parse", "HEAD"]:
        raise SystemExit(93)
    print("0123456789abcdef0123456789abcdef01234567")
    raise SystemExit(0)
print("fake " + operation + " output")
if os.environ.get("FAIL_CHECK") == operation:
    raise SystemExit(int(os.environ.get("FAIL_STATUS", "1")))
"""


@dataclass
class VerifyFixture:
    root: Path
    env: dict[str, str]
    log: Path
    artifacts: Path

    def calls(self, operation: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if self.log.exists():
            for line in self.log.read_text().splitlines():
                row = json.loads(line)
                assert isinstance(row, dict)
                rows.append(row)
        if operation is None:
            return rows
        return [
            row
            for row in rows
            if (
                row["tool"] == "uv"
                and row["args"][:4] == ["run", "--offline", "--no-sync", operation]
            )
            or (row["tool"] == "pnpm" and row["args"] == ["run", operation])
        ]

    def run(
        self, target: str, *arguments: str, **overrides: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.root / "scripts/verify.sh"), target, *arguments],
            cwd=self.root,
            env={**self.env, **overrides},
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )


def _executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def verify_fixture(tmp_path: Path) -> VerifyFixture:
    source = Path(__file__).resolve().parents[2]
    root, fake_bin = tmp_path / "fixture-repo", tmp_path / "fake-bin"
    artifacts, caller = tmp_path / "artifacts", tmp_path / "caller"
    log = tmp_path / "calls.jsonl"
    for directory in (
        root / "scripts",
        root / "backend/.venv/bin",
        root / "frontend/node_modules",
        fake_bin,
        artifacts,
    ):
        directory.mkdir(parents=True)
    _executable(root / "scripts/verify.sh", (source / "scripts/verify.sh").read_text())
    _executable(root / "backend/.venv/bin/python", "#!/bin/sh\nexit 0\n")
    for tool in ("uv", "node", "pnpm", "git"):
        _executable(fake_bin / tool, _FAKE_TOOL)
    for filename, version in (
        (".uv-version", "1.2.3"),
        (".python-test-version", "3.11.9"),
        (".node-test-version", "24.21.0"),
    ):
        (root / filename).write_text(version + "\n")
    for filename in ("AGENTS.md", "CLAUDE.md"):
        (root / filename).write_text("same\n")
    (root / "frontend/package.json").write_text(json.dumps({"packageManager": "pnpm@9.9.9"}))
    # Never copy caller credentials into the recording tools' environment.
    env = {
        "PATH": f"{fake_bin}{os.pathsep}{os.defpath}",
        "LANG": "C.UTF-8",
        "VERIFY_LOG": str(log),
        "TEST_UV_VERSION": "1.2.3",
        "TEST_PYTHON_VERSION": "3.11.9",
        "TEST_NODE_VERSION": "24.21.0",
        "TEST_PNPM_VERSION": "9.9.9",
        "DECLARED_PNPM_VERSION": "9.9.9",
        "CLAUDE_HUB_VERIFY_ARTIFACT_ROOT": str(artifacts),
        "CLAUDE_HUB_PROVIDER_NETWORK_MODE": "caller-fixture-mode",
    }
    for key in (
        "HOME",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "CLAUDE_HUB_HOME",
        "CLAUDE_HUB_STATE_ROOT",
    ):
        path = caller / key.lower()
        path.mkdir(parents=True)
        env[key] = str(path)
    return VerifyFixture(root, env, log, artifacts)


def test_all_continues_after_failure(verify_fixture: VerifyFixture) -> None:
    result = verify_fixture.run("all", FAIL_CHECK="mypy")
    assert result.returncode != 0
    output = result.stdout + result.stderr
    for target in (
        "docs",
        "backend-format",
        "backend-types",
        "backend-tests",
        "frontend-lint",
        "frontend-types",
        "frontend-tests",
        "frontend-build",
    ):
        assert f"==> {target}" in output
    assert "<== FAIL backend-types" in output
    assert "<== PASS backend-tests" in output
    assert "<== PASS frontend-build" in output
    for operation in (
        "black",
        "isort",
        "mypy",
        "pytest",
        "lint:check",
        "typecheck",
        "test:unit",
        "build:bundle",
    ):
        assert len(verify_fixture.calls(operation)) == 1


def test_types_include_tests(verify_fixture: VerifyFixture) -> None:
    result = verify_fixture.run("backend-types")
    assert result.returncode == 0, result.stderr
    calls = verify_fixture.calls("mypy")
    assert len(calls) == 1
    assert calls[0]["args"] == ["run", "--offline", "--no-sync", "mypy", "."]
    assert Path(calls[0]["cwd"]) == verify_fixture.root / "backend"


@pytest.mark.parametrize(
    ("target", "key", "operation"),
    [
        ("backend-types", "TEST_UV_VERSION", "mypy"),
        ("backend-types", "TEST_PYTHON_VERSION", "mypy"),
        ("frontend-lint", "TEST_NODE_VERSION", "lint:check"),
        ("frontend-lint", "TEST_PNPM_VERSION", "lint:check"),
    ],
)
def test_mismatched_tools_do_not_run_checks(
    verify_fixture: VerifyFixture, target: str, key: str, operation: str
) -> None:
    result = verify_fixture.run(target, **{key: "0.0.0"})
    assert result.returncode != 0
    assert not verify_fixture.calls(operation)


def test_runtime_isolation_and_failure_evidence(verify_fixture: VerifyFixture) -> None:
    synthetic = {
        key: "synthetic-value"
        for key in (
            "CODEX_HOME",
            "TRAE_HOME",
            "CLAUDE_CONFIG_DIR",
            "CLAUDE_HUB_ALLOW_LIVE_RUNTIME",
            "CLAUDE_HUB_TOKEN",
            "CLAUDE_HUB_BASE_URL",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "PYTEST_ADDOPTS",
            "PYTEST_PLUGINS",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        )
    }
    result = verify_fixture.run("backend-tests", FAIL_CHECK="pytest", FAIL_STATUS="7", **synthetic)
    assert result.returncode == 7
    evidence_dirs = list(verify_fixture.artifacts.iterdir())
    assert len(evidence_dirs) == 1
    evidence = evidence_dirs[0]
    assert (evidence / "exit-code").read_text().strip() == "7"
    assert "fake pytest output" in (evidence / "pytest.log").read_text()
    assert "scope=full" in (evidence / "environment.txt").read_text()
    assert str(evidence) in result.stdout
    calls = verify_fixture.calls("pytest")
    assert len(calls) == 1
    assert calls[0]["args"] == [
        "run",
        "--offline",
        "--no-sync",
        "pytest",
        "-xvs",
        "--ignore=tests/test_terminal_replay.py",
        "--ignore=tests/test_terminal_input_latency_perf.py",
        "--cov=claude_hub",
        "--cov-report=xml:coverage.xml",
        "--cov-report=term-missing",
    ]
    environment = calls[0]["env"]
    for key, directory in (
        ("HOME", "home"),
        ("TMPDIR", "tmp"),
        ("XDG_CONFIG_HOME", "xdg-config"),
        ("XDG_STATE_HOME", "xdg-state"),
        ("XDG_CACHE_HOME", "xdg-cache"),
        ("CLAUDE_HUB_HOME", "hub"),
        ("CLAUDE_HUB_STATE_ROOT", "state"),
    ):
        assert Path(environment[key]) == evidence / directory
    assert environment["CLAUDE_HUB_TMUX_SOCKET"].startswith("ch-verify-")
    # pytest's conftest owns the provider-network default, not the shell wrapper.
    assert environment["CLAUDE_HUB_PROVIDER_NETWORK_MODE"] == "caller-fixture-mode"
    for key in synthetic:
        assert environment[key] is None
    assert Path(environment["COVERAGE_FILE"]) == evidence / ".coverage"
    assert environment["COREPACK_ENABLE_NETWORK"] == "0"
    assert environment["COREPACK_ENABLE_AUTO_PIN"] == "0"


def test_external_backend_is_rejected(verify_fixture: VerifyFixture) -> None:
    result = verify_fixture.run(
        "backend-tests", CLAUDE_HUB_TEST_BACKEND_URL="http://127.0.0.1:8173"
    )
    assert result.returncode != 0
    assert "must not use an external backend" in result.stderr
    assert not verify_fixture.calls("pytest")
    assert not list(verify_fixture.artifacts.iterdir())


def test_docs_need_no_language_tools(verify_fixture: VerifyFixture) -> None:
    result = verify_fixture.run("docs", TEST_UV_VERSION="wrong", TEST_NODE_VERSION="wrong")
    assert result.returncode == 0, result.stderr
    assert verify_fixture.calls() == []


def test_verification_pins_do_not_auto_select_service_runtimes() -> None:
    root = Path(__file__).resolve().parents[2]
    assert (root / ".python-test-version").is_file()
    assert (root / ".node-test-version").is_file()
    assert not (root / ".python-version").exists()
    assert not (root / ".node-version").exists()


def test_focused_scope_preserves_selection_and_private_environment(
    verify_fixture: VerifyFixture,
) -> None:
    result = verify_fixture.run("backend-tests", "--", "tests/test_demo.py::test_one", "-q")
    assert result.returncode == 0, result.stderr
    calls = verify_fixture.calls("pytest")
    assert len(calls) == 1
    assert calls[0]["args"] == [
        "run",
        "--offline",
        "--no-sync",
        "pytest",
        "tests/test_demo.py::test_one",
        "-q",
    ]
    evidence_dirs = list(verify_fixture.artifacts.iterdir())
    assert len(evidence_dirs) == 1
    evidence = evidence_dirs[0]
    assert Path(calls[0]["env"]["HOME"]) == evidence / "home"
    metadata = (evidence / "environment.txt").read_text()
    assert "scope=focused" in metadata
    assert "pytest_arg=tests/test_demo.py::test_one" in metadata
    assert "pytest_arg=-q" in metadata
    assert (evidence / "exit-code").read_text().strip() == "0"


@pytest.mark.parametrize(
    "arguments",
    [
        ("all", "--", "x"),
        ("backend-types", "--", "x"),
        ("backend-tests", "x"),
        ("backend-tests", "--"),
    ],
)
def test_scope_arguments_are_rejected_outside_explicit_focused_mode(
    verify_fixture: VerifyFixture, arguments: tuple[str, ...]
) -> None:
    result = verify_fixture.run(arguments[0], *arguments[1:])
    assert result.returncode != 0
    assert verify_fixture.calls() == []
