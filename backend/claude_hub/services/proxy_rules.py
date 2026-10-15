"""Shared proxy-selection rules for outbound Hub HTTP traffic.

This module owns one decision: given a target URL and the proxy settings visible
in an environment, must the request go through a proxy and which one. Callers
apply that decision themselves -- the native provider selector builds subprocess
environment variables, while in-process clients build HTTPX clients -- so this
module never mutates :data:`os.environ` and never performs I/O.

It also owns the HTTPX async client factory for in-process callers. HTTPX 0.28
translates every ``NO_PROXY`` entry into a URL pattern and raises
``httpx.InvalidURL`` while the client is being constructed when an entry is a
CIDR block it cannot express as a URL, such as the ``fe80::/10`` and ``fd00::/8``
entries that corporate environments commonly set. Resolving the proxy here and
handing HTTPX an explicit answer keeps that parser out of the path.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Mapping, Optional, Tuple
from urllib.parse import SplitResult, urlsplit
from urllib.request import getproxies

import httpx


def bypasses_proxy(endpoint: SplitResult, no_proxy: Optional[str]) -> bool:
    """Report whether a ``NO_PROXY`` list exempts ``endpoint`` from proxying."""
    if not no_proxy:
        return False
    host = (endpoint.hostname or "").rstrip(".").casefold()
    port = endpoint.port or (443 if endpoint.scheme == "https" else 80)
    for raw_entry in no_proxy.split(","):
        entry = raw_entry.strip().casefold()
        if not entry:
            continue
        if entry == "*":
            return True
        entry_host, entry_port = _split_no_proxy_entry(entry)
        if entry_port is not None and entry_port != port:
            continue
        try:
            if "/" in entry_host and ipaddress.ip_address(host) in ipaddress.ip_network(
                entry_host, strict=False
            ):
                return True
        except ValueError:
            pass
        normalized = entry_host.removeprefix("*.").lstrip(".").rstrip(".")
        if normalized and (host == normalized or host.endswith(f".{normalized}")):
            return True
    return False


def _split_no_proxy_entry(entry: str) -> Tuple[str, Optional[int]]:
    if entry.startswith("["):
        closing = entry.find("]")
        if closing != -1:
            host = entry[1:closing]
            suffix = entry[closing + 1 :]
            if suffix.startswith(":") and suffix[1:].isdigit():
                return host, int(suffix[1:])
            return host, None
    if entry.count(":") == 1:
        host, raw_port = entry.rsplit(":", 1)
        if raw_port.isdigit():
            return host, int(raw_port)
    return entry, None


def environment_proxy_for_url(
    url: str, proxies: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    """Resolve the proxy for ``url``, or ``None`` when it must be reached directly.

    ``proxies`` is keyed like :func:`urllib.request.getproxies` (``http``,
    ``https``, ``all``, ``no``), the same mapping HTTPX reads from the
    environment, so an explicit mapping keeps tests away from process state.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("proxy selection requires an absolute HTTP or HTTPS URL")
    available = getproxies() if proxies is None else dict(proxies)
    if bypasses_proxy(parsed, available.get("no")):
        return None
    candidate = available.get(parsed.scheme) or available.get("all")
    selected = candidate.strip() if isinstance(candidate, str) else ""
    return selected or None


def create_async_client(api_base_url: str, *, timeout: float) -> httpx.AsyncClient:
    """Create an async client for ``api_base_url`` without HTTPX reading ``NO_PROXY``.

    Raises :class:`ValueError` when ``api_base_url`` is not an absolute HTTP or
    HTTPS URL, because the proxy decision has no host to match otherwise.
    """
    proxy = environment_proxy_for_url(api_base_url)
    options: dict[str, Any] = {
        "base_url": api_base_url,
        "timeout": timeout,
        "trust_env": True,
    }
    if proxy is not None:
        # An explicit proxy replaces HTTPX's environment mounts entirely. Every
        # request from this client targets the origin the decision was made for.
        options["proxy"] = proxy
    else:
        # Without a proxy argument only an explicit transport suppresses those
        # mounts; its own trust_env keeps SSL_CERT_FILE and SSL_CERT_DIR working.
        options["transport"] = httpx.AsyncHTTPTransport(trust_env=True)
    return httpx.AsyncClient(**options)
