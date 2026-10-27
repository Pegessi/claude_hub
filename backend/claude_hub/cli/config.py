"""Resolved CLI configuration and precedence handling.

Base URL precedence: explicit flag > ``CLAUDE_HUB_URL`` > config file
``[default].base_url`` > a URL derived from ``CLAUDE_HUB_HOST`` /
``CLAUDE_HUB_PORT`` > an automatic loopback probe > built-in default.

When no URL is configured explicitly and ``CLAUDE_HUB_HOST`` is unset, the CLI
probes ``http://127.0.0.1:<port>/health`` and ``http://[::1]:<port>/health``
with a short timeout and uses the first healthy endpoint (the Hub commonly
listens on IPv6 only). Nothing is cached on disk.

The config file is TOML with keys under a ``[default]`` table::

    [default]
    base_url = "http://127.0.0.1:8173"
    token = "..."
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import httpx

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8173
DEFAULT_BASE_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
DEFAULT_CONFIG_PATH = "~/.config/claude-hub/config.toml"

# Keep the auto-probe brief: a refused loopback connection fails immediately,
# so this only bounds genuinely unreachable/black-holed addresses.
HEALTH_PROBE_TIMEOUT = 0.5

# Bind wildcards to their loopback counterparts: a Hub started on 0.0.0.0 / ::
# is reachable via 127.0.0.1 / ::1, while the wildcard itself is not a valid
# connect target (and bare ``::`` cannot be put in a URL unbracketed).
_WILDCARD_HOSTS = {"0.0.0.0": "127.0.0.1", "::": "::1"}


@dataclass
class Settings:
    """Resolved connection settings for a CLI invocation."""

    base_url: str
    token: Optional[str] = None
    cookie: Optional[str] = None
    json_output: bool = False
    verbose: bool = False
    default_env_preset: Optional[str] = None
    default_env_presets: Dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.default_env_presets is None:
            self.default_env_presets = {}

    def env_preset_for_agent_type(self, agent_type: str) -> Optional[str]:
        """Return the default env preset for a given agent type, or None.

        Config ``[default_env_presets]`` wins. Claude falls back to ``day1``
        when that table has no entry. Set ``claude = "none"`` to disable.
        """
        if agent_type in self.default_env_presets:
            value = self.default_env_presets[agent_type].strip()
            if not value or value == "none":
                return None
            return value
        if agent_type == "claude":
            return "day1"
        return None


def _read_config_file(path: Path) -> Dict[str, Any]:
    """Read the full TOML config file.

    Returns an empty mapping when the file is missing or malformed.
    """
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _str_or_none(value: Any) -> Optional[str]:
    """Return ``value`` if it is a string, else ``None``.

    Config-file values of the wrong TYPE (e.g. ``base_url = 8173``) are ignored
    so resolution falls through to env/default, rather than silently coercing a
    non-string into a surprising ``str(...)`` form. This complements the
    documented "malformed file is ignored" contract for parse errors.
    """
    return value if isinstance(value, str) else None


def _strip_url_or_none(value: Optional[str]) -> Optional[str]:
    """Strip an explicit URL value; whitespace-only becomes unset.

    Surrounding whitespace in a copied URL is incidental, so a wholesale
    ``.strip()`` is safe for http(s) URLs. A value containing only whitespace
    is treated as if it were never provided, so resolution falls through to the
    next source in the flag > env > config precedence chain.
    """
    if value is None:
        return None
    value = value.strip()
    return value or None


def _coerce_port(raw: Optional[str]) -> int:
    """Parse ``CLAUDE_HUB_PORT``; missing/invalid values fall back to default."""
    if raw:
        try:
            port = int(raw)
        except ValueError:
            return DEFAULT_PORT
        if 1 <= port <= 65535:
            return port
    return DEFAULT_PORT


def _normalize_host(host: str) -> str:
    """Strip incidental brackets and map wildcard bind hosts to loopback."""
    host = host.strip()
    if len(host) >= 2 and host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return _WILDCARD_HOSTS.get(host, host)


def build_base_url(host: str, port: int) -> str:
    """Assemble an HTTP base URL, bracketing bare IPv6 literals."""
    host = _normalize_host(host)
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"


def probe_health(
    base_url: str,
    *,
    timeout: float = HEALTH_PROBE_TIMEOUT,
    transport: Optional[httpx.BaseTransport] = None,
) -> bool:
    """Return whether ``GET {base_url}/health`` identifies a healthy Hub.

    The probe is a Hub-discovery mechanism, not a generic liveness check, so a
    2xx status alone is not enough: the response must be JSON with
    ``status == "healthy"``, the shape returned by the Hub's ``/health`` route.
    A 2xx with a non-JSON (or unrecognized) body is deliberately treated as
    unhealthy — another service answering on the port must not be mistaken for
    the Hub. Any transport error (refused, timeout, ...) or non-2xx response
    also counts as unhealthy. The result is never persisted.
    """
    try:
        with httpx.Client(timeout=timeout, trust_env=False, transport=transport) as client:
            response = client.get(f"{base_url.rstrip('/')}/health")
    except httpx.HTTPError:
        return False
    if not response.is_success:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("status") == "healthy"


def auto_detect_base_url(
    port: int,
    *,
    probe: Callable[[str], bool] = probe_health,
) -> str:
    """Probe IPv4 then IPv6 loopback; fall back to the IPv4 default URL.

    Tries 127.0.0.1 first to match the historical default, then ``[::1]`` for
    IPv6-only hosts. Returns the first base URL whose ``/health`` endpoint
    answers, or the default-form URL when neither does.
    """
    for host in (DEFAULT_HOST, "::1"):
        candidate = build_base_url(host, port)
        if probe(candidate):
            return candidate
    return build_base_url(DEFAULT_HOST, port)


def resolve_settings(
    *,
    base_url: Optional[str],
    token: Optional[str],
    cookie: Optional[str],
    json_output: bool,
    verbose: bool,
    config_path: Optional[str],
    health_probe: Optional[Callable[[str], bool]] = None,
) -> Settings:
    """Resolve settings honoring flag > env > config file > host/port > probe.

    Explicit URL sources (flag, ``CLAUDE_HUB_URL``, config ``base_url``) and an
    explicit ``CLAUDE_HUB_HOST`` are authoritative: no health probe runs. With
    neither, the loopback probe picks between IPv4 and IPv6.
    """
    raw_config_path = config_path or os.environ.get("CLAUDE_HUB_CONFIG") or DEFAULT_CONFIG_PATH
    file_config = _read_config_file(Path(raw_config_path).expanduser())
    default_section = file_config.get("default", {})
    if not isinstance(default_section, dict):
        default_section = {}

    explicit_base_url = (
        _strip_url_or_none(base_url)
        or _strip_url_or_none(os.environ.get("CLAUDE_HUB_URL"))
        or _strip_url_or_none(_str_or_none(default_section.get("base_url")))
    )
    env_host = os.environ.get("CLAUDE_HUB_HOST")
    if env_host is not None:
        env_host = env_host.strip() or None
    env_port = _coerce_port(os.environ.get("CLAUDE_HUB_PORT"))

    if explicit_base_url is not None:
        resolved_base_url = explicit_base_url
    elif env_host is not None:
        resolved_base_url = build_base_url(env_host, env_port)
    else:
        probe = health_probe if health_probe is not None else probe_health
        resolved_base_url = auto_detect_base_url(env_port, probe=probe)
    resolved_token = (
        token or os.environ.get("CLAUDE_HUB_TOKEN") or _str_or_none(default_section.get("token"))
    )
    resolved_default_env_preset = _str_or_none(default_section.get("default_env_preset"))

    # Per-agent-type default env presets from [default_env_presets] section.
    presets_section = file_config.get("default_env_presets", {})
    resolved_default_env_presets: Dict[str, str] = {}
    if isinstance(presets_section, dict):
        for key, value in presets_section.items():
            if isinstance(key, str) and isinstance(value, str):
                resolved_default_env_presets[key] = value

    return Settings(
        base_url=str(resolved_base_url),
        token=str(resolved_token) if resolved_token is not None else None,
        cookie=cookie,
        json_output=json_output,
        verbose=verbose,
        default_env_preset=resolved_default_env_preset,
        default_env_presets=resolved_default_env_presets,
    )
