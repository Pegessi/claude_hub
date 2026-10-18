"""Single-owner network selection cache, lock and in-flight probes."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Mapping, Optional, Tuple

from ...proxy_rules import bypasses_proxy
from .network_config import (
    _codex_endpoint_for_login_mode,
    _codex_login_status_key,
    _log_selection,
    _parse_endpoint,
    _proxy_for_endpoint,
    _selection_error,
    _selection_key,
    _without_proxy,
    resolve_provider_endpoint,
)
from .network_types import (
    _FAILURE_TTL_SECONDS,
    _LOGIN_STATUS_TTL_SECONDS,
    _MAX_CACHE_ENTRIES,
    _MAX_CONCURRENT_PROBES,
    _SUCCESS_TTL_SECONDS,
    _VALID_MODES,
    PROVIDER_NETWORK_MODE_ENV,
    PROXY_ENV_NAMES,
    LoginStatusCallable,
    ProbeCallable,
    ProviderEndpointUnknownError,
    ProviderNetworkError,
    _Selection,
)


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


class NetworkSelectorCore(ABC):
    """Resolve one subprocess environment with cached, coalesced probes."""

    def __init__(
        self,
        *,
        probe: Optional[ProbeCallable] = None,
        login_status: Optional[LoginStatusCallable] = None,
        success_ttl: float = _SUCCESS_TTL_SECONDS,
        failure_ttl: float = _FAILURE_TTL_SECONDS,
        max_concurrent: int = _MAX_CONCURRENT_PROBES,
    ) -> None:
        self._probe = probe or self._default_probe
        self._login_status = login_status or self._default_login_status
        self._success_ttl = success_ttl
        self._failure_ttl = failure_ttl
        self._cache: Dict[str, Tuple[float, _Selection | ProviderNetworkError]] = {}
        self._inflight: Dict[str, asyncio.Task[_Selection]] = {}
        self._login_cache: Dict[str, Tuple[float, Optional[str]]] = {}
        self._login_inflight: Dict[str, asyncio.Task[Optional[str]]] = {}
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def select(self, provider: str, env: Mapping[str, str]) -> Dict[str, str]:
        """Return the exact environment for the next provider subprocess.

        ``inherit`` is the explicit compatibility escape hatch for provider
        configurations whose final endpoint cannot be derived without reading
        credentials or provider-managed configuration layers.
        """
        resolved_env = dict(env)
        mode = resolved_env.get(PROVIDER_NETWORK_MODE_ENV, "auto").strip().lower()
        if mode not in _VALID_MODES:
            allowed = ", ".join(sorted(_VALID_MODES))
            raise ProviderNetworkError(
                f"invalid {PROVIDER_NETWORK_MODE_ENV}; expected one of: {allowed}"
            )
        if mode == "inherit":
            _log_selection(
                provider=provider,
                mode=mode,
                decision="explicit_inherit",
                endpoint=None,
                direct=None,
                proxy=None,
                selected_env=resolved_env,
            )
            return resolved_env

        endpoint = await self._resolve_endpoint(provider, resolved_env, mode)
        if endpoint is None:
            _log_selection(
                provider=provider,
                mode=mode,
                decision="compatibility_inherit",
                endpoint=None,
                direct=None,
                proxy=None,
                selected_env=resolved_env,
            )
            return resolved_env
        proxy_material = None
        if mode != "direct":
            proxy_material = [
                (name, resolved_env.get(name))
                for name in (*PROXY_ENV_NAMES, "no_proxy", "NO_PROXY")
            ]
        key = _selection_key(provider, endpoint, mode, proxy_material)
        selection = await self._cached_selection(
            key,
            endpoint=endpoint,
            mode=mode,
            env=resolved_env,
        )
        if selection.route == "direct":
            selected_env = _without_proxy(resolved_env)
        else:
            selected_env = resolved_env
        _log_selection(
            provider=provider,
            mode=mode,
            decision=selection.route,
            endpoint=endpoint,
            direct=selection.direct,
            proxy=selection.proxy,
            selected_env=selected_env,
        )
        return selected_env

    async def _resolve_endpoint(
        self, provider: str, env: Mapping[str, str], mode: str
    ) -> Optional[str]:
        normalized_provider = provider.strip().lower()
        try:
            return resolve_provider_endpoint(normalized_provider, env)
        except ProviderEndpointUnknownError as exc:
            unresolved = exc
        if normalized_provider == "codex":
            login_mode = await self._cached_codex_login_status(env)
            endpoint = _codex_endpoint_for_login_mode(login_mode, env)
            if endpoint is not None:
                return endpoint
        if mode == "auto":
            return None
        raise ProviderEndpointUnknownError(
            f"{unresolved}; forced network mode requires a verified endpoint; "
            f"set {PROVIDER_NETWORK_MODE_ENV}=inherit to preserve provider routing"
        ) from unresolved

    async def _cached_codex_login_status(self, env: Mapping[str, str]) -> Optional[str]:
        key = _codex_login_status_key(env)
        now = time.monotonic()
        async with self._lock:
            cached = self._login_cache.get(key)
            if cached is not None and cached[0] > now:
                return cached[1]
            task = self._login_inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._run_login_status_task(key, env))
                self._login_inflight[key] = task
                task.add_done_callback(_consume_task_exception)
        return await asyncio.shield(task)

    async def _run_login_status_task(self, key: str, env: Mapping[str, str]) -> Optional[str]:
        task = asyncio.current_task()
        try:
            async with self._semaphore:
                status = await self._login_status(env)
            async with self._lock:
                self._login_cache[key] = (
                    time.monotonic() + _LOGIN_STATUS_TTL_SECONDS,
                    status,
                )
                while len(self._login_cache) > _MAX_CACHE_ENTRIES:
                    oldest_key = min(
                        self._login_cache,
                        key=lambda cache_key: self._login_cache[cache_key][0],
                    )
                    self._login_cache.pop(oldest_key, None)
            return status
        finally:
            async with self._lock:
                if self._login_inflight.get(key) is task:
                    self._login_inflight.pop(key, None)

    async def _cached_selection(
        self,
        key: str,
        *,
        endpoint: str,
        mode: str,
        env: Mapping[str, str],
    ) -> _Selection:
        now = time.monotonic()
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] > now:
                value = cached[1]
                if isinstance(value, ProviderNetworkError):
                    raise ProviderNetworkError(str(value))
                return value
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._run_selection_task(
                        key,
                        endpoint=endpoint,
                        mode=mode,
                        env=env,
                    )
                )
                self._inflight[key] = task
                task.add_done_callback(_consume_task_exception)
        return await asyncio.shield(task)

    async def _run_selection_task(
        self,
        key: str,
        *,
        endpoint: str,
        mode: str,
        env: Mapping[str, str],
    ) -> _Selection:
        task = asyncio.current_task()
        try:
            selection = await self._select_uncached(
                endpoint=endpoint,
                mode=mode,
                env=env,
            )
        except ProviderNetworkError as exc:
            async with self._lock:
                self._store_cache(
                    key,
                    ProviderNetworkError(str(exc)),
                    self._failure_ttl,
                )
            raise
        else:
            async with self._lock:
                ttl = (
                    self._failure_ttl if selection.route == "timeout_inherit" else self._success_ttl
                )
                self._store_cache(key, selection, ttl)
            return selection
        finally:
            async with self._lock:
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)

    def _store_cache(
        self,
        key: str,
        value: _Selection | ProviderNetworkError,
        ttl: float,
    ) -> None:
        now = time.monotonic()
        self._cache[key] = (now + ttl, value)
        for stale_key in [
            cache_key for cache_key, (expires_at, _) in self._cache.items() if expires_at <= now
        ]:
            self._cache.pop(stale_key, None)
        while len(self._cache) > _MAX_CACHE_ENTRIES:
            oldest_key = min(self._cache, key=lambda cache_key: self._cache[cache_key][0])
            self._cache.pop(oldest_key, None)

    async def _select_uncached(
        self,
        *,
        endpoint: str,
        mode: str,
        env: Mapping[str, str],
    ) -> _Selection:
        async with self._semaphore:
            if mode in {"auto", "direct"}:
                direct = await self._probe(endpoint, None)
                if direct.reachable:
                    return _Selection(route="direct", direct=direct)
                if mode == "direct":
                    raise _selection_error(endpoint, direct=direct)
            else:
                direct = None

            parsed = _parse_endpoint(endpoint)
            proxy_url, no_proxy = _proxy_for_endpoint(parsed, env)
            if proxy_url is None or bypasses_proxy(parsed, no_proxy):
                if mode == "auto" and direct is not None and direct.phase == "timeout":
                    return _Selection(route="timeout_inherit", direct=direct)
                note = "not configured" if proxy_url is None else "bypassed by NO_PROXY"
                raise _selection_error(endpoint, direct=direct, proxy_note=note)
            proxied = await self._probe(endpoint, proxy_url)
            if proxied.reachable:
                return _Selection(route="proxy", direct=direct, proxy=proxied)
            if mode == "auto" and proxied.phase == "timeout":
                # The probe has a deliberately short budget. Preserve the
                # configured route rather than treating a slow HEAD response
                # as proof that the actual provider request cannot succeed.
                return _Selection(route="timeout_inherit", direct=direct, proxy=proxied)
            raise _selection_error(endpoint, direct=direct, proxy=proxied)

    @property
    @abstractmethod
    def _default_probe(self) -> ProbeCallable:
        """Return the concrete entry point's currently selected probe."""

    @property
    @abstractmethod
    def _default_login_status(self) -> LoginStatusCallable:
        """Return the concrete entry point's currently selected login probe."""
