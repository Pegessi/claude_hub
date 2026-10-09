"""Lifecycle support for the five explicitly selected mocked browser runners."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import platform
import signal
import sys
import tomllib
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, ViewportSize, async_playwright

ROOT = Path(__file__).resolve().parents[2]
_CLOSE_TIMEOUT_S = 5.0


def check_python_tools() -> dict[str, str]:
    expected_python = (ROOT / ".python-test-version").read_text().strip()
    actual_python = platform.python_version()
    if actual_python != expected_python:
        raise RuntimeError(f"use declared Python {expected_python}; found {actual_python}")
    lock = tomllib.loads((ROOT / "backend" / "uv.lock").read_text())
    versions = {
        package["version"] for package in lock["package"] if package["name"] == "playwright"
    }
    if len(versions) != 1:
        raise RuntimeError("backend/uv.lock must declare exactly one Playwright version")
    actual_playwright = importlib.metadata.version("playwright")
    if actual_playwright != next(iter(versions)):
        raise RuntimeError("installed Playwright does not match backend/uv.lock")
    return {"python": actual_python, "playwright": actual_playwright}


class BrowserRun:
    def __init__(self, output: Path, executable: Path) -> None:
        self.output = output
        self.executable = executable
        self.signal_number: int | None = None
        self.cancelled = False
        self.used = False
        self.cleanup: list[dict[str, str]] = []
        self._cleanup_tasks: list[asyncio.Task[Any]] = []

    async def _close(self, name: str, operation: Coroutine[Any, Any, Any] | None) -> None:
        if operation is None:
            self.cleanup.append({"step": name, "state": "not_acquired"})
            return
        closing = operation

        async def close_operation() -> BaseException | None:
            try:
                await closing
            except BaseException as exc:
                return exc
            return None

        task = asyncio.create_task(close_operation(), name=f"browser-close-{name}")
        self._cleanup_tasks.append(task)
        deadline = asyncio.get_running_loop().time() + _CLOSE_TIMEOUT_S
        while not task.done():
            try:
                done, _ = await asyncio.wait(
                    {task}, timeout=max(0.0, deadline - asyncio.get_running_loop().time())
                )
            except asyncio.CancelledError:
                # A first signal during cleanup must not skip browser/driver close.
                self.cancelled = True
                continue
            if not done:
                task.cancel()
                self.cleanup.append({"step": name, "state": "timed_out"})
                return
        try:
            failure = task.result()
        except BaseException as exc:
            failure = exc
        if failure is not None:
            self.cleanup.append(
                {"step": name, "state": "error", "error_type": type(failure).__name__}
            )
        else:
            self.cleanup.append({"step": name, "state": "closed"})

    @property
    def cleanup_complete(self) -> bool:
        return all(item["state"] in {"closed", "not_acquired"} for item in self.cleanup) and all(
            task.done() for task in self._cleanup_tasks
        )

    @asynccontextmanager
    async def context(
        self, *, viewport: ViewportSize, launch_args: list[str] | None = None
    ) -> AsyncIterator[BrowserContext]:
        if self.used:
            raise RuntimeError("each browser runner must own exactly one browser context")
        self.used = True
        manager = async_playwright()
        browser = context = None
        body_failed = False
        try:
            playwright = await manager.start()
            browser = await playwright.chromium.launch(
                headless=True, executable_path=str(self.executable), args=launch_args or []
            )
            context = await browser.new_context(viewport=viewport, service_workers="block")
            yield context
        except BaseException:
            body_failed = True
            raise
        finally:
            await self._close("context", context.close() if context is not None else None)
            await self._close("browser", browser.close() if browser is not None else None)
            # Attempt driver exit even if startup did not return a driver object.
            await self._close("driver", manager.__aexit__(None, None, None))
            for task in self._cleanup_tasks:
                if task.done() and not task.cancelled():
                    task.exception()
            if not body_failed:
                if self.cancelled or self.signal_number is not None:
                    raise asyncio.CancelledError()
                if not self.cleanup_complete:
                    raise RuntimeError("browser resource cleanup was incomplete")


async def _execute(
    main: Callable[[BrowserRun], Awaitable[None]], run: BrowserRun, versions: dict[str, str]
) -> int:
    loop = asyncio.get_running_loop()
    failure: BaseException | None = None

    async def scenario() -> None:
        nonlocal failure
        try:
            await main(run)
        except BaseException as exc:
            failure = exc

    task = asyncio.create_task(scenario(), name="browser-scenario")
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def interrupt(sig: int) -> None:
        if run.signal_number is None:
            run.signal_number = sig
            task.cancel()

    for sig in previous:
        loop.add_signal_handler(sig, interrupt, sig)
    try:
        await task
    except BaseException as exc:
        failure = exc
    finally:
        for sig, handler in previous.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)

    interrupted = (
        run.signal_number is not None
        or run.cancelled
        or isinstance(failure, asyncio.CancelledError)
    )
    passed = failure is None and not interrupted and run.used and run.cleanup_complete
    result = "PASS" if passed else "INTERRUPTED" if interrupted else "FAIL"
    exit_code = 0 if passed else 128 + run.signal_number if run.signal_number else 1
    report = {
        "schema": 1,
        "runner": Path(sys.argv[0]).name,
        "pid": os.getpid(),
        "result": result,
        "exit_code": exit_code,
        "signal": run.signal_number,
        "versions": versions,
        "error_type": type(failure).__name__ if failure is not None else None,
        "cleanup": run.cleanup,
        "cleanup_complete": run.cleanup_complete,
        "process_tree_exit": "not_verified",
    }
    run.output.mkdir(mode=0o700, exist_ok=True)
    temporary = run.output / "lifecycle.json.tmp"
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(run.output / "lifecycle.json")
    print(json.dumps(report), flush=True)
    if failure is not None and not isinstance(failure, asyncio.CancelledError):
        traceback.print_exception(failure)
    return exit_code


def run_browser_main(main: Callable[[BrowserRun], Awaitable[None]]) -> None:
    if len(sys.argv) != 4:
        raise SystemExit("usage: RUNNER OWNED_LOOPBACK_URL NEW_OUTPUT_DIR EXISTING_BROWSER")
    versions = check_python_tools()
    executable = Path(sys.argv[3])
    if not executable.is_absolute():
        raise SystemExit("browser executable must be an explicit absolute path")
    executable = executable.resolve(strict=True)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise SystemExit("an existing executable browser is required; no downloads are performed")
    output = Path(sys.argv[2]).resolve()
    if output.exists() or not output.parent.is_dir():
        raise SystemExit("output must be a new directory under an existing owned parent")
    raise SystemExit(asyncio.run(_execute(main, BrowserRun(output, executable), versions)))
