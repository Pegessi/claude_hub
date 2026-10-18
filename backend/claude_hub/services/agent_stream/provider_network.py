"""Compatibility entry points for provider network selection.

Implementations live in ``providers/network_*``; this module owns the one
default selector and preserves existing probe monkeypatch targets.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import socket
import ssl
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx

from ..proxy_rules import bypasses_proxy
from .providers import network_probe
from .providers.network_config import (
    _codex_endpoint_for_login_mode as _codex_endpoint_for_login_mode,
)
from .providers.network_config import _codex_login_status_key as _codex_login_status_key
from .providers.network_config import (
    _configured_model_provider_endpoint as _configured_model_provider_endpoint,
)
from .providers.network_config import _display_authority as _display_authority
from .providers.network_config import _effective_proxy_value as _effective_proxy_value
from .providers.network_config import _endpoint_origin as _endpoint_origin
from .providers.network_config import _first_env as _first_env
from .providers.network_config import _has_nonempty_proxy as _has_nonempty_proxy
from .providers.network_config import _log_selection as _log_selection
from .providers.network_config import _parse_endpoint as _parse_endpoint
from .providers.network_config import _probe_log_fields as _probe_log_fields
from .providers.network_config import _probe_url as _probe_url
from .providers.network_config import _provider_config_paths as _provider_config_paths
from .providers.network_config import _proxy_for_endpoint as _proxy_for_endpoint
from .providers.network_config import _read_provider_config as _read_provider_config
from .providers.network_config import _selection_error as _selection_error
from .providers.network_config import _selection_key as _selection_key
from .providers.network_config import _truthy_env as _truthy_env
from .providers.network_config import _without_proxy as _without_proxy
from .providers.network_config import (
    provider_network_configuration_fingerprint as provider_network_configuration_fingerprint,
)
from .providers.network_config import resolve_provider_endpoint as resolve_provider_endpoint
from .providers.network_selector import NetworkSelectorCore
from .providers.network_selector import _consume_task_exception as _consume_task_exception
from .providers.network_types import _ALTERNATE_CLAUDE_MODES as _ALTERNATE_CLAUDE_MODES
from .providers.network_types import _DEFAULT_ENDPOINTS as _DEFAULT_ENDPOINTS
from .providers.network_types import _ENDPOINT_ENV_NAMES as _ENDPOINT_ENV_NAMES
from .providers.network_types import _FAILURE_TTL_SECONDS as _FAILURE_TTL_SECONDS
from .providers.network_types import _LOGIN_STATUS_TIMEOUT_SECONDS as _LOGIN_STATUS_TIMEOUT_SECONDS
from .providers.network_types import _LOGIN_STATUS_TTL_SECONDS as _LOGIN_STATUS_TTL_SECONDS
from .providers.network_types import _MAX_CACHE_ENTRIES as _MAX_CACHE_ENTRIES
from .providers.network_types import _MAX_CONCURRENT_PROBES as _MAX_CONCURRENT_PROBES
from .providers.network_types import _PROBE_TIMEOUT_SECONDS as _PROBE_TIMEOUT_SECONDS
from .providers.network_types import _SUCCESS_TTL_SECONDS as _SUCCESS_TTL_SECONDS
from .providers.network_types import _TRUTHY_VALUES as _TRUTHY_VALUES
from .providers.network_types import _VALID_MODES as _VALID_MODES
from .providers.network_types import PROVIDER_NETWORK_MODE_ENV as PROVIDER_NETWORK_MODE_ENV
from .providers.network_types import PROXY_ENV_NAMES as PROXY_ENV_NAMES
from .providers.network_types import LoginStatusCallable as LoginStatusCallable
from .providers.network_types import ProbeCallable as ProbeCallable
from .providers.network_types import ProbeResult as ProbeResult
from .providers.network_types import ProviderEndpointUnknownError as ProviderEndpointUnknownError
from .providers.network_types import ProviderNetworkError as ProviderNetworkError
from .providers.network_types import _Selection as _Selection
from .providers.network_types import logger as logger


async def _probe_endpoint(endpoint: str, proxy_url: Optional[str]) -> ProbeResult:
    return await network_probe._probe_endpoint(
        endpoint, proxy_url, timeout_seconds=_PROBE_TIMEOUT_SECONDS
    )


async def _codex_login_status(env: Mapping[str, str]) -> Optional[str]:
    return await network_probe._codex_login_status(
        env, timeout_seconds=_LOGIN_STATUS_TIMEOUT_SECONDS
    )


async def _kill_process(process: asyncio.subprocess.Process) -> None:
    await network_probe._kill_process(process)


def _connect_failure_phase(exc: BaseException) -> str:
    return network_probe._connect_failure_phase(exc)


class ProviderNetworkSelector(NetworkSelectorCore):
    """Public selector with explicit per-instance probe dependencies."""

    @property
    def _default_probe(self) -> ProbeCallable:
        return _probe_endpoint

    @property
    def _default_login_status(self) -> LoginStatusCallable:
        return _codex_login_status


_default_selector = ProviderNetworkSelector()


async def select_provider_subprocess_env(provider: str, env: Mapping[str, str]) -> Dict[str, str]:
    """Select networking for one provider subprocess without global mutation."""
    return await _default_selector.select(provider, env)
