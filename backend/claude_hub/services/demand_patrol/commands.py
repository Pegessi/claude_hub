"""Injectable, bounded JSON CLI transport; never opens credential files."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

_MAX_OUTPUT_BYTES = 32 * 1024 * 1024
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SENSITIVE_KEY_NAME_RE = re.compile(
    r"(?i)(?:token|secret|password|passwd|pwd|api[_-]?key|app[_-]?key|client[_-]?key"
    r"|private[_-]?key|access[_-]?key|session[_-]?key|auth|authorization|cookie|credential|jwt)"
)
_URL_USERINFO_RE = re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s:@]+(?::[^/\s@]*)?@")
_SENSITIVE_KV_RE = re.compile(
    r"""(?i)['"]?[a-z0-9_.-]*(?:token|secret|password|passwd|pwd|api[_-]?key|app[_-]?key"""
    r"""|client[_-]?key|private[_-]?key|access[_-]?key|session[_-]?key|auth|authorization"""
    r"""|cookie|credential|jwt)[a-z0-9_.-]*['"]?\s*[:=]\s*(?:bearer\s+)?"""
    r"""(?:'[^']*'|"[^"]*"|[^\s,;}\]]+)"""
)
_TOKEN_PREFIX_RE = re.compile(
    r"""(?i)\b(?:bearer\s+[^\s,;"'}\]]+|(?:cli|u|t|sk)-[a-z0-9_-]{6,}"""
    r"""|eyJ[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,})\b"""
)


class CommandError(RuntimeError):
    """A CLI did not return a usable, complete JSON response."""


def _sanitize_error_text(text: str) -> str:
    cleaned = _URL_USERINFO_RE.sub(r"\1[redacted]@", text)
    cleaned = _SENSITIVE_KV_RE.sub("[redacted]", cleaned)
    cleaned = _TOKEN_PREFIX_RE.sub("[redacted]", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned[:240]


def extract_cli_error_detail(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    if "error" not in payload and payload.get("ok") is not False:
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        subtype = error.get("subtype") or error.get("type")
        msg = error.get("message") or error.get("msg")
        parts: list[str] = []
        if (
            isinstance(subtype, str)
            and subtype.strip()
            and _SAFE_CODE_RE.fullmatch(subtype.strip())
            and not _SENSITIVE_KEY_NAME_RE.search(subtype)
        ):
            parts.append(subtype.strip())
        if (
            isinstance(code, (int, str))
            and not isinstance(code, bool)
            and str(code).strip()
            and _SAFE_CODE_RE.fullmatch(str(code).strip())
        ):
            parts.append(f"code={str(code).strip()}")
        header = f"{parts[0]} ({parts[1]})" if len(parts) == 2 else " ".join(parts)
        if isinstance(msg, str) and msg.strip():
            sanitized_msg = _sanitize_error_text(msg.strip())
            detail = f"{header}: {sanitized_msg}" if header else sanitized_msg
        else:
            detail = header
        return _sanitize_error_text(detail)
    if isinstance(error, str) and error.strip():
        return _sanitize_error_text(error)
    if payload.get("ok") is False:
        msg = payload.get("message") or payload.get("msg")
        if isinstance(msg, str) and msg.strip():
            return _sanitize_error_text(msg)
    return ""


def _stream_error_detail(stdout: bytes, stderr: bytes) -> str:
    saw_structured = False
    for raw in (stderr, stdout):
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        saw_structured = True
        detail = extract_cli_error_detail(parsed)
        if detail:
            return detail
    if saw_structured:
        # Unrecognized JSON payloads fail closed rather than echoing raw keys/values.
        return ""
    if stderr:
        text = stderr.decode("utf-8", errors="replace").strip()
        if text:
            first_line = text.splitlines()[0].strip()
            if first_line.startswith(("{", "[")):
                return ""
            return _sanitize_error_text(first_line)
    return ""


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
    env = {
        "INFOWALL_URL": os.environ.get("INFOWALL_URL", "http://127.0.0.1:18899"),
        "INFOWALL_API_KEY_FILE": os.environ.get(
            "INFOWALL_API_KEY_FILE", "/home/tiger/.local/state/infowall/api-key"
        ),
    }
    if urlsplit(env["INFOWALL_URL"]).hostname in {"127.0.0.1", "localhost", "::1"}:
        # The Go CLI also inherits host proxy settings; local primary traffic
        # must remain local regardless of upper/lower-case proxy conventions.
        env.update(NO_PROXY="127.0.0.1,localhost,::1", no_proxy="127.0.0.1,localhost,::1")
    return env


def _resolve_executable(name: str) -> str:
    relative = {
        "infowall": ".local/opt/infowall/current/infowall",
        "lark-cli": ".local/bin/lark-cli",
    }.get(name)
    if relative is None:
        return name
    found = shutil.which(name)
    if found:
        return found
    # Isolated runtime HOME and minimal PATH must not hide host-installed CLIs.
    for installed in (Path.home() / relative, Path("/home/tiger") / relative):
        if installed.is_file() and os.access(installed, os.X_OK):
            return str(installed)
    return name


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
    executable = _resolve_executable(argv[0])
    try:
        proc = await asyncio.create_subprocess_exec(
            executable,
            *argv[1:],
            stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **(env or {})},
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        raise CommandError(f"{argv[0]} is unavailable") from exc
    reader: asyncio.Task[bytes] | None = None
    stderr_reader: asyncio.Task[bytes] | None = None
    try:
        async with asyncio.timeout(timeout):
            assert proc.stdout is not None
            reader = asyncio.create_task(_read_bounded(proc.stdout))
            stderr_stream = getattr(proc, "stderr", None)
            if stderr_stream is not None:
                stderr_reader = asyncio.create_task(_read_bounded(stderr_stream))
            if input_text is not None:
                assert proc.stdin is not None
                proc.stdin.write(input_text.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            stdout = await reader
            stderr = await stderr_reader if stderr_reader is not None else b""
            await proc.wait()
        if proc.returncode != 0:
            detail = _stream_error_detail(stdout, stderr)
            suffix = f": {detail}" if detail else ""
            raise CommandError(f"{argv[0]} exited with status {proc.returncode}{suffix}")
        try:
            return json.loads(stdout)
        except (ValueError, UnicodeError) as exc:
            raise CommandError(f"{argv[0]} returned invalid JSON") from exc
    except TimeoutError as exc:
        raise CommandError(f"{argv[0]} timed out after {timeout:g}s") from exc
    finally:
        for task in (reader, stderr_reader):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        # Wrappers may leave descendants holding stdout open. Only this owned
        # session is killed; killing the direct PID alone cannot bound proc.wait().
        try:
            if proc.returncode is None:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            # A paused, unread stdout pipe can delay wait() after output overflow.
            # Cleanup failure must not replace the primary budget/CLI exception.
            pass
        finally:
            # Process has no public close(); close its pipes while the loop is
            # still alive instead of leaving transport destruction to GC.
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                transport.close()
