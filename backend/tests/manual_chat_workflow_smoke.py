"""Opt-in workflow acceptance in an isolated Hub with an explicit Codex account.

Run only from the candidate feature worktree, after building its frontend.
The source and managed worker keep that worktree as cwd. Backend and report
CLI commands use the private runtime cwd. No existing Hub is addressed.

The browser uses real startup and work APIs. Source stream and Feishu binding
are fixtures, and known external font links/resource hints are removed from
the entry response. Raw runtime evidence is private, never auto-uploaded or
certified sanitized. Cleanup requires a living, dedicated Linux controller;
it does not promise descendant cleanup after controller SIGKILL.

A signal during early directory allocation can leave empty task-owned
runtime/evidence directories. No account has been copied and no child has
been started at that point.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import unquote, urlsplit

import httpx

if TYPE_CHECKING:
    from tests import smoke_runtime_guard as guard
elif __package__:
    from tests import smoke_runtime_guard as guard
else:
    import smoke_runtime_guard as guard

_PROXY_NAMES = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)
_ROUTE_PROXY_NAMES = tuple(name for name in _PROXY_NAMES if name.lower() != "no_proxy")
_PARENT_ENV_NAMES = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "USER", "LOGNAME")


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise RuntimeError(reason)


def _error(errors: list[str], stage: str, exc: BaseException) -> None:
    # Exception text may contain command output or proxy authentication.
    errors.append(f"{stage}:{type(exc).__name__}")


def _environment(
    parent: Mapping[str, str],
    runtime: Path,
    backend: Path,
    url: str,
    socket_name: str,
    model: str,
    mode: str,
) -> dict[str, str]:
    env = {key: parent[key] for key in _PARENT_ENV_NAMES if key in parent}
    env.setdefault("PATH", os.defpath)
    env.setdefault("LANG", "C.UTF-8")
    env.update(
        HOME=str(runtime / "home"),
        CODEX_HOME=str(runtime / "codex"),
        XDG_CONFIG_HOME=str(runtime / "xdg-config"),
        XDG_CACHE_HOME=str(runtime / "xdg-cache"),
        XDG_DATA_HOME=str(runtime / "xdg-data"),
        XDG_STATE_HOME=str(runtime / "xdg-state"),
        XDG_RUNTIME_DIR=str(runtime / "xdg-run"),
        XDG_CONFIG_DIRS=str(runtime / "xdg-system-config"),
        XDG_DATA_DIRS=str(runtime / "xdg-system-data"),
        TMPDIR=str(runtime / "tmp"),
        TEMP=str(runtime / "tmp"),
        TMP=str(runtime / "tmp"),
        TMUX_TMPDIR=str(runtime / "t"),
        SHELL=str(runtime / "shell"),
        TERM="xterm-256color",
        PYTHONNOUSERSITE="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=str(backend),
        CLAUDE_HUB_HOME=str(runtime / "hub"),
        CLAUDE_HUB_STATE_ROOT=str(runtime / "hub" / "workspaces"),
        CLAUDE_HUB_CONFIG=str(runtime / "cli.toml"),
        CLAUDE_HUB_TMUX_SOCKET=socket_name,
        CLAUDE_HUB_URL=url,
        CLAUDE_HUB_PROVIDER_NETWORK_MODE=mode,
        CODEX_MODEL=model,
        PORT=str(urlsplit(url).port),
        SERVE_FRONTEND="true",
    )
    return env


def _proxy_inputs(parent: Mapping[str, str], mode: str, authorized: bool) -> dict[str, str]:
    if mode == "direct":
        return {}
    _require(authorized, "auto/proxy requires explicit --inherit-proxy-env")
    values = {key: parent[key] for key in _PROXY_NAMES if key in parent}
    if mode == "proxy":
        _require(any(values.get(key) for key in _ROUTE_PROXY_NAMES), "proxy is not configured")
    return values


async def _network_selection(env: Mapping[str, str]) -> dict[str, Any]:
    from claude_hub.services.agent_stream import provider_network

    async def strict_login_status(values: Mapping[str, str]) -> str:
        # The shared selector's auto compatibility fallback is too permissive
        # for this smoke. Do not proceed if the copied account cannot be identified.
        mode = await provider_network._codex_login_status(values)
        if mode not in {"chatgpt", "api_key"}:
            raise provider_network.ProviderEndpointUnknownError("smoke login mode is unknown")
        return mode

    selector = provider_network.ProviderNetworkSelector(login_status=strict_login_status)
    selected = await selector.select("codex", env)
    return {
        "requested_mode": env[provider_network.PROVIDER_NETWORK_MODE_ENV],
        "proxy_selected": any(selected.get(key) for key in provider_network.PROXY_ENV_NAMES),
    }


class _EntryHints(HTMLParser):
    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=False)
        self.offsets = [0, *(match.end() for match in re.finditer("\n", text))]
        self.removals: list[tuple[int, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link":
            return
        values = dict(attrs)
        rel = set((values.get("rel") or "").lower().split())
        host = urlsplit(values.get("href") or "").hostname
        if not (rel & {"preconnect", "dns-prefetch"}) and host not in {
            "fonts.googleapis.com",
            "fonts.gstatic.com",
        }:
            return
        tag_text = self.get_starttag_text()
        if tag_text is None:
            raise ValueError("link tag text is unavailable")
        line, column = self.getpos()
        start = self.offsets[line - 1] + column
        self.removals.append((start, start + len(tag_text)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def _strip_entry_hints(text: str) -> str:
    parser = _EntryHints(text)
    parser.feed(text)
    parser.close()
    for start, end in reversed(parser.removals):
        text = text[:start] + text[end:]
    return text


class _WorkflowUiBoundary:
    def __init__(self, url: str, tab_id: str, work_id: str | None) -> None:
        parsed = urlsplit(url)
        self.origin = (parsed.scheme, parsed.hostname, parsed.port)
        self.tab_id = tab_id
        self.work_id = work_id
        self.stream = f"/api/workspaces/tabs/{tab_id}/stream"
        self.work_path = f"/api/tabs/{tab_id}/work"
        self.errors: list[str] = []
        self.counts = dict(capabilities=0, events=0, wait=0, live=0, binding=0, work=0)
        self.closing = False
        self.deadline = time.monotonic() + 20

    def _reject(self, route: Any, reason: str) -> None:
        self.errors.append(reason)
        route.abort("blockedbyclient")

    def websocket(self, route: Any) -> None:
        if not self.closing:
            self.errors.append("unexpected-websocket")
        route.close()

    def http(self, route: Any) -> None:
        if self.closing:
            return
        try:
            self._http(route)
        except Exception as exc:
            _error(self.errors, "ui-route", exc)
            if not self.closing:
                route.abort("blockedbyclient")

    def _http(self, route: Any) -> None:
        request = route.request
        parsed = urlsplit(request.url)
        path, method = parsed.path, request.method
        if (
            (parsed.scheme, parsed.hostname, parsed.port) != self.origin
            or parsed.username is not None
            or parsed.password is not None
            or unquote(path) != path
            or ".." in path.split("/")
            or time.monotonic() >= self.deadline
        ):
            self._reject(route, "unexpected-origin-path-or-deadline")
            return
        if method == "GET" and path == "/api/feishu/bot/binding":
            self.counts["binding"] += 1
            route.fulfill(status=200, json={"binding": None})
            return
        if method == "GET" and path == self.stream + "/capabilities":
            self.counts["capabilities"] += 1
            route.fulfill(
                status=200,
                json={
                    "structured": True,
                    "adapter_id": "manual-work-smoke",
                    "schema_version": 1,
                    "sources": [],
                    "supports_approval_ui": False,
                    "supports_tool_timeline": True,
                    "supports_images": False,
                    "supports_dynamic_modes": False,
                    "supports_goals": False,
                    "goal_execution_owner": None,
                    "goal_usage_quality": "unavailable",
                    "available_modes": [],
                    "current_mode": None,
                    "available_models": [],
                    "current_model": None,
                    "current_reasoning_effort": None,
                },
            )
            return
        if (method, path) in {
            ("GET", self.stream + "/events"),
            ("POST", self.stream + "/wait"),
        }:
            key = "wait" if method == "POST" else "events"
            self.counts[key] += 1
            if key == "wait":
                if self.counts[key] > 256:
                    self._reject(route, "stream-wait-budget")
                    return
                time.sleep(0.05)
            route.fulfill(status=200, json={"events": [], "next_sequence": -1, "has_more": False})
            return
        if method == "GET" and path == self.stream + "/live":
            self.counts["live"] += 1
            # HTTP 204 ends EventSource retries; long-poll remains the fixture.
            route.fulfill(status=204, body="")
            return
        allowed_get = {
            "/api/auth/check",
            "/api/tabs",
            "/api/tabs/archived",
            "/api/tabs/status",
            "/api/workspaces",
            "/api/workspaces/sessions",
            "/api/env-presets",
            f"/api/tabs/{self.tab_id}",
            f"/api/tabs/{self.tab_id}/goal/current",
            self.work_path,
        }
        if self.work_id:
            allowed_get.add(self.work_path + "/" + self.work_id)
        static = path in {"/", "/vite.svg"} or path.startswith("/assets/")
        if not (
            (method == "GET" and (path in allowed_get or static))
            or (method == "POST" and path == f"/api/tabs/{self.tab_id}/view")
        ):
            self._reject(route, "unexpected-http")
            return
        # Do not let redirects escape the validated origin through route.fetch.
        response = route.fetch(max_redirects=0, timeout=5000)
        if not 200 <= response.status < 300:
            self._reject(route, "real-http-status")
            return
        body = response.body()
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in {"content-length", "content-encoding"}
        }
        if path == self.work_path:
            rows = json.loads(body)
            if self.work_id is None:
                _require(rows == [], "warmup work list was not empty")
            else:
                _require(
                    len(rows) == 1
                    and rows[0]["id"] == self.work_id
                    and rows[0]["source_tab_id"] == self.tab_id
                    and rows[0]["status"] == "completed",
                    "real work response did not match the completed owned work",
                )
            self.counts["work"] += 1
        if path == "/":
            body = _strip_entry_hints(body.decode("utf-8")).encode("utf-8")
        route.fulfill(status=response.status, headers=headers, body=body)

    def ready(self) -> bool:
        return all(self.counts.values())


def _browser(spec: dict[str, Any]) -> None:
    from playwright.sync_api import BrowserContext, sync_playwright

    _require(hasattr(BrowserContext, "route_web_socket"), "Playwright WS routing is unavailable")
    boundary = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=spec["executable"],
            headless=True,
            timeout=10000,
            args=["--no-proxy-server", "--disable-background-networking"],
        )
        context = None
        try:
            context = browser.new_context(
                service_workers="block",
                viewport={"width": 1280, "height": 900},
            )
            if spec.get("url"):
                boundary = _WorkflowUiBoundary(spec["url"], spec["tab_id"], spec.get("work_id"))
                context.route("**/*", boundary.http)
                context.route_web_socket("**/*", boundary.websocket)
            else:
                context.route("**/*", lambda route: route.abort("blockedbyclient"))
                context.route_web_socket("**/*", lambda route: route.close())
            page = context.new_page()
            if boundary:
                page.on("pageerror", lambda _: boundary.errors.append("pageerror"))
                page.goto(spec["url"], wait_until="domcontentloaded", timeout=10000)
                page.get_by_test_id("feishu-binding-trigger").wait_for(timeout=10000)
                deadline = time.monotonic() + 10
                while not boundary.ready() and not boundary.errors and time.monotonic() < deadline:
                    page.wait_for_timeout(50)
                _require(boundary.ready() and not boundary.errors, "UI initialization failed")
                if spec.get("work_id"):
                    card = page.locator(f'[data-work-id="{spec["work_id"]}"]')
                    card.wait_for(timeout=5000)
                    _require(card.get_attribute("data-status") == "completed", "work card mismatch")
                    page.screenshot(path=spec["screenshot"], full_page=True, timeout=5000)
            else:
                page.goto("about:blank")
                _require(page.evaluate("1 + 1") == 2, "browser JavaScript probe failed")
        finally:
            if boundary:
                boundary.closing = True
            try:
                if context is not None:
                    context.close()
            finally:
                browser.close()
    if boundary:
        _require(not boundary.errors, "UI boundary rejected a request or page error")
    Path(spec["proof"]).write_text(
        json.dumps(
            {
                "fresh_context": True,
                "service_workers": "blocked",
                "source_stream": "mocked" if boundary else "not-opened",
                "feishu_binding": "mocked-unbound" if boundary else "not-opened",
                "entry_external_fonts_and_hints": "removed" if boundary else "not-opened",
                "real_work_reads": boundary.counts["work"] if boundary else 0,
                "unexpected_requests": 0,
            }
        ),
        encoding="utf-8",
    )


def _spawn(
    command: list[str],
    env: dict[str, str],
    cwd: Path,
    known: list[subprocess.Popen[Any]],
    output: Path,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.Popen[Any]:
    with (
        output.open("wb") as stdout,
        output.with_name(output.name + ".stderr").open("wb") as stderr,
    ):
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            pass_fds=pass_fds,
        )
    known.append(process)
    return process


def _command(
    command: list[str],
    env: dict[str, str],
    cwd: Path,
    known: list[subprocess.Popen[Any]],
    errors: list[str],
    output: Path,
    timeout: float,
    check: bool = True,
) -> int:
    process = _spawn(command, env, cwd, known, output)
    try:
        code = process.wait(timeout=timeout)
    except (Exception, KeyboardInterrupt):
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
        except (Exception, KeyboardInterrupt) as exc:
            _error(errors, "command-cancel", exc)
        raise
    if check:
        _require(code == 0, "child command failed; raw output remains private")
    return code


def _child_code(body: str) -> str:
    # -I ignores inherited PYTHONPATH; both imports are pinned explicitly.
    return (
        "import sys; sys.dont_write_bytecode = True; sys.path[:0] = sys.argv[1:3]; "
        "import manual_chat_workflow_smoke as smoke; " + body
    )


def _run_browser(
    spec: dict[str, Any],
    env: dict[str, str],
    runtime: Path,
    backend: Path,
    known: list[subprocess.Popen[Any]],
    errors: list[str],
    phase: str,
) -> None:
    command = [
        sys.executable,
        "-I",
        "-c",
        _child_code("import json; smoke._browser(json.loads(sys.argv[3]))"),
        str(backend / "tests"),
        str(backend),
        json.dumps(spec),
    ]
    _command(command, env, runtime, known, errors, runtime / f"{phase}.stdout", 40)


def _write_shell(env: Mapping[str, str]) -> None:
    exports = "\n".join(f"export {key}={shlex.quote(value)}" for key, value in env.items())
    path = Path(env["SHELL"])
    path.write_text(
        "#!/bin/sh\nset -eu\nunset BASH_ENV ENV CDPATH\n"
        + exports
        + '\nexec /bin/bash --noprofile --norc "$@"\n',
        encoding="utf-8",
    )
    path.chmod(0o700)


def _absent(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False


def _stop_stack(
    known: list[subprocess.Popen[Any]],
    env: dict[str, str],
    runtime: Path,
    socket_path: Path,
    errors: list[str],
    phase: str,
) -> dict[str, Any]:
    start_errors = len(errors)
    # Only these Popen objects own reaping for the known controller children.
    # SIGCHLD is restored to default before any child is created.
    for process in list(known):
        try:
            if process.poll() is None:
                process.terminate()
        except ProcessLookupError:
            pass
        except (Exception, KeyboardInterrupt) as exc:
            _error(errors, "terminate", exc)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=2)
            except (Exception, KeyboardInterrupt) as exc:
                _error(errors, "kill-wait", exc)
        except (Exception, KeyboardInterrupt) as exc:
            _error(errors, "wait", exc)
    try:
        if not _absent(socket_path):
            code = _command(
                ["tmux", "-S", str(socket_path), "kill-server"],
                env,
                runtime,
                known,
                errors,
                runtime / f"{phase}-tmux.stdout",
                3,
                check=False,
            )
            _require(code == 0, "named tmux stop failed")
    except (Exception, KeyboardInterrupt) as exc:
        _error(errors, "tmux-stop", exc)
    core: dict[str, Any] = {"complete": False}
    try:
        core = guard.stop_owned_descendants(known, term_timeout=3, kill_timeout=3)
        if not core["complete"]:
            errors.append("descendants:unverified")
    except (Exception, KeyboardInterrupt) as exc:
        _error(errors, "process-guard", exc)
        core = {"complete": False}
    socket_absent = False
    try:
        socket_absent = _absent(socket_path)
        if not socket_absent:
            errors.append("tmux-socket:present")
    except (Exception, KeyboardInterrupt) as exc:
        _error(errors, "tmux-socket-check", exc)
    return {
        "processes_verified": bool(core["complete"]),
        "tmux_socket_absent": socket_absent,
        "process_guard": core,
        "complete": bool(core["complete"]) and socket_absent and len(errors) == start_errors,
    }


def _remove_path(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(metadata.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _remove_sensitive(runtime: Path, writers_verified: bool, errors: list[str]) -> bool:
    start_errors = len(errors)
    # launch_env may contain per-tab credential/proxy wrappers. Raw logs are
    # deliberately NOT certified clean when these known paths are removed.
    targets = [runtime / "shell", runtime / "hub" / "launch_env"]
    targets.append(runtime / "codex" if writers_verified else runtime / "codex" / "auth.json")
    for path in targets:
        try:
            _remove_path(path)
        except (Exception, KeyboardInterrupt) as exc:
            _error(errors, "sensitive-remove", exc)
    absent = True
    for path in targets:
        try:
            absent = _absent(path) and absent
        except (Exception, KeyboardInterrupt) as exc:
            absent = False
            _error(errors, "sensitive-check", exc)
    verified = writers_verified and absent and len(errors) == start_errors
    if not verified:
        errors.append("sensitive-paths:unverified")
    return verified


def _final_cleanup(
    known: list[subprocess.Popen[Any]],
    env: dict[str, str],
    runtime: Path,
    socket_path: Path,
    listener: socket.socket | None,
    port: int | None,
    errors: list[str],
) -> dict[str, Any]:
    start_errors = len(errors)
    cleanup: dict[str, Any] = {"processes_verified": False, "complete": False}
    try:
        cleanup = _stop_stack(known, env, runtime, socket_path, errors, "final")
    except (Exception, KeyboardInterrupt) as exc:
        _error(errors, "stack-cleanup", exc)
    if listener is not None:
        try:
            listener.close()
        except (Exception, KeyboardInterrupt) as exc:
            _error(errors, "listener-close", exc)
    if port is not None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", port))
            cleanup["port_available_after_close"] = True
        except (Exception, KeyboardInterrupt) as exc:
            cleanup["port_available_after_close"] = False
            _error(errors, "port-check", exc)
    cleanup["sensitive_paths_removed_after_exit"] = False
    try:
        cleanup["sensitive_paths_removed_after_exit"] = _remove_sensitive(
            runtime,
            bool(cleanup.get("processes_verified")),
            errors,
        )
    except (Exception, KeyboardInterrupt) as exc:
        _error(errors, "sensitive-cleanup", exc)
    cleanup["complete"] = (
        bool(cleanup.get("complete"))
        and bool(cleanup.get("processes_verified"))
        and (port is None or bool(cleanup.get("port_available_after_close")))
        and bool(cleanup.get("sensitive_paths_removed_after_exit"))
        and len(errors) == start_errors
    )
    return cleanup


def _write_result(path: Path, result: dict[str, Any]) -> None:
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def _finish_result(result: dict[str, Any], runtime: Path, artifact: Path) -> bool:
    errors = result["errors"]
    passed = (
        result.get("scenario_passed") and result.get("cleanup", {}).get("complete") and not errors
    )
    result["status"] = "pass" if passed else "failed"
    result["runtime_retained"] = result["status"] != "pass"
    result["raw_evidence_is_private"] = True
    result["shareable_verified"] = False
    result["auto_uploaded"] = False
    written = False
    try:
        _write_result(artifact / "result.json", result)
        written = True
    except (Exception, KeyboardInterrupt) as exc:
        _error(errors, "result-write", exc)
        result.update(status="failed", runtime_retained=True)
    if written and result["status"] == "pass":
        try:
            shutil.rmtree(runtime)
        except (Exception, KeyboardInterrupt) as exc:
            _error(errors, "runtime-remove", exc)
            result.update(status="failed", runtime_retained=True)
    if result["status"] == "failed":
        try:
            _write_result(artifact / "result.json", result)
        except (Exception, KeyboardInterrupt) as exc:
            _error(errors, "final-result-write", exc)
    print(json.dumps(result), flush=True)
    return bool(result["status"] == "pass")


def _ready(
    client: httpx.Client, process: subprocess.Popen[Any], previous: str | None = None
) -> str:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        _require(process.poll() is None, "isolated backend exited before readiness")
        try:
            response = client.get("/health", timeout=1)
            if response.status_code == 200:
                value = response.json()
                instance = value.get("instance_id")
                if value.get("status") == "healthy" and isinstance(instance, str) and instance:
                    _require(instance != previous, "restart reused the previous backend instance")
                    return instance
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise TimeoutError("isolated backend did not become ready")


class _CleanupSignals:
    def __init__(self) -> None:
        self.deferred = False
        self.received = {"SIGINT": False, "SIGTERM": False}

    def __call__(self, signum: int, frame: Any) -> None:
        if signum == signal.SIGINT:
            name = "SIGINT"
        elif signum == signal.SIGTERM:
            name = "SIGTERM"
        else:
            return
        already_deferred = self.deferred
        self.deferred = True
        self.received[name] = True
        if not already_deferred:
            raise KeyboardInterrupt

    def install(self) -> None:
        watched = {signal.SIGINT, signal.SIGTERM}
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, watched)
        try:
            for number in watched:
                signal.signal(number, self)
        except BaseException:
            # No runtime or child exists yet; leave signals blocked until exit.
            self.deferred = True
            raise
        signal.pthread_sigmask(signal.SIG_SETMASK, previous - watched)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-provider", action="store_true", required=True)
    parser.add_argument(
        "--runtime-root", type=Path, required=True, help="Existing parent for a new private runtime"
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        required=True,
        help="Existing parent for a new owned evidence directory",
    )
    parser.add_argument("--codex-auth-file", type=Path, required=True)
    parser.add_argument("--browser-executable", type=Path, required=True)
    parser.add_argument("--codex-model", required=True)
    parser.add_argument("--network-mode", choices=("auto", "direct", "proxy"), required=True)
    parser.add_argument("--inherit-proxy-env", action="store_true")
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    _require(args.timeout > 0 and bool(args.codex_model.strip()), "invalid timeout or model")
    backend = Path(__file__).resolve().parents[1]
    checkout = backend.parent
    _require(
        checkout.parent == Path.home().resolve() / "claude_hub_worktree"
        and (checkout / ".git").is_file(),
        "run only from a canonical linked feature worktree",
    )
    _require(_absent(checkout / ".codex" / "config.toml"), "repo-local Codex configuration exists")
    _require(
        (checkout / "frontend" / "dist" / "index.html").is_file(),
        "build the candidate frontend first",
    )
    auth_source = args.codex_auth_file.expanduser().resolve(strict=True)
    auth_stat = auth_source.stat()
    _require(
        stat.S_ISREG(auth_stat.st_mode)
        and auth_stat.st_uid == os.getuid()
        and not auth_stat.st_mode & 0o077,
        "the explicit auth source must be an owner-only regular file",
    )
    browser = args.browser_executable.expanduser().resolve(strict=True)
    _require(browser.is_file() and os.access(browser, os.X_OK), "browser executable is unavailable")
    _require(os.access("/bin/bash", os.X_OK), "the private wrapper requires /bin/bash")
    runtime_root = args.runtime_root.expanduser().resolve(strict=True)
    artifact_root = args.artifact_dir.expanduser().resolve(strict=True)
    _require(runtime_root.is_dir() and artifact_root.is_dir(), "output parents must be directories")
    proxy_inputs = _proxy_inputs(os.environ, args.network_mode, args.inherit_proxy_env)
    interrupts = _CleanupSignals()
    interrupts.install()
    os.umask(0o077)
    runtime = Path(tempfile.mkdtemp(prefix="chw-", dir=runtime_root))
    try:
        artifact = Path(tempfile.mkdtemp(prefix="chw-evidence-", dir=artifact_root))
    except BaseException:
        runtime.rmdir()
        raise
    errors: list[str] = []
    known: list[subprocess.Popen[Any]] = []
    result: dict[str, Any] = {
        "checkout": str(checkout),
        "runtime": str(runtime),
        "artifact_dir": str(artifact),
        "errors": errors,
        "scenario_passed": False,
        "phase": "preflight",
        "termination_signals": interrupts.received,
    }
    listener = None
    client = None
    env: dict[str, str] = {}
    socket_path = runtime / "not-started"
    process = None
    tab_id = work_id = None
    port = None
    try:
        # This must be a dedicated process, not an imported pytest/session runner.
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        guard.require_runtime_capabilities()
        _require(
            re.fullmatch(r"[A-Za-z0-9_./-]+", str(runtime)) is not None,
            "runtime path is not shell-safe",
        )
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        socket_name = "chw-" + uuid.uuid4().hex[:12]
        env = _environment(
            os.environ, runtime, backend, url, socket_name, args.codex_model, args.network_mode
        )
        browser_env = dict(env)
        socket_path = Path(env["TMUX_TMPDIR"]) / f"tmux-{os.getuid()}" / socket_name
        _require(len(os.fsencode(socket_path)) < 104, "private tmux socket path is too long")
        for name in (
            "HOME",
            "CODEX_HOME",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_RUNTIME_DIR",
            "XDG_CONFIG_DIRS",
            "XDG_DATA_DIRS",
            "TMPDIR",
            "TMUX_TMPDIR",
            "CLAUDE_HUB_HOME",
            "CLAUDE_HUB_STATE_ROOT",
        ):
            Path(env[name]).mkdir(mode=0o700, parents=True, exist_ok=True)
        Path(env["CLAUDE_HUB_CONFIG"]).write_text("", encoding="utf-8")
        codex_home = Path(env["CODEX_HOME"])
        codex_home.joinpath("config.toml").write_text(
            'cli_auth_credentials_store = "file"\nmodel_provider = "openai"\n'
            + "[projects."
            + json.dumps(str(checkout), ensure_ascii=False)
            + "]\n"
            + 'trust_level = "trusted"\n',
            encoding="utf-8",
        )
        for executable in ("codex", "tmux", "ttyd"):
            _require(
                shutil.which(executable, path=env["PATH"]) is not None,
                "required executable is unavailable",
            )
        _command(
            ["git", "-C", str(checkout), "symbolic-ref", "--short", "HEAD"],
            env,
            runtime,
            known,
            errors,
            runtime / "branch.stdout",
            5,
        )
        branch = (runtime / "branch.stdout").read_text(encoding="utf-8").strip()
        _require(bool(branch) and branch != "main", "the smoke must not execute from main")
        result.update(url=url, branch=branch, codex_model=args.codex_model)
        _run_browser(
            {"executable": str(browser), "proof": str(artifact / "browser-preflight.json")},
            browser_env,
            runtime,
            backend,
            known,
            errors,
            "browser-preflight",
        )
        preflight_cleanup = guard.stop_owned_descendants(known, term_timeout=1, kill_timeout=2)
        result["browser_preflight_cleanup"] = preflight_cleanup
        _require(preflight_cleanup["complete"], "browser preflight left unverified descendants")

        # Only this explicitly selected account is copied. No real HOME config,
        # provider endpoint override, token environment, or CLI preset is imported.
        shutil.copyfile(auth_source, codex_home / "auth.json")
        (codex_home / "auth.json").chmod(0o600)
        env.update(proxy_inputs)
        result["phase"] = "network-preflight"
        _command(
            [
                sys.executable,
                "-I",
                "-c",
                _child_code(
                    "import asyncio,json,os; print(json.dumps(asyncio.run(smoke._network_selection(dict(os.environ)))))"
                ),
                str(backend / "tests"),
                str(backend),
            ],
            env,
            runtime,
            known,
            errors,
            runtime / "network.stdout",
            15,
        )
        network = json.loads((runtime / "network.stdout").read_text(encoding="utf-8"))
        _require(
            isinstance(network.get("proxy_selected"), bool), "invalid network preflight result"
        )
        if not network["proxy_selected"]:
            for key in _ROUTE_PROXY_NAMES:
                env.pop(key, None)
        result["network"] = network
        _write_shell(env)

        def start_backend(phase: str) -> subprocess.Popen[Any]:
            _command(
                [
                    "tmux",
                    "-f",
                    "/dev/null",
                    "-L",
                    socket_name,
                    "new-session",
                    "-d",
                    "-s",
                    "__tmux_server_keepalive__",
                    env["SHELL"],
                ],
                env,
                runtime,
                known,
                errors,
                runtime / f"{phase}-tmux-start.stdout",
                5,
            )
            assert listener is not None
            return _spawn(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "uvicorn",
                    "claude_hub.main:app",
                    "--fd",
                    str(listener.fileno()),
                ],
                env,
                runtime,
                known,
                runtime / f"{phase}-backend.stdout",
                (listener.fileno(),),
            )

        result["phase"] = "initial-backend"
        process = start_backend("initial")
        client = httpx.Client(base_url=url, trust_env=False, timeout=30, follow_redirects=False)
        instance = _ready(client, process)

        def request(method: str, path: str, **kwargs: Any) -> Any:
            assert client is not None
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json() if response.content else None

        workspace = request("POST", "/api/workspaces/ensure", json={"path": str(checkout)})
        wid = workspace["id"]
        request("PUT", f"/api/workspaces/{wid}/feedback/automation", json={"enabled": False})
        _require(
            request(
                "GET",
                f"/api/workspaces/{wid}/feedback/context",
                params={"query": "unrelated-smoke-topic"},
            )
            == [],
            "fresh feedback context was not empty",
        )
        tab_env = {key: value for key, value in env.items() if key not in _PROXY_NAMES}
        tab = request(
            "POST",
            "/api/tabs",
            json={
                "name": "Isolated workflow acceptance",
                "cwd": str(checkout),
                "agent_type": "codex",
                "session_kind": "chat",
                "env": tab_env,
            },
        )
        tab_id = tab["id"]
        result.update(tab_id=tab_id, workspace_id=wid)
        result["phase"] = "ui-warmup"
        _run_browser(
            {
                "executable": str(browser),
                "url": url,
                "tab_id": tab_id,
                "proof": str(artifact / "browser-warmup.json"),
            },
            browser_env,
            runtime,
            backend,
            known,
            errors,
            "browser-warmup",
        )

        marker = artifact / "worker-marker.json"
        marker_code = (
            "import json,os; from pathlib import Path; import claude_hub; "
            f"p=Path({str(marker)!r}); "
            "data={'marker':'HUB_WORKFLOW_MARKER','cwd':os.getcwd(),"
            "'package_file':str(Path(claude_hub.__file__).resolve())}; "
            "p.open('x',encoding='utf-8').write(json.dumps(data))"
        )
        marker_command = shlex.join([sys.executable, "-B", "-c", marker_code])
        cli_prefix = [
            sys.executable,
            "-B",
            "-m",
            "claude_hub.cli",
            "--base-url",
            url,
            "--config",
            env["CLAUDE_HUB_CONFIG"],
        ]
        prompt = (
            "This is a bounded transport acceptance test. Keep your assigned cwd unchanged. "
            "Do not inspect/edit the repo, run git, create another worktree, delegate, or create schedules. "
            "The only file you may create is this owned marker; first run exactly: "
            + marker_command
            + ". "
            "Then use only the Chat-linked work report command from your assignment: first kind=progress "
            "summary=HUB_WORKFLOW_PROGRESS validation=transport-smoke; then kind=completed "
            "summary=HUB_WORKFLOW_COMPLETE validation=transport-smoke; then stop. "
            "Use the explicit source tab/task/session IDs provided by that assignment. "
            "For each report, use a subshell that changes only that command's cwd to "
            + shlex.quote(str(runtime))
            + " and executes "
            + shlex.join(cli_prefix)
            + " work report ... . The private environment pins PYTHONPATH to this candidate. "
            "Do not read authentication or configuration files or print environment values."
        )
        request_key = "acceptance-once"
        work_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"claude-hub:chat-work:{tab_id}:{request_key}")
        )
        result["work_id"] = work_id
        cli_env = {**env, "CLAUDE_HUB_TAB_ID": tab_id}
        result["phase"] = "create-work"
        _command(
            cli_prefix
            + [
                "work",
                "create",
                "--workspace-id",
                wid,
                "--request-key",
                request_key,
                "--title",
                "Verify linked task reporting",
                "--prompt",
                prompt,
                "--agent-type",
                "codex",
                "--model",
                args.codex_model,
                "--task-mode",
                "direct",
                "--cwd",
                str(checkout),
            ],
            cli_env,
            runtime,
            known,
            errors,
            runtime / "cli-create.stdout",
            160,
        )
        work = json.loads((runtime / "cli-create.stdout").read_text(encoding="utf-8"))
        _require(
            work["id"] == work_id and len(work["executions"]) == 1, "created work identity mismatch"
        )
        task_id = work["executions"][0]["task_id"]
        nodes = request("GET", f"/api/workspaces/{wid}/tasks/{task_id}/tree")
        task = next(node for node in nodes if node["id"] == task_id)
        worker_sid, worker_tab = task["chat_work_owned_session_id"], task["chat_work_owned_tab_id"]
        _require(
            bool(worker_sid) and bool(worker_tab) and worker_tab != tab_id,
            "owned worker identity is missing",
        )
        session = next(
            item for item in request("GET", "/api/workspaces/sessions") if item["id"] == worker_sid
        )
        _require(
            session["tab_id"] == worker_tab
            and session["workspace_id"] == wid
            and session["caller_owned_ephemeral"],
            "owned worker incarnation mismatch",
        )
        result.update(task_id=task_id, worker_session_id=worker_sid, worker_tab_id=worker_tab)
        retry = request(
            "POST",
            f"/api/tabs/{tab_id}/work",
            json={
                "workspace_id": wid,
                "request_key": request_key,
                "title": "Verify linked task reporting",
                "prompt": prompt,
                "agent_type": "codex",
                "model": args.codex_model,
                "task_mode": "direct",
                "cwd": str(checkout),
                "kind": "task",
            },
        )
        _require(
            retry["id"] == work_id and retry["run_count"] == 1, "idempotent retry changed work"
        )
        result["phase"] = "await-report"
        deadline = time.monotonic() + args.timeout
        seen_progress = False
        while time.monotonic() < deadline:
            work = request("GET", f"/api/tabs/{tab_id}/work/{work_id}", timeout=10)
            seen_progress |= (work.get("latest_result") or {}).get("kind") == "progress"
            if work["status"] in {"completed", "failed", "review", "stopped"}:
                break
            time.sleep(1)
        _require(work["status"] == "review", "work did not await caller acceptance")
        _require(
            work["latest_result"]["summary"] == "HUB_WORKFLOW_COMPLETE",
            "completion summary mismatch",
        )
        _require(
            work["run_count"] == 1
            and len(work["executions"]) == 1
            and work["cwd"] == str(checkout)
            and work["model"] == args.codex_model,
            "work execution contract mismatch",
        )
        reports = request("GET", f"/api/workspaces/{wid}/tasks/{task_id}/reports")
        _require(
            any(
                report.get("state") == "working" and report.get("chat_work_outcome") == "progress"
                for report in reports
            ),
            "canonical progress report was not preserved",
        )
        with marker.open("rb") as stream:
            marker_bytes = stream.read(4097)
        _require(len(marker_bytes) <= 4096, "worker marker exceeded its size limit")
        _require(
            json.loads(marker_bytes)
            == {
                "marker": "HUB_WORKFLOW_MARKER",
                "cwd": str(checkout),
                "package_file": str((backend / "claude_hub" / "__init__.py").resolve()),
            },
            "worker marker did not prove candidate cwd and package",
        )
        result.update(
            observed_progress_poll=seen_progress,
            progress_preserved_before_completion=True,
            marker_verified=True,
        )
        result["phase"] = "caller-acceptance"
        _command(
            cli_prefix
            + [
                "task",
                "accept",
                task_id,
                "--workspace-id",
                wid,
                "--cleanup-session",
            ],
            cli_env,
            runtime,
            known,
            errors,
            runtime / "cli-accept.stdout",
            60,
        )
        work = request("GET", f"/api/tabs/{tab_id}/work/{work_id}")
        _require(work["status"] == "completed", "caller acceptance did not complete work")
        deadline = time.monotonic() + 20
        removed = False
        while time.monotonic() < deadline:
            sessions = request("GET", "/api/workspaces/sessions", timeout=3)
            tab_response = client.get(f"/api/tabs/{worker_tab}", timeout=3)
            _require(tab_response.status_code in {200, 404}, "worker tab verification failed")
            removed = (
                not any(item["id"] == worker_sid for item in sessions)
                and tab_response.status_code == 404
            )
            if removed:
                break
            time.sleep(0.2)
        _require(removed, "accepted work retained its exact owned worker session/tab")
        result.update(caller_acceptance=True, exact_worker_tab_404=True)

        result["phase"] = "cold-restart"
        restart_cleanup = _stop_stack(known, env, runtime, socket_path, errors, "restart")
        result["restart_cleanup"] = restart_cleanup
        _require(restart_cleanup["complete"], "old stack could not be verified stopped")
        # The controller still owns the listening fd; the port is not released here.
        process = start_backend("restarted")
        _ready(client, process, previous=instance)
        recovered = request("GET", f"/api/tabs/{tab_id}/work/{work_id}")
        _require(
            recovered["status"] == "completed"
            and recovered["run_count"] == 1
            and len(recovered["executions"]) == 1
            and recovered["executions"][0]["task_id"] == task_id
            and recovered["latest_result"]["summary"] == "HUB_WORKFLOW_COMPLETE",
            "cold restart changed or replayed completed work",
        )
        _require(
            client.get(f"/api/tabs/{worker_tab}", timeout=3).status_code == 404,
            "restart restored the deleted worker tab",
        )
        _require(
            {item["id"] for item in request("GET", "/api/tabs")} == {tab_id},
            "unexpected live tab after restart",
        )
        result["cold_restart"] = True
        result["phase"] = "browser-work"
        _run_browser(
            {
                "executable": str(browser),
                "url": url,
                "tab_id": tab_id,
                "work_id": work_id,
                "screenshot": str(artifact / "browser.png"),
                "proof": str(artifact / "browser-work.json"),
            },
            browser_env,
            runtime,
            backend,
            known,
            errors,
            "browser-work",
        )
        interrupts.deferred = True
        result["scenario_passed"] = True
    except (Exception, KeyboardInterrupt) as exc:
        interrupts.deferred = True
        result["failure_phase"] = result["phase"]
        _error(errors, "scenario", exc)
    finally:
        interrupts.deferred = True
        result["phase"] = "cleanup"
        if client is not None:
            if tab_id and work_id and not result["scenario_passed"]:
                try:
                    response = client.patch(
                        f"/api/tabs/{tab_id}/work/{work_id}", json={"action": "stop"}, timeout=5
                    )
                    _require(response.status_code in {200, 404}, "owned work stop failed")
                except (Exception, KeyboardInterrupt) as exc:
                    _error(errors, "work-stop", exc)
            if tab_id:
                try:
                    response = client.delete(f"/api/tabs/{tab_id}", timeout=5)
                    _require(response.status_code in {200, 204, 404}, "source tab delete failed")
                    _require(
                        client.get(f"/api/tabs/{tab_id}", timeout=3).status_code == 404,
                        "source tab removal was not verified",
                    )
                    result["source_tab_404"] = True
                except (Exception, KeyboardInterrupt) as exc:
                    _error(errors, "source-tab-remove", exc)
            try:
                client.close()
            except (Exception, KeyboardInterrupt) as exc:
                _error(errors, "client-close", exc)
        result["cleanup"] = _final_cleanup(
            known,
            env,
            runtime,
            socket_path,
            listener,
            port,
            errors,
        )
        raise SystemExit(0 if _finish_result(result, runtime, artifact) else 1)


if __name__ == "__main__":
    main()
