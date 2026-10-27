"""Trust boundary for reverse-proxy request headers.

This is the auth-hardening branch's self-contained copy of the *stable trust
primitives* shared with the ``codex/rv-proxy-trust`` branch. That branch owns
the full module — including the browser Origin / unsafe-HTTP policy and the
``CLAUDE_HUB_ALLOWED_ORIGINS`` setting — and its file is the superset that
wins when the two branches integrate. Everything here is intentionally
limited to what cookie/security decisions need, and the function bodies are
kept byte-identical so the union merge is trivial.

Reverse proxies (nginx, ttyd front-ends, tunnels) append
``X-Forwarded-For`` and ``X-Forwarded-Proto``. Those headers are fully
attacker-controlled unless the *direct* TCP peer is a trusted proxy. Every
trust decision here therefore keys off the direct connection peer
(``connection.client.host``) and never off header contents alone. A client
connected directly cannot make itself look like loopback by sending
``X-Forwarded-For: 127.0.0.1``, and a remote peer cannot flip the effective
scheme to HTTPS with ``X-Forwarded-Proto``.
"""

import ipaddress
from typing import Optional, Sequence, Union

from starlette.requests import HTTPConnection

from ..config import settings

_IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
_IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

# Origin/scheme values are http/https only.
_ORIGIN_SCHEME_PORTS = {"http": 80, "https": 443}


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
