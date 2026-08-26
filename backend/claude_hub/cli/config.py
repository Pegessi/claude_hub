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


def _read_config_file(path: Path) -> Dict[str, Any]:
    """Read the ``[default]`` table from a TOML config file.

    Returns an empty mapping when the file is missing or malformed.
    """
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = data.get("default", {})
    return section if isinstance(section, dict) else {}


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

    resolved_base_url = (
        base_url
        or os.environ.get("CLAUDE_HUB_URL")
        or _str_or_none(file_config.get("base_url"))
        or DEFAULT_BASE_URL
    )
    resolved_token = (
        token or os.environ.get("CLAUDE_HUB_TOKEN") or _str_or_none(file_config.get("token"))
    )
    resolved_default_env_preset = _str_or_none(file_config.get("default_env_preset"))

    return Settings(
        base_url=str(resolved_base_url),
        token=str(resolved_token) if resolved_token is not None else None,
        cookie=cookie,
        json_output=json_output,
        verbose=verbose,
        default_env_preset=resolved_default_env_preset,
    )
