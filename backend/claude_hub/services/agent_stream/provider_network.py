"""Choose direct or configured-proxy networking for native Chat providers.

The selector works only with a caller-supplied subprocess environment.  It
never mutates :data:`os.environ`, and it never retries a provider turn.  A
short unauthenticated ``HEAD`` request proves transport reachability; every
HTTP response is considered reachable because authorization, rate limits, and
model/path errors are application outcomes rather than routing failures.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
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

logger = logging.getLogger(__name__)

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


def _probe_log_fields(result: Optional[ProbeResult]) -> Dict[str, Any]:
    if result is None:
        return {
            "attempted": False,
            "reachable": None,
            "phase": "not_attempted",
            "status_code": None,
        }
    allowed_phases = {
        "dns",
        "http",
        "network",
        "proxy-auth",
        "proxy-connect",
        "tcp",
        "timeout",
        "tls",
    }
    phase = result.phase if result.phase in allowed_phases else "unknown"
    return {
        "attempted": True,
        "reachable": result.reachable,
        "phase": phase,
        "status_code": result.status_code,
    }


def _endpoint_origin(endpoint: Optional[str]) -> Optional[str]:
    if endpoint is None:
        return None
    parsed = _parse_endpoint(endpoint)
    return f"{parsed.scheme}://{_display_authority(parsed)}"


def _has_nonempty_proxy(env: Mapping[str, str]) -> bool:
    return any(bool(env.get(name)) for name in PROXY_ENV_NAMES)


def _log_selection(
    *,
    provider: str,
    mode: str,
    decision: str,
    endpoint: Optional[str],
    direct: Optional[ProbeResult],
    proxy: Optional[ProbeResult],
    selected_env: Mapping[str, str],
) -> None:
    record = {
        "tab_id": selected_env.get("CLAUDE_HUB_TAB_ID"),
        "provider": provider.strip().lower(),
        "requested_mode": mode,
        "decision": decision,
        "endpoint_origin": _endpoint_origin(endpoint),
        "direct": _probe_log_fields(direct),
        "proxy": _probe_log_fields(proxy),
        "returned_env_has_proxy": _has_nonempty_proxy(selected_env),
    }
    message = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
    if decision == "compatibility_inherit":
        logger.warning("provider_network_selection %s", message)
    else:
        logger.info("provider_network_selection %s", message)


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


class ProviderNetworkSelector:
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
        self._probe = probe or _probe_endpoint
        self._login_status = login_status or _codex_login_status
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
                self._store_cache(key, selection, self._success_ttl)
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
            if proxy_url is not None and _bypasses_proxy(parsed, no_proxy):
                raise _selection_error(endpoint, direct=direct, proxy_note="bypassed by NO_PROXY")
            if proxy_url is None:
                raise _selection_error(endpoint, direct=direct, proxy_note="not configured")
            proxied = await self._probe(endpoint, proxy_url)
            if proxied.reachable:
                return _Selection(route="proxy", direct=direct, proxy=proxied)
            raise _selection_error(endpoint, direct=direct, proxy=proxied)


def _selection_key(
    provider: str,
    endpoint: str,
    mode: str,
    proxy_material: Optional[list[Tuple[str, Optional[str]]]],
) -> str:
    material = json.dumps(
        [provider, endpoint, mode, proxy_material],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _selection_error(
    endpoint: str,
    *,
    direct: Optional[ProbeResult] = None,
    proxy: Optional[ProbeResult] = None,
    proxy_note: Optional[str] = None,
) -> ProviderNetworkError:
    parsed = _parse_endpoint(endpoint)
    authority = _display_authority(parsed)
    reasons = []
    if direct is not None:
        reasons.append(f"direct {direct.phase} failure")
    if proxy is not None:
        reasons.append(f"proxy {proxy.phase} failure")
    elif proxy_note:
        reasons.append(f"proxy {proxy_note}")
    return ProviderNetworkError(
        f"provider network preflight failed for {authority}: " + "; ".join(reasons)
    )


def _first_env(env: Mapping[str, str], names: Tuple[str, ...]) -> Optional[str]:
    for name in names:
        value = env.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _truthy_env(env: Mapping[str, str], name: str) -> bool:
    value = env.get(name)
    return isinstance(value, str) and value.strip().lower() in _TRUTHY_VALUES


def resolve_provider_endpoint(provider: str, env: Mapping[str, str]) -> str:
    """Resolve the provider endpoint from the effective initialization config."""
    provider = provider.strip().lower()
    if provider == "claude" and any(_truthy_env(env, key) for key in _ALTERNATE_CLAUDE_MODES):
        raise ProviderEndpointUnknownError(
            "Claude Bedrock, Vertex, and Foundry endpoints are provider-managed; "
            f"set {PROVIDER_NETWORK_MODE_ENV}=inherit"
        )

    explicit = _first_env(env, _ENDPOINT_ENV_NAMES.get(provider, ()))
    if explicit:
        _parse_endpoint(explicit)
        return explicit

    if provider in {"codex", "traex"}:
        configured = _configured_model_provider_endpoint(provider, env)
        if configured:
            _parse_endpoint(configured)
            return configured
        raise ProviderEndpointUnknownError(
            f"cannot determine the effective {provider} endpoint from its model-provider "
            f"configuration; configure the provider base URL and, for Codex, "
            f"forced_login_method, or set {PROVIDER_NETWORK_MODE_ENV}=inherit"
        )

    if provider == "claude":
        raise ProviderEndpointUnknownError(
            "cannot determine Claude's effective endpoint without an explicit " "ANTHROPIC_BASE_URL"
        )
    default = _DEFAULT_ENDPOINTS.get(provider)
    if default is None:
        raise ProviderNetworkError(f"provider network selection is unsupported for {provider}")
    return default


def _configured_model_provider_endpoint(provider: str, env: Mapping[str, str]) -> Optional[str]:
    config = _read_provider_config(provider, env)
    if not config:
        return None
    effective = config
    profile_name = config.get("profile")
    profiles = config.get("profiles")
    if isinstance(profile_name, str) and isinstance(profiles, dict):
        profile = profiles.get(profile_name)
        if isinstance(profile, dict):
            effective = {**config, **profile}
    provider_id = effective.get("model_provider")
    providers = config.get("model_providers")
    configured_base_url: Optional[str] = None
    if isinstance(provider_id, str) and isinstance(providers, dict):
        provider_config = providers.get(provider_id)
        if isinstance(provider_config, dict):
            base_url = provider_config.get("base_url")
            if isinstance(base_url, str) and base_url.strip():
                configured_base_url = base_url.strip()

    if provider == "codex":
        login_method = effective.get("forced_login_method")
        if isinstance(login_method, str):
            normalized_login = login_method.strip().lower().replace("-", "_")
            if normalized_login == "chatgpt":
                chatgpt_base_url = effective.get("chatgpt_base_url")
                if isinstance(chatgpt_base_url, str) and chatgpt_base_url.strip():
                    return chatgpt_base_url.strip()
                return "https://chatgpt.com/backend-api/"
            if normalized_login in {"api", "api_key", "apikey"}:
                if configured_base_url:
                    return configured_base_url
                openai_base_url = effective.get("openai_base_url")
                if isinstance(openai_base_url, str) and openai_base_url.strip():
                    return openai_base_url.strip()
                return "https://api.openai.com/v1"
        # A custom provider id with an explicit base URL is unambiguous. The
        # built-in `openai` provider is not: its API-key and ChatGPT login paths
        # use different endpoints, so require forced_login_method or inherit.
        if configured_base_url and provider_id != "openai":
            return configured_base_url
        return None

    if configured_base_url:
        return configured_base_url
    openai_base_url = effective.get("openai_base_url")
    if isinstance(openai_base_url, str) and openai_base_url.strip():
        return openai_base_url.strip()
    return None


def _provider_config_paths(provider: str, env: Mapping[str, str]) -> Tuple[Path, ...]:
    home = Path(env.get("HOME") or "~").expanduser()
    if provider == "codex":
        root = Path(env.get("CODEX_HOME") or home / ".codex").expanduser()
        return (root / "config.toml",)
    root = Path(env.get("TRAE_HOME") or home / ".trae").expanduser()
    return (root / "traecli.toml", root / "config.toml")


def _read_provider_config(provider: str, env: Mapping[str, str]) -> Dict[str, object]:
    for path in _provider_config_paths(provider, env):
        try:
            with path.open("rb") as stream:
                parsed = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _codex_endpoint_for_login_mode(
    login_mode: Optional[str], env: Mapping[str, str]
) -> Optional[str]:
    if login_mode is None:
        return None
    config = _read_provider_config("codex", env)
    effective = config
    profile_name = config.get("profile")
    profiles = config.get("profiles")
    if isinstance(profile_name, str) and isinstance(profiles, dict):
        profile = profiles.get(profile_name)
        if isinstance(profile, dict):
            effective = {**config, **profile}
    if login_mode == "chatgpt":
        configured = effective.get("chatgpt_base_url")
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        return "https://chatgpt.com/backend-api/"
    if login_mode != "api_key":
        return None
    provider_id = effective.get("model_provider", "openai")
    providers = config.get("model_providers")
    if isinstance(provider_id, str) and isinstance(providers, dict):
        provider_config = providers.get(provider_id)
        if isinstance(provider_config, dict):
            configured = provider_config.get("base_url")
            if isinstance(configured, str) and configured.strip():
                return configured.strip()
    configured = effective.get("openai_base_url")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return "https://api.openai.com/v1"


def _codex_login_status_key(env: Mapping[str, str]) -> str:
    root = _provider_config_paths("codex", env)[0].parent
    metadata: list[Tuple[str, Optional[int], Optional[int]]] = []
    for path in (root / "auth.json", root / "config.toml"):
        try:
            stat = path.stat()
        except OSError:
            metadata.append((str(path), None, None))
        else:
            metadata.append((str(path), stat.st_mtime_ns, stat.st_size))
    material = json.dumps(
        [env.get("PATH"), env.get("CODEX_HOME"), env.get("HOME"), metadata],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def _codex_login_status(env: Mapping[str, str]) -> Optional[str]:
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
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=_LOGIN_STATUS_TIMEOUT_SECONDS
        )
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


def provider_network_configuration_fingerprint(provider: str, env: Mapping[str, str]) -> str:
    """Hash route-relevant effective config without retaining secret values."""
    provider = provider.strip().lower()
    try:
        endpoint = resolve_provider_endpoint(provider, env)
    except ProviderNetworkError:
        endpoint = "<unresolved>"
    relevant_names = (
        PROVIDER_NETWORK_MODE_ENV,
        *PROXY_ENV_NAMES,
        "no_proxy",
        "NO_PROXY",
        *_ENDPOINT_ENV_NAMES.get(provider, ()),
        *_ALTERNATE_CLAUDE_MODES,
        "CODEX_HOME",
        "TRAE_HOME",
        "HOME",
    )
    config_metadata: list[Tuple[str, Optional[int], Optional[int]]] = []
    if provider in {"codex", "traex"}:
        config_paths = list(_provider_config_paths(provider, env))
        if provider == "codex":
            config_paths.append(config_paths[0].parent / "auth.json")
        for path in config_paths:
            try:
                stat = path.stat()
            except OSError:
                config_metadata.append((str(path), None, None))
            else:
                config_metadata.append((str(path), stat.st_mtime_ns, stat.st_size))
    material = json.dumps(
        [
            provider,
            endpoint,
            [(name, env.get(name)) for name in relevant_names],
            config_metadata,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _parse_endpoint(endpoint: str) -> SplitResult:
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise ProviderNetworkError("provider endpoint is not a valid URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderNetworkError("provider endpoint must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ProviderNetworkError("provider endpoint must not contain credentials")
    if port is not None and not 1 <= port <= 65535:
        raise ProviderNetworkError("provider endpoint port must be in 1..65535")
    return parsed


def _display_authority(parsed: SplitResult) -> str:
    host = parsed.hostname or "unknown"
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{host}:{port}"


def _probe_url(parsed: SplitResult) -> str:
    host = parsed.hostname or ""
    display_host = f"[{host}]" if ":" in host else host
    default_port = 443 if parsed.scheme == "https" else 80
    netloc = (
        display_host if parsed.port in {None, default_port} else f"{display_host}:{parsed.port}"
    )
    # Never transmit configured query parameters: they can contain credentials.
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", "", ""))


def _effective_proxy_value(env: Mapping[str, str], name: str) -> Optional[str]:
    lower = name.lower()
    upper = name.upper()
    lower_value = env.get(lower)
    upper_value = env.get(upper)
    if (
        isinstance(lower_value, str)
        and lower_value.strip()
        and isinstance(upper_value, str)
        and upper_value.strip()
        and lower_value.strip() != upper_value.strip()
    ):
        raise ProviderNetworkError(f"conflicting lowercase and uppercase {upper} provider settings")
    # An explicitly present lowercase variable wins over its uppercase
    # counterpart, including when it is empty to disable the setting.
    value = lower_value if lower in env else upper_value
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _proxy_for_endpoint(
    endpoint: SplitResult, env: Mapping[str, str]
) -> Tuple[Optional[str], Optional[str]]:
    proxy = _effective_proxy_value(env, f"{endpoint.scheme}_proxy")
    if proxy is None:
        proxy = _effective_proxy_value(env, "all_proxy")
    no_proxy = _effective_proxy_value(env, "no_proxy")
    if proxy is not None:
        parsed = urlsplit(proxy)
        if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
            raise ProviderNetworkError("configured provider proxy is not a valid supported URL")
    return proxy, no_proxy


def _bypasses_proxy(endpoint: SplitResult, no_proxy: Optional[str]) -> bool:
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


def _without_proxy(env: Mapping[str, str]) -> Dict[str, str]:
    selected = dict(env)
    for name in PROXY_ENV_NAMES:
        selected.pop(name, None)
    return selected


async def _probe_endpoint(endpoint: str, proxy_url: Optional[str]) -> ProbeResult:
    parsed = _parse_endpoint(endpoint)
    timeout = httpx.Timeout(_PROBE_TIMEOUT_SECONDS)
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
                timeout=_PROBE_TIMEOUT_SECONDS,
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


_default_selector = ProviderNetworkSelector()


async def select_provider_subprocess_env(provider: str, env: Mapping[str, str]) -> Dict[str, str]:
    """Select networking for one provider subprocess without global mutation."""
    return await _default_selector.select(provider, env)
