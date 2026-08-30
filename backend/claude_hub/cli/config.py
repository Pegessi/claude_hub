"""Resolved CLI configuration and precedence handling.

Precedence for each setting: explicit flag > environment variable > config file
> built-in default. The config file is TOML with keys under a ``[default]``
table::

    [default]
    base_url = "http://127.0.0.1:8173"
    token = "..."
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_BASE_URL = "http://127.0.0.1:8173"
DEFAULT_CONFIG_PATH = "~/.config/claude-hub/config.toml"


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
        """Return the default env preset for a given agent type, or None."""
        return self.default_env_presets.get(agent_type)


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


def resolve_settings(
    *,
    base_url: Optional[str],
    token: Optional[str],
    cookie: Optional[str],
    json_output: bool,
    verbose: bool,
    config_path: Optional[str],
) -> Settings:
    """Resolve settings honoring flag > env > config file > default."""
    raw_config_path = config_path or os.environ.get("CLAUDE_HUB_CONFIG") or DEFAULT_CONFIG_PATH
    file_config = _read_config_file(Path(raw_config_path).expanduser())
    default_section = file_config.get("default", {})
    if not isinstance(default_section, dict):
        default_section = {}

    resolved_base_url = (
        base_url
        or os.environ.get("CLAUDE_HUB_URL")
        or _str_or_none(default_section.get("base_url"))
        or DEFAULT_BASE_URL
    )
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
