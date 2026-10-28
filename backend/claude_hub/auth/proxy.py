"""Trust boundaries for reverse-proxy headers and browser requests.

Reverse proxies (nginx, ttyd front-ends, tunnels) append ``X-Forwarded-For``
and ``X-Forwarded-Proto``. Those headers are fully attacker-controlled unless
the *direct* TCP peer is a trusted proxy. Every trust decision here therefore
keys off the direct connection peer (``connection.client.host``) and never
off header contents alone. A client connected directly cannot make itself
look like loopback by sending ``X-Forwarded-For: 127.0.0.1``.

The browser Origin check closes two cross-site holes:

- WebSocket hijacking (CSWSH): browser pages always send ``Origin`` on the
  upgrade and it must match the destination ``Host`` (or an explicitly
  allowed origin); native clients that send no ``Origin`` (ttyd, websocat,
  curl) keep working.
- State-changing HTTP requests: cross-site "simple" POSTs/PUTs/PATCHes/
  DELETEs execute even though CORS blocks reading their responses, so unsafe
  methods with a browser ``Origin`` are held to the same same-origin policy.
  Safe methods (GET/HEAD/OPTIONS) and Origin-less native clients are exempt.
"""

import ipaddress
import logging
from typing import Optional, Sequence, Union
from urllib.parse import urlsplit

from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket

from ..config import settings

logger = logging.getLogger(__name__)

_IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
_IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

# Origin carries http/https only (browsers never send ws/wss in Origin).
_ORIGIN_SCHEME_PORTS = {"http": 80, "https": 443}
# Host headers without an explicit port are compared against default ports.
_DEFAULT_HOST_PORTS = frozenset((80, 443))
# Methods that can change state and therefore require a same-origin browser
# request. CORS preflight (OPTIONS) and safe reads are verified by CORS alone.
UNSAFE_HTTP_METHODS = frozenset(("POST", "PUT", "PATCH", "DELETE"))


def _normalize_ip(ip: _IPAddress) -> _IPAddress:
    """Collapse IPv4-mapped IPv6 addresses (``::ffff:203.0.113.5``) to v4.

    Proxies and dual-stack sockets may report the same peer either way;
    without normalization a v4 allowlist entry would never match a mapped v6
    representation.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def _ip_matches_networks(value: str, networks: Sequence[_IPNetwork]) -> bool:
    """True when ``value`` is a valid IP contained in one of ``networks``."""
    try:
        ip = _normalize_ip(ipaddress.ip_address(value.strip()))
    except (ValueError, AttributeError):
        return False
    return any(ip in network for network in networks)


def _resolve_networks(
    trusted_networks: Optional[Sequence[_IPNetwork]],
) -> Sequence[_IPNetwork]:
    return settings.trusted_proxy_networks if trusted_networks is None else trusted_networks


def direct_peer_is_trusted(
    connection: HTTPConnection,
    trusted_networks: Optional[Sequence[_IPNetwork]] = None,
) -> bool:
    """Whether the direct connection peer is a configured trusted proxy."""
    if connection.client is None:
        return False
    return _ip_matches_networks(connection.client.host, _resolve_networks(trusted_networks))


def client_ip_from_request(
    connection: HTTPConnection,
    trusted_networks: Optional[Sequence[_IPNetwork]] = None,
) -> str:
    """Return the client IP, honoring XFF only from a trusted direct peer.

    When the direct peer is untrusted, ``X-Forwarded-For`` is ignored
    completely and the direct peer address is returned. When the direct peer
    is trusted, the chain is walked right-to-left: the right-most hop is the
    peer the trusted proxy saw; each address that is itself trusted is
    stripped, and the left-most untrusted address is the client. If every
    hop is trusted, the left-most entry is used. Malformed entries are
    ignored rather than trusted or fatal.
    """
    networks = _resolve_networks(trusted_networks)
    if connection.client is None:
        # Preserve the historical fallback for transports without a peer.
        return "127.0.0.1"

    direct = connection.client.host
    if not _ip_matches_networks(direct, networks):
        return direct

    forwarded_for = connection.headers.get("x-forwarded-for")
    if not forwarded_for:
        return direct

    hops: list[_IPAddress] = []
    for token in forwarded_for.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            hops.append(_normalize_ip(ipaddress.ip_address(token)))
        except ValueError:
            # Garbage entry: never trusted, but stripping it here would let a
            # client inject one to stop the chain walk at a forged address.
            # Skip it and keep walking toward the client.
            continue

    for hop in reversed(hops):
        if not any(hop in network for network in networks):
            return str(hop)
    return str(hops[0]) if hops else direct


def scheme_from_request(
    connection: HTTPConnection,
    trusted_networks: Optional[Sequence[_IPNetwork]] = None,
) -> str:
    """Return the effective request scheme.

    Honors the first ``X-Forwarded-Proto`` token only when the direct peer is
    trusted and the value is ``http``/``https``; otherwise falls back to the
    connection scheme. This is the single function cookie Secure decisions
    (e.g. ``_use_secure_cookie``) should use behind a reverse proxy.
    """
    networks = _resolve_networks(trusted_networks)
    if connection.client is not None and _ip_matches_networks(connection.client.host, networks):
        forwarded_proto = connection.headers.get("x-forwarded-proto")
        if forwarded_proto:
            candidate = forwarded_proto.split(",", 1)[0].strip().lower()
            if candidate in _ORIGIN_SCHEME_PORTS:
                return candidate
    return connection.url.scheme


def _parse_origin(origin: str) -> Optional[tuple[str, str, int]]:
    """Split an Origin/URL into (scheme, hostname, effective port)."""
    try:
        parts = urlsplit(origin.strip())
        # urllib validates the port lazily, when the property is read.
        hostname, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme not in _ORIGIN_SCHEME_PORTS or not hostname:
        return None
    if port is None:
        port = _ORIGIN_SCHEME_PORTS[parts.scheme]
    return parts.scheme, hostname.lower(), port


def _parse_host_target(host: str) -> Optional[tuple[str, Optional[int]]]:
    """Split a Host header / bare allowed entry into (hostname, port|None).

    Understands ``name``, ``name:port``, ``[::1]`` and ``[::1]:port``.
    """
    try:
        parts = urlsplit("//" + host.strip())
        hostname, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not hostname:
        return None
    return hostname.lower(), port


def _host_target_matches(hostname: str, port: int, host_target: tuple[str, Optional[int]]) -> bool:
    host_name, host_port = host_target
    if host_name != hostname:
        return False
    if host_port is None:
        # Browsers omit the default port from Host even when Origin carries
        # the scheme default explicitly.
        return port in _DEFAULT_HOST_PORTS
    return host_port == port


def browser_origin_is_allowed(
    *,
    origin: Optional[str],
    host: Optional[str],
    allowed_origins: Sequence[str] = (),
) -> bool:
    """Decide whether a browser ``Origin`` is acceptable for this target.

    Shared by the WebSocket handshake and the unsafe-HTTP-write guard.

    - No ``Origin`` header (native clients) is always allowed.
    - A parseable Origin whose host[:port] equals ``Host`` is allowed.
    - An Origin listed in ``allowed_origins`` (full origin or bare
      host[:port]) is allowed.
    - Any other present/malformed Origin is rejected.
    """
    if origin is None or not origin.strip():
        return True

    target = _parse_origin(origin)
    if target is None:
        return False
    scheme, hostname, port = target

    if host:
        host_target = _parse_host_target(host)
        if host_target is not None and _host_target_matches(hostname, port, host_target):
            return True

    for entry in allowed_origins:
        entry = entry.strip()
        if not entry:
            continue
        if "://" in entry:
            parsed = _parse_origin(entry)
            if parsed is not None and parsed == (scheme, hostname, port):
                return True
        else:
            host_entry = _parse_host_target(entry)
            if host_entry is not None and _host_target_matches(hostname, port, host_entry):
                return True
    return False


def _scope_header(headers: Sequence[tuple[bytes, bytes]], name: str) -> Optional[str]:
    """Read a lowercase header name from raw ASGI scope headers."""
    raw_name = name.encode()
    for key, value in headers:
        if key == raw_name:
            return value.decode("latin-1")
    return None


def unsafe_http_request_is_allowed(
    *,
    method: str,
    headers: Sequence[tuple[bytes, bytes]],
    allowed_origins: Optional[Sequence[str]] = None,
) -> bool:
    """Same-origin policy for state-changing HTTP requests.

    Safe methods and requests without an ``Origin`` (curl, the CLI,
    server-to-server webhooks) pass; an unsafe method carrying a browser
    ``Origin`` must satisfy the same Host/allowlist match as WebSockets.
    """
    if method.upper() not in UNSAFE_HTTP_METHODS:
        return True
    origin = _scope_header(headers, "origin")
    if origin is None or not origin.strip():
        return True
    return browser_origin_is_allowed(
        origin=origin,
        host=_scope_header(headers, "host"),
        allowed_origins=(
            settings.allowed_origins_list if allowed_origins is None else allowed_origins
        ),
    )


async def reject_bad_websocket_origin(connection: WebSocket) -> bool:
    """Deny a browser cross-site WebSocket handshake before it is accepted.

    Returns ``True`` after sending a pre-accept close (code 1008) when the
    handshake must be rejected; callers ``return`` immediately in that case.
    Starlette/uvicorn translate a pre-accept ``websocket.close`` into an
    HTTP 403 handshake denial. Returns ``False`` when the handshake may
    proceed.
    """
    origin = connection.headers.get("origin")
    if origin is None:
        return False
    host = connection.headers.get("host")
    if browser_origin_is_allowed(
        origin=origin,
        host=host,
        allowed_origins=settings.allowed_origins_list,
    ):
        return False
    logger.warning(
        "Rejecting WebSocket handshake with Origin %r targeting Host %r",
        origin,
        host,
    )
    await connection.close(code=1008)
    return True
