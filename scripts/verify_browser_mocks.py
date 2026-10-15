#!/usr/bin/env python3
"""Run the mocked browser checks with explicitly owned child processes."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
REFUSAL_PORT = 59999
RUNNERS = (
    ("browser_task_execution_panel.py", "/tests/task_execution_panel_harness.html"),
    ("browser_agent_workspace_task_create.py", "/tests/agent_workspace_task_create_harness.html"),
    ("browser_chat_subagent_region.py", ""),
    ("browser_chat_composer.py", ""),
    ("browser_managed_chat_question.py", ""),
    ("browser_feishu_provenance.py", ""),
    ("browser_feishu_bot_components.py", "/tests/feishu_bot_components_harness.html"),
    ("browser_chat_queue_activation.py", ""),
    ("browser_chat_goal_steer.py", ""),
    ("browser_chat_image_quality.py", ""),
    ("browser_service_restart.py", ""),
)

# A scenario's entry page cannot be derived from its filename, so the table
# above stays explicit. What must not stay manual is membership: a scenario
# added to the directory and forgotten here would never run, which is how
# three of them sat unexecuted. Discovery is the source of truth for the
# set; the table only supplies each one's entry page.
_DISCOVERED = {
    path.name
    for path in sorted((FRONTEND / "tests").glob("browser_*.py"))
    if path.name != "browser_lifecycle.py"
}
_REGISTERED = {script for script, _ in RUNNERS}
if _DISCOVERED != _REGISTERED:
    missing = sorted(_DISCOVERED - _REGISTERED)
    unknown = sorted(_REGISTERED - _DISCOVERED)
    raise SystemExit(
        "browser scenario registry is out of sync; "
        f"present but unregistered: {missing or 'none'}; "
        f"registered but absent: {unknown or 'none'}"
    )


class Interrupted(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def child_environment(home: Path, temporary: Path) -> dict[str, str]:
    # No provider credentials, Hub settings, proxy or loader options are inherited.
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "TZ", "LD_LIBRARY_PATH")
        if key in os.environ
    }
    env.update(
        {
            "HOME": str(home),
            "TMPDIR": str(temporary),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_STATE_HOME": str(home / "state"),
            "XDG_CACHE_HOME": str(home / "cache"),
            "PYTHONUNBUFFERED": "1",
            "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
            "COREPACK_ENABLE_NETWORK": "0",
            "COREPACK_ENABLE_AUTO_PIN": "0",
        }
    )
    for directory in (home, temporary, home / "config", home / "state", home / "cache"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return env


def stop_owned(process: subprocess.Popen[bytes] | None, first_signal: int) -> dict[str, Any] | None:
    if process is None:
        return None
    errors: list[str] = []
    forced = False
    if process.poll() is None:
        try:
            process.send_signal(first_signal)
        except ProcessLookupError:
            pass
        except OSError as exc:
            errors.append(type(exc).__name__)
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        forced = True
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except OSError as exc:
            errors.append(type(exc).__name__)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            errors.append("owned_process_did_not_exit")
    return {
        "pid": process.pid,
        "exit_code": process.returncode,
        "forced_kill": forced,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True, type=Path)
    parser.add_argument("--browser", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--port", type=int, default=5291)
    parser.add_argument("--runner-timeout", type=float, default=120.0)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.port in {5173, 8173, 10025, 18183, REFUSAL_PORT}:
        parser.error("use a dedicated, non-shared Vite port")
    if not math.isfinite(args.runner_timeout) or args.runner_timeout <= 0:
        parser.error("runner timeout must be finite and positive")
    if not args.node.is_absolute() or not args.browser.is_absolute():
        parser.error("node and browser must be explicit absolute paths")
    node, browser = args.node.resolve(strict=True), args.browser.resolve(strict=True)
    if any(not path.is_file() or not os.access(path, os.X_OK) for path in (node, browser)):
        parser.error("node and browser must already exist and be executable")
    vite_entry = FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"
    if not vite_entry.is_file():
        parser.error("installed frontend dependencies are required; nothing will be installed")

    sys.path.insert(0, str(FRONTEND / "tests"))
    from browser_lifecycle import check_python_tools

    versions = check_python_tools()
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    (output / "runs").mkdir(mode=0o700)
    print(f"Browser mock evidence: {output}", flush=True)
    env = child_environment(output / "tool-home", output / "tool-tmp")
    actual_node = subprocess.run(
        [str(node), "--version"], env=env, capture_output=True, text=True, check=True, timeout=5
    ).stdout.strip()
    expected_node = (ROOT / ".node-test-version").read_text().strip()
    if actual_node != f"v{expected_node}":
        raise RuntimeError(f"use declared Node {expected_node}; found {actual_node}")
    versions["node"] = actual_node

    interrupted: list[int] = []
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def request_stop(signum: int, _frame: Any) -> None:
        if not interrupted:
            interrupted.append(signum)

    for sig in previous:
        signal.signal(sig, request_stop)
    results: list[dict[str, Any]] = []
    error: str | None = None
    vite = active = None
    active_output: Path | None = None
    active_record: dict[str, Any] | None = None
    runner_cleanup = vite_cleanup = None
    vite_port_accepting: bool | None = None
    temporary_root: Path | None = None
    refusal = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    vite_log = (output / "vite.log").open("wb")
    try:
        # Keep browser socket paths short even when the evidence directory is deep.
        temporary_root = Path(tempfile.mkdtemp(prefix="ch-ui-", dir="/tmp"))
        env = child_environment(output / "vite-home", temporary_root / "0")
        refusal.bind(("127.0.0.1", REFUSAL_PORT))
        target = f"http://127.0.0.1:{REFUSAL_PORT}"
        env.update(
            {
                "VITE_API_TARGET": target,
                "VITE_SYSTEM_API_TARGET": target,
                "VITE_PORT": str(args.port),
            }
        )
        nonce = uuid.uuid4().hex
        ready_path = f"/__browser_mocks_ready/{nonce}"
        config = output / "vite.browser-mocks.config.mjs"
        config.write_text(
            "import base from " + json.dumps(str(FRONTEND / "vite.config.ts")) + ";\n"
            "export default { ...base, envDir: false,\n"
            " server: { ...base.server, host: '127.0.0.1', port: "
            + str(args.port)
            + ", strictPort: true },\n"
            " plugins: [...(base.plugins || []), { name: 'browser-mocks-ready', configureServer(server) {\n"
            " server.middlewares.use((req, res, next) => { if (req.url === "
            + json.dumps(ready_path)
            + ") {\n"
            " res.setHeader('Content-Type', 'text/plain'); res.end("
            + json.dumps(nonce)
            + "); return; } next(); });\n"
            " } }] };\n",
            encoding="utf-8",
        )
        vite = subprocess.Popen(
            [
                str(node),
                str(vite_entry),
                "--config",
                str(config),
                "--host",
                "127.0.0.1",
                "--port",
                str(args.port),
                "--strictPort",
            ],
            cwd=FRONTEND,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=vite_log,
            stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{args.port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        deadline = time.monotonic() + 30
        while True:
            if interrupted:
                raise Interrupted("interrupted during Vite startup")
            if vite.poll() is not None:
                raise RuntimeError("owned Vite exited during startup")
            try:
                with opener.open(base + ready_path, timeout=1) as response:
                    if response.status == 200 and response.read(128).decode() == nonce:
                        break
            except (OSError, urllib.error.URLError):
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("owned Vite readiness deadline exceeded")
            time.sleep(0.1)

        for script, suffix in RUNNERS:
            if interrupted:
                raise Interrupted("interrupted before the next runner")
            run_output = output / "runs" / Path(script).stem
            record: dict[str, Any] = {
                "runner": script,
                "result": "FAIL",
                "pid": None,
                "exit_code": None,
            }
            results.append(record)
            active_output, active_record = run_output, record
            run_env = child_environment(
                output / "homes" / Path(script).stem, temporary_root / str(len(results))
            )
            record["tmpdir"] = run_env["TMPDIR"]
            with (output / f"{Path(script).stem}.log").open("wb") as log:
                active = subprocess.Popen(
                    [
                        sys.executable,
                        str(FRONTEND / "tests" / script),
                        base + suffix,
                        str(run_output),
                        str(browser),
                    ],
                    cwd=FRONTEND,
                    env=run_env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                record["pid"] = active.pid
                deadline = time.monotonic() + args.runner_timeout
                while active.poll() is None:
                    if interrupted:
                        raise Interrupted("interrupted during browser runner")
                    if vite.poll() is not None:
                        raise RuntimeError("owned Vite exited while a runner was active")
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"runner deadline exceeded: {script}")
                    time.sleep(0.1)
                record["exit_code"] = active.wait()
                active = None
            lifecycle = json.loads((run_output / "lifecycle.json").read_text())
            if lifecycle.get("runner") != script or lifecycle.get("pid") != record["pid"]:
                raise RuntimeError("runner lifecycle evidence does not match the owned child")
            record["lifecycle"] = lifecycle
            if lifecycle.get("cleanup_complete") is not True:
                raise RuntimeError(
                    "runner cleanup was incomplete; remaining runners were not started"
                )
            if record["exit_code"] == 0 and lifecycle.get("result") == "PASS":
                business = json.loads((run_output / "report.json").read_text())
                if business.get("result") != "PASS":
                    raise RuntimeError("runner did not produce a passing business report")
                record["result"] = "PASS"
        if vite.poll() is not None:
            raise RuntimeError("owned Vite exited before requested shutdown")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        requested_signal = interrupted[0] if interrupted else signal.SIGTERM
        runner_cleanup = stop_owned(active, requested_signal)
        if active is not None and active_record is not None and active_output is not None:
            active_record["exit_code"] = active.returncode
            try:
                lifecycle = json.loads((active_output / "lifecycle.json").read_text())
                if (
                    lifecycle.get("runner") != active_record["runner"]
                    or lifecycle.get("pid") != active_record["pid"]
                ):
                    raise RuntimeError("interrupted runner evidence does not match its child")
                active_record["lifecycle"] = lifecycle
                active_record["result"] = "INTERRUPTED" if interrupted else "FAIL"
            except Exception as exc:
                active_record["lifecycle_error"] = type(exc).__name__
        vite_cleanup = stop_owned(vite, signal.SIGTERM)
        if vite is not None:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                vite_port_accepting = probe.connect_ex(("127.0.0.1", args.port)) == 0
        refusal.close()
        vite_log.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    clean = all(
        item is None
        or (item["exit_code"] is not None and not item["forced_kill"] and not item["errors"])
        for item in (runner_cleanup, vite_cleanup)
    )
    passed = (
        vite_port_accepting is False
        and not interrupted
        and error is None
        and clean
        and len(results) == len(RUNNERS)
        and all(item["result"] == "PASS" for item in results)
    )
    temporary_cleanup = "not_created" if temporary_root is None else "retained"
    if passed:
        assert temporary_root is not None
        try:
            shutil.rmtree(temporary_root)
            temporary_cleanup = "removed"
        except OSError as exc:
            passed = False
            error = f"temporary cleanup failed: {type(exc).__name__}"
    report = {
        "schema": 1,
        "result": "PASS" if passed else "INTERRUPTED" if interrupted else "FAIL",
        "versions": versions,
        "temporary_root": str(temporary_root) if temporary_root else None,
        "temporary_cleanup": temporary_cleanup,
        "vite_tmpdir": str(temporary_root / "0") if temporary_root else None,
        "port": args.port,
        "error": error,
        "runners": results,
        "runner_cleanup": runner_cleanup,
        "vite_cleanup": vite_cleanup,
        "vite_port_accepting_after_cleanup": vite_port_accepting,
        "not_run": [script for script, _ in RUNNERS[len(results) :]],
        "process_tree_exit": "not_verified",
    }
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    return 0 if passed else 128 + interrupted[0] if interrupted else 1


if __name__ == "__main__":
    raise SystemExit(main())
