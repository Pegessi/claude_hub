"""Read optional installation-specific defaults without committing them to Git."""

from __future__ import annotations

import json
import os

from claude_hub.services.runtime_isolation import resolve_runtime_home


def local_setting(name: str, default: str = "") -> str:
    """Prefer explicit environment values, then the private runtime settings file."""
    if name in os.environ:
        return os.environ[name]
    return private_setting(name, default)


def private_setting(name: str, default: str = "") -> str:
    """Read an installation setting without consulting process environment."""
    try:
        data = json.loads((resolve_runtime_home() / "local_runtime_settings.json").read_text())
    except (OSError, UnicodeError, ValueError):
        return default
    if not isinstance(data, dict):
        return default
    value = data.get(name)
    return value if isinstance(value, str) else default
