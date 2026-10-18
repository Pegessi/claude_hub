"""Resolve safe provider endpoints, proxy policy and configuration fingerprints."""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .network_types import (
    _ALTERNATE_CLAUDE_MODES,
    _DEFAULT_ENDPOINTS,
    _ENDPOINT_ENV_NAMES,
    _TRUTHY_VALUES,
    PROVIDER_NETWORK_MODE_ENV,
    PROXY_ENV_NAMES,
    ProbeResult,
    ProviderEndpointUnknownError,
    ProviderNetworkError,
    logger,
)


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
    if decision in {"compatibility_inherit", "timeout_inherit"}:
        logger.warning("provider_network_selection %s", message)
    else:
        logger.info("provider_network_selection %s", message)


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


def _without_proxy(env: Mapping[str, str]) -> Dict[str, str]:
    selected = dict(env)
    for name in PROXY_ENV_NAMES:
        selected.pop(name, None)
    return selected
