"""Bounded subprocess and HTTP network probes with explicit timeouts."""

from __future__ import annotations

import asyncio
import shutil
import socket
import ssl
from typing import Mapping, Optional

import httpx

from .network_config import _parse_endpoint, _probe_url
from .network_types import ProbeResult


async def _codex_login_status(env: Mapping[str, str], *, timeout_seconds: float) -> Optional[str]:
    executable = shutil.which("codex", path=env.get("PATH"))
    if executable is None:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "login",
            "status",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env),
        )
    except (OSError, NotImplementedError):
        return None
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.CancelledError:
        await _kill_process(process)
        raise
    except (asyncio.TimeoutError, OSError):
        await _kill_process(process)
        return None
    if process.returncode != 0:
        return None
    # The official command reports only the login mechanism. Never return or
    # log unrecognized output because future versions could add account data.
    output = (stdout + stderr)[:4096].decode("utf-8", errors="ignore").casefold()
    if "logged in using chatgpt" in output:
        return "chatgpt"
    if "logged in using" in output and "api key" in output:
        return "api_key"
    return None


async def _kill_process(process: asyncio.subprocess.Process) -> None:
    try:
        process.kill()
    except OSError:
        pass
    try:
        await process.wait()
    except OSError:
        pass


async def _probe_endpoint(
    endpoint: str, proxy_url: Optional[str], *, timeout_seconds: float
) -> ProbeResult:
    parsed = _parse_endpoint(endpoint)
    timeout = httpx.Timeout(timeout_seconds)
    try:
        async with httpx.AsyncClient(
            proxy=proxy_url,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = await asyncio.wait_for(
                client.head(
                    _probe_url(parsed),
                    headers={"User-Agent": "claude-hub-network-preflight/1"},
                ),
                timeout=timeout_seconds,
            )
    except (httpx.TimeoutException, asyncio.TimeoutError):
        return ProbeResult(False, "timeout")
    except httpx.ProxyError as exc:
        return ProbeResult(False, "proxy-auth" if "407" in str(exc) else "proxy-connect")
    except httpx.ConnectError as exc:
        return ProbeResult(False, _connect_failure_phase(exc))
    except (httpx.TransportError, ssl.SSLError):
        return ProbeResult(False, "transport")
    except (ValueError, ImportError):
        return ProbeResult(False, "proxy-config" if proxy_url else "configuration")
    if proxy_url:
        if response.status_code == 407:
            return ProbeResult(False, "proxy-auth", response.status_code)
        if 300 <= response.status_code < 400:
            return ProbeResult(False, "proxy-redirect", response.status_code)
        if 500 <= response.status_code < 600:
            return ProbeResult(False, "proxy-gateway", response.status_code)
    return ProbeResult(True, "http", response.status_code)


def _connect_failure_phase(exc: BaseException) -> str:
    current: Optional[BaseException] = exc
    while current is not None:
        if isinstance(current, (ssl.SSLError, ssl.CertificateError)):
            return "tls"
        if isinstance(current, socket.gaierror):
            return "dns"
        current = current.__cause__ or current.__context__
    return "tcp"
