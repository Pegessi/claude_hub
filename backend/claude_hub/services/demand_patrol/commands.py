"""Injectable, bounded JSON CLI transport; never opens credential files."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from pathlib import Path
from typing import Any, Protocol

_MAX_OUTPUT_BYTES = 4 * 1024 * 1024


class CommandError(RuntimeError):
    """A CLI did not return a usable, complete JSON response."""


class CommandRunner(Protocol):
    async def __call__(
        self,
        argv: list[str],
        *,
        timeout: float = 30,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> Any: ...


def infowall_env() -> dict[str, str]:
    return {
        "INFOWALL_URL": os.environ.get("INFOWALL_URL", "http://127.0.0.1:18899"),
        "INFOWALL_API_KEY_FILE": os.environ.get(
            "INFOWALL_API_KEY_FILE", "/home/tiger/.local/state/infowall/api-key"
        ),
    }


async def _read_bounded(stream: asyncio.StreamReader) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := await stream.read(65536):
        size += len(chunk)
        if size > _MAX_OUTPUT_BYTES:
            raise CommandError("CLI response exceeds patrol output budget")
        chunks.append(chunk)
    return b"".join(chunks)


async def run_command(
    argv: list[str],
    *,
    timeout: float = 30,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> Any:
    if not argv:
        raise ValueError("CLI argv must not be empty")
    executable = argv[0]
    if executable == "infowall" and not shutil.which(executable):
        installed = Path.home() / ".local/opt/infowall/current/infowall"
        if installed.is_file():
            executable = str(installed)
    try:
        proc = await asyncio.create_subprocess_exec(
            executable,
            *argv[1:],
            stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, **(env or {})},
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        raise CommandError(f"{argv[0]} is unavailable") from exc
    reader: asyncio.Task[bytes] | None = None
    try:
        async with asyncio.timeout(timeout):
            assert proc.stdout is not None
            reader = asyncio.create_task(_read_bounded(proc.stdout))
            if input_text is not None:
                assert proc.stdin is not None
                proc.stdin.write(input_text.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            stdout = await reader
            await proc.wait()
        if proc.returncode != 0:
            # CLI stderr may contain tokens or raw private message bodies.
            raise CommandError(f"{argv[0]} exited with status {proc.returncode}")
        try:
            return json.loads(stdout)
        except (ValueError, UnicodeError) as exc:
            raise CommandError(f"{argv[0]} returned invalid JSON") from exc
    except TimeoutError as exc:
        raise CommandError(f"{argv[0]} timed out after {timeout:g}s") from exc
    finally:
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        # Wrappers may leave descendants holding stdout open. Only this owned
        # session is killed; killing the direct PID alone cannot bound proc.wait().
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            elif proc.returncode is None:
                proc.kill()
        except ProcessLookupError:
            pass
        await asyncio.wait_for(proc.wait(), timeout=5)
