"""Provider network value types and default configuration constants."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, Optional

logger = logging.getLogger("claude_hub.services.agent_stream.provider_network")


PROVIDER_NETWORK_MODE_ENV = "CLAUDE_HUB_PROVIDER_NETWORK_MODE"


PROXY_ENV_NAMES = (
    "http_proxy",
    "HTTP_PROXY",
    "https_proxy",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
)


_ALTERNATE_CLAUDE_MODES = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)


_DEFAULT_ENDPOINTS = {
    "cursor": "https://api2.cursor.sh",
}


_ENDPOINT_ENV_NAMES = {
    "claude": ("ANTHROPIC_BASE_URL",),
    "cursor": ("CURSOR_API_ENDPOINT",),
    # TRAE_API_BASE_URL is reported by the installed TraeX CLI. Codex does not
    # document an environment base-URL override, so its effective config must
    # supply the endpoint instead of guessing from an auth mode.
    "traex": ("TRAE_API_BASE_URL",),
}


_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on", "y"})


_VALID_MODES = frozenset({"auto", "direct", "proxy", "inherit"})


_SUCCESS_TTL_SECONDS = 30.0


_FAILURE_TTL_SECONDS = 5.0


_PROBE_TIMEOUT_SECONDS = 3.0


_LOGIN_STATUS_TIMEOUT_SECONDS = 3.0


_LOGIN_STATUS_TTL_SECONDS = 300.0


_MAX_CONCURRENT_PROBES = 8


_MAX_CACHE_ENTRIES = 256


@dataclass(frozen=True)
class ProbeResult:
    """Sanitized result of one route probe."""

    reachable: bool
    phase: str
    status_code: Optional[int] = None


@dataclass(frozen=True)
class _Selection:
    route: str
    direct: Optional[ProbeResult] = None
    proxy: Optional[ProbeResult] = None


ProbeCallable = Callable[[str, Optional[str]], Awaitable[ProbeResult]]


LoginStatusCallable = Callable[[Mapping[str, str]], Awaitable[Optional[str]]]


class ProviderNetworkError(RuntimeError):
    """Raised when no selected route can reach the provider endpoint."""


class ProviderEndpointUnknownError(ProviderNetworkError):
    """Raised when safe non-secret configuration cannot identify an endpoint."""
