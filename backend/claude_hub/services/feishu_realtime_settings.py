"""Runtime storage for the Feishu WebSocket dispatch switch."""

import json
import os
import tempfile
from pathlib import Path
from threading import Lock

from .runtime_isolation import resolve_runtime_home

ENABLED_ENV = "CLAUDE_HUB_FEISHU_REALTIME_EVENTS_ENABLED"
SETTINGS_FILE = "feishu_realtime_events.json"
_write_lock = Lock()


def parse_switch(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
    return None


def realtime_env_override() -> bool | None:
    value = parse_switch(os.environ.get(ENABLED_ENV))
    return value if value is not None else parse_switch(os.environ.get("enabled"))


def write_realtime_enabled(enabled: bool) -> None:
    write_realtime_settings({"enabled": enabled})


def write_realtime_settings(changes: dict[str, object]) -> None:
    """Replace the settings document atomically, preserving all other keys."""
    path = resolve_runtime_home() / SETTINGS_FILE
    with _write_lock:
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            settings = {}
        if not isinstance(settings, dict):
            raise ValueError("Feishu realtime settings must be a JSON object")
        if "enabled" in changes:
            settings.pop(ENABLED_ENV, None)
        settings.update(changes)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        )
        temporary = Path(handle.name)
        try:
            with handle:
                json.dump(settings, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
