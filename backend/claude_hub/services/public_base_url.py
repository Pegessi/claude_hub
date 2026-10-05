"""Resolve the canonical public origin used in externally configured URLs."""

from __future__ import annotations

import os
import urllib.parse

from claude_hub.config import settings

_PUBLIC_URL_ENV = "CLAUDE_HUB_PUBLIC_BASE_URL"
_PROVIDER_URL_ENV = "CLAUDE_HUB_PROVIDER_PUBLIC_URL"


def _validated_origin(value: str, source: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{source} must be an absolute http/https origin")
    if parsed.username or parsed.password:
        raise ValueError(f"{source} must not contain credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(f"{source} must not contain a path, query, or fragment")
    return candidate


def get_public_base_url() -> str:
    """Return the validated canonical origin for public Hub links.

    Resolution is intentionally independent of request headers: externally
    supplied Host and forwarding headers must not influence Bot callback URLs.
    """

    for env_name in (_PUBLIC_URL_ENV, _PROVIDER_URL_ENV):
        value = os.environ.get(env_name)
        if value:
            return _validated_origin(value, env_name)

    host = settings.host.strip()
    if host in {"0.0.0.0", "::", "[::]", ""}:
        host = "127.0.0.1"
    elif ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return _validated_origin(f"http://{host}:{settings.port}", "configured host and port")
