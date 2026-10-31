"""Startup guard against exposing an unauthenticated Hub.

With Feishu OAuth disabled the API trusts every local-network peer and skips
authentication. Binding such an instance to a wildcard, LAN or public
interface exposes unauthenticated terminal/agent control to anyone who can
reach the port. The decision is a pure function of the configured bind host
(:func:`evaluate_listen_host`) so it can be tested without binding a socket;
:func:`enforce_listen_policy` combines that verdict with the auth and
opt-in settings at every launch path.

Link-local interfaces (169.254.0.0/16, fe80::/10) are treated like loopback:
the existing ``Settings.is_local_network_ip`` auth exemption already trusts
them, and they are not routable beyond the local segment.
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass

from ..config import settings

logger = logging.getLogger(__name__)

# Names that unambiguously identify the local machine.
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})
# Wildcard "all interfaces" spellings accepted by uvicorn / common shells.
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "*"})

_INSECURE_LISTEN_ENV = "CLAUDE_HUB_ALLOW_INSECURE_LISTEN"


@dataclass(frozen=True)
class ListenVerdict:
    """Whether binding ``host`` stays on a local-only interface."""

    allowed: bool
    reason: str
    detail: str


def evaluate_listen_host(host: str | None) -> ListenVerdict:
    """Classify a bind host without DNS lookups or opening a socket.

    Reasons: ``loopback`` / ``link-local`` are allowed; ``wildcard``,
    ``non-local-ip``, ``hostname`` and ``empty`` are refused for an
    unauthenticated server. A hostname can never be proven local without a
    resolver (and resolver answers are network-dependent), so non-loopback
    names conservatively count as exposed.
    """
    if host is None or not host.strip():
        return ListenVerdict(
            False,
            "empty",
            "No bind host was configured, so its exposure scope is unknown.",
        )

    raw = host.strip().lower()

    if raw in _LOOPBACK_HOSTNAMES:
        return ListenVerdict(True, "loopback", f"{host!r} resolves to the local machine only.")

    if raw in _WILDCARD_HOSTS:
        return ListenVerdict(
            False,
            "wildcard",
            f"{host!r} listens on all interfaces, including non-local ones.",
        )

    ip_literal = raw[1:-1] if raw.startswith("[") and raw.endswith("]") else raw
    try:
        ip = ipaddress.ip_address(ip_literal)
    except ValueError:
        return ListenVerdict(
            False,
            "hostname",
            f"{host!r} is a hostname; non-loopback names may resolve to a " "non-local interface.",
        )

    if ip.is_loopback:
        return ListenVerdict(True, "loopback", f"{host!r} is a loopback address.")
    if ip.is_link_local:
        return ListenVerdict(
            True,
            "link-local",
            f"{host!r} is a link-local address, not routable beyond the segment.",
        )
    return ListenVerdict(
        False,
        "non-local-ip",
        f"{host!r} is reachable beyond loopback and link-local interfaces.",
    )


class InsecureListenError(RuntimeError):
    """Raised when an unauthenticated server would bind beyond local links."""


def _refusal_message(host: str | None, verdict: ListenVerdict) -> str:
    return (
        "Refusing to start: authentication is disabled "
        "(FEISHU_APP_ID/FEISHU_APP_SECRET are not configured), but the Hub is "
        f"configured to listen on {host!r}, which is not loopback or "
        f"link-local. {verdict.detail} To fix this, either configure Feishu "
        "OAuth (FEISHU_APP_ID + FEISHU_APP_SECRET, optionally "
        "AUTH_ALLOWED_EMAILS/AUTH_ALLOWED_OPEN_IDS), bind a loopback address "
        "(e.g. --host 127.0.0.1), or explicitly accept the exposure with "
        f"{_INSECURE_LISTEN_ENV}=1."
    )


def enforce_listen_policy(
    host: str | None,
    *,
    auth_enabled: bool,
    allow_insecure: bool,
) -> ListenVerdict:
    """Raise :class:`InsecureListenError` for an unsafe unauthenticated bind.

    An authenticated server may listen anywhere; loopback and link-local are
    always safe; an explicit opt-in passes (with a warning) for deliberately
    exposed single-user/LAN deployments.
    """
    verdict = evaluate_listen_host(host)
    if verdict.allowed:
        return verdict
    if auth_enabled:
        return verdict
    if allow_insecure:
        logger.warning(
            "Authentication is disabled but listening on %r was explicitly " "allowed via %s=1.",
            host,
            _INSECURE_LISTEN_ENV,
        )
        return verdict
    raise InsecureListenError(_refusal_message(host, verdict))


def enforce_settings_listen_policy(host: str | None = None) -> ListenVerdict:
    """Apply the policy using process settings and the effective bind host."""
    effective_host = host if host is not None else (settings.bind_host or settings.host)
    return enforce_listen_policy(
        effective_host,
        auth_enabled=settings.auth_enabled,
        allow_insecure=settings.allow_insecure_listen,
    )
