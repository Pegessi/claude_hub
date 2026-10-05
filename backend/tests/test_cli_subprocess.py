"""End-to-end CLI process tests against an isolated HTTP boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import click
import pytest
from click.testing import CliRunner

from claude_hub.cli.main import _MachineReadableGroup, cli


class _MockHubServer(ThreadingHTTPServer):
    daemon_threads = True


@contextmanager
def _mock_hub(
    responder: Callable[[BaseHTTPRequestHandler], tuple[int, Any]],
) -> Iterator[tuple[str, list[tuple[str, str]]]]:
    requests: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def _respond(self) -> None:
            requests.append((self.command, self.path))
            status, body = responder(self)
            self.send_response(status)
            if body is not None:
                encoded = json.dumps(body).encode()
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(encoded)))
            else:
                encoded = b""
            self.end_headers()
            if encoded:
                try:
                    self.wfile.write(encoded)
                except BrokenPipeError:
                    pass

        do_DELETE = _respond
        do_GET = _respond
        do_POST = _respond

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = _MockHubServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    config = tmp_path / "config" / "config.toml"
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    for path in (home, config.parent, runtime, state):
        path.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for key in ("CLAUDE_HUB_TOKEN", "CLAUDE_HUB_URL"):
        env.pop(key, None)
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config.parent),
            "XDG_RUNTIME_DIR": str(runtime),
            "CLAUDE_HUB_CONFIG": str(config),
            "CLAUDE_HUB_HOME": str(runtime / "hub-home"),
            "CLAUDE_HUB_STATE_ROOT": str(state),
            "CLAUDE_HUB_TMUX_SOCKET": "agent-cli-test",
        }
    )
    return env


def _cli_argv(entrypoint: str) -> list[str]:
    if entrypoint == "module":
        return [sys.executable, "-m", "claude_hub.cli"]
    if entrypoint == "console":
        return [str(Path(sys.executable).with_name("claude-hub"))]
    raise ValueError(f"unsupported entrypoint: {entrypoint}")


def _run_cli(
    tmp_path: Path,
    base_url: str,
    *args: str,
    input_text: Optional[str] = None,
    timeout: float = 15,
    entrypoint: str = "module",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            *_cli_argv(entrypoint),
            "--base-url",
            base_url,
            *args,
        ],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=_isolated_env(tmp_path),
    )


@pytest.mark.parametrize("entrypoint", ["module", "console"])
def test_explicit_context_exit_is_preserved(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    def responder(_: BaseHTTPRequestHandler) -> tuple[int, Any]:
        pytest.fail("parse-action must not make an HTTP request")

    with _mock_hub(responder) as (base_url, requests):
        result = _run_cli(
            tmp_path,
            base_url,
            "--json",
            "feishu",
            "parse-action",
            "{}",
            entrypoint=entrypoint,
        )

    assert result.returncode == 1
    assert result.stdout == "null\n"
    assert result.stderr == ""
    assert requests == []


def test_cli_runner_preserves_explicit_context_exit() -> None:
    result = CliRunner().invoke(cli, ["--json", "feishu", "parse-action", "{}"])

    assert result.exit_code == 1
    assert result.stdout == "null\n"
    assert result.stderr == ""


def test_integer_command_return_is_not_treated_as_an_exit_code() -> None:
    @click.group(cls=_MachineReadableGroup)
    @click.option("--json/--no-json", "json_output", default=False)
    def local_cli(json_output: bool) -> None:
        del json_output

    @local_cli.command("return-seven")
    def return_seven() -> int:
        return 7

    result = CliRunner().invoke(local_cli, ["--json", "return-seven"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize("entrypoint", ["module", "console"])
def test_json_success_stdout_is_single_document(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    def responder(_: BaseHTTPRequestHandler) -> tuple[int, Any]:
        return 200, []

    with _mock_hub(responder) as (base_url, requests):
        result = _run_cli(
            tmp_path,
            base_url,
            "--json",
            "workspace",
            "list",
            entrypoint=entrypoint,
        )

    assert result.returncode == 0
    assert json.loads(result.stdout) == []
    assert result.stderr == ""
    assert requests == [("GET", "/api/workspaces")]


@pytest.mark.parametrize("entrypoint", ["module", "console"])
def test_json_help_exits_zero(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    def responder(_: BaseHTTPRequestHandler) -> tuple[int, Any]:
        pytest.fail("help must not make an HTTP request")

    with _mock_hub(responder) as (base_url, requests):
        result = _run_cli(
            tmp_path,
            base_url,
            "--json",
            "--help",
            entrypoint=entrypoint,
        )

    assert result.returncode == 0
    assert result.stdout.startswith("Usage:")
    assert result.stderr == ""
    assert requests == []


def test_root_json_option_parser_ignores_subcommand_option_values(tmp_path: Path) -> None:
    def responder(_: BaseHTTPRequestHandler) -> tuple[int, Optional[dict[str, Any]]]:
        return 404, {"detail": "Task not found"}

    with _mock_hub(responder) as (base_url, requests):
        json_result = _run_cli(
            tmp_path,
            base_url,
            "--json",
            "task",
            "abort",
            "task-1",
            "--reason",
            "--no-json",
        )
        text_result = _run_cli(
            tmp_path,
            base_url,
            "task",
            "abort",
            "task-2",
            "--reason",
            "--json",
        )

    assert json_result.returncode == 1
    assert json_result.stdout == ""
    assert json.loads(json_result.stderr)["error"] == "Task not found"
    assert text_result.returncode == 1
    assert text_result.stdout == ""
    assert text_result.stderr == "Error: Task not found\n"
    assert requests == [
        ("POST", "/api/workspaces/tasks/task-1/abort"),
        ("POST", "/api/workspaces/tasks/task-2/abort"),
    ]


def test_root_json_option_parser_ignores_subcommand_position_values(tmp_path: Path) -> None:
    def responder(_: BaseHTTPRequestHandler) -> tuple[int, Any]:
        pytest.fail("parse-action must not make an HTTP request")

    with _mock_hub(responder) as (base_url, requests):
        json_result = _run_cli(
            tmp_path,
            base_url,
            "--json",
            "feishu",
            "parse-action",
            "--",
            "--no-json",
        )
        text_result = _run_cli(
            tmp_path,
            base_url,
            "feishu",
            "parse-action",
            "--",
            "--json",
        )

    assert json_result.returncode == 1
    assert json_result.stdout == ""
    assert json.loads(json_result.stderr)["error"].startswith("invalid JSON payload:")
    assert text_result.returncode == 1
    assert text_result.stdout == ""
    assert text_result.stderr.startswith("Error: invalid JSON payload:")
    assert requests == []


def test_json_http_error_is_machine_readable_on_stderr(tmp_path: Path) -> None:
    def responder(_: BaseHTTPRequestHandler) -> tuple[int, Optional[dict[str, Any]]]:
        return 404, {"detail": "Task not found"}

    with _mock_hub(responder) as (base_url, requests):
        result = _run_cli(
            tmp_path,
            base_url,
            "--json",
            "task",
            "abort",
            "missing",
            "--reason",
            "test",
        )

    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "ok": False,
        "error": "Task not found",
        "exit_code": 1,
    }
    assert requests == [("POST", "/api/workspaces/tasks/missing/abort")]


def test_json_usage_error_is_machine_readable(tmp_path: Path) -> None:
    def responder(_: BaseHTTPRequestHandler) -> tuple[int, Any]:
        pytest.fail("usage failure must not make an HTTP request")

    with _mock_hub(responder) as (base_url, requests):
        result = _run_cli(
            tmp_path,
            base_url,
            "--json",
            "task",
            "abort",
        )

    assert result.returncode == 2
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["ok"] is False
    assert error["exit_code"] == 2
    assert error["error"] == "Missing argument 'TASK_ID'."
    assert requests == []


def test_json_parameter_error_is_machine_readable_without_http(tmp_path: Path) -> None:
    def responder(_: BaseHTTPRequestHandler) -> tuple[int, Optional[dict[str, Any]]]:
        pytest.fail("parameter validation must not make an HTTP request")

    with _mock_hub(responder) as (base_url, requests):
        result = _run_cli(
            tmp_path,
            base_url,
            "--json",
            "task",
            "report",
            "task-1",
            "--limit",
            "0",
        )

    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "ok": False,
        "error": "--limit must be >= 1.",
        "exit_code": 1,
    }
    assert requests == []


def test_json_network_timeout_is_machine_readable(tmp_path: Path) -> None:
    def responder(_: BaseHTTPRequestHandler) -> tuple[int, Any]:
        time.sleep(5.5)
        return 200, []

    with _mock_hub(responder) as (base_url, requests):
        result = _run_cli(
            tmp_path,
            base_url,
            "--json",
            "task",
            "wait",
            "workspace-1",
            "task-1",
            "--timeout-seconds",
            "0",
        )

    assert result.returncode == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["ok"] is False
    assert error["exit_code"] == 1
    assert "timed out" in error["error"].lower()
    assert requests == [
        (
            "POST",
            "/api/workspaces/workspace-1/tasks/task-1/wait"
            "?since_sequence=0&subtree=false&timeout_seconds=0.0",
        )
    ]


@pytest.mark.parametrize("entrypoint", ["module", "console"])
@pytest.mark.parametrize(
    ("resource_args", "expected_path"),
    [
        (("session", "delete", "session-owned"), "/api/workspaces/sessions/session-owned"),
        (("task", "delete", "task-owned"), "/api/workspaces/tasks/task-owned"),
    ],
)
def test_delete_without_tty_sends_one_authorized_request(
    tmp_path: Path,
    entrypoint: str,
    resource_args: tuple[str, ...],
    expected_path: str,
) -> None:
    def responder(_: BaseHTTPRequestHandler) -> tuple[int, Any]:
        return 204, None

    with _mock_hub(responder) as (base_url, requests):
        result = _run_cli(
            tmp_path,
            base_url,
            "--json",
            *resource_args,
            input_text="",
            entrypoint=entrypoint,
        )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {"ok": True}
    assert result.stderr == ""
    assert requests == [("DELETE", expected_path)]


@pytest.mark.parametrize("entrypoint", ["module", "console"])
@pytest.mark.parametrize(
    ("resource_args", "expected_path"),
    [
        (("session", "delete", "session-owned"), "/api/workspaces/sessions/session-owned"),
        (("task", "delete", "task-owned"), "/api/workspaces/tasks/task-owned"),
    ],
)
def test_delete_failure_exits_nonzero_without_retry(
    tmp_path: Path,
    entrypoint: str,
    resource_args: tuple[str, ...],
    expected_path: str,
) -> None:
    def responder(_: BaseHTTPRequestHandler) -> tuple[int, Optional[dict[str, Any]]]:
        return 409, {"detail": "Deletion rejected"}

    with _mock_hub(responder) as (base_url, requests):
        result = _run_cli(
            tmp_path,
            base_url,
            "--json",
            *resource_args,
            input_text="",
            entrypoint=entrypoint,
        )

    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "ok": False,
        "error": "Deletion rejected",
        "exit_code": 1,
    }
    assert requests == [("DELETE", expected_path)]
