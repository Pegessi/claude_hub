"""Persistent Feishu chat bindings for the ``claude-hub feishu`` CLI.

Scenario A pushes cards to a human over Feishu, so the CLI needs to know *which*
chat to push to. Rather than force every ``send-card`` invocation to carry a raw
``chat_id``, the CLI keeps a small named-binding store: ``feishu bind ops
--chat-id oc_xxx`` records a friendly alias, and ``send-card --to ops`` resolves
it. A raw ``oc_...`` chat id is always accepted directly too.

The store is a flat JSON object (``{name: chat_id}``) under the CLI config dir
(``$CLAUDE_HUB_CONFIG_DIR`` or ``~/.config/claude-hub/feishu_bindings.json``).
It is deliberately separate from the read-only TOML connection config: bindings
are user-managed mutable state, written via ``bind`` / ``unbind``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict

# Bindings live alongside the connection config but in their own mutable file.
DEFAULT_CONFIG_DIR = "~/.config/claude-hub"
BINDINGS_FILENAME = "feishu_bindings.json"


def bindings_path() -> Path:
    """Resolve the bindings file path, honoring ``$CLAUDE_HUB_CONFIG_DIR``."""
    base = os.environ.get("CLAUDE_HUB_CONFIG_DIR") or DEFAULT_CONFIG_DIR
    return Path(base).expanduser() / BINDINGS_FILENAME


def load_bindings() -> Dict[str, str]:
    """Load all name -> chat_id bindings.

    Returns an empty mapping when the file is missing or malformed, so a
    corrupted file degrades to "no bindings" rather than crashing the CLI.
    """
    path = bindings_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Coerce to a clean str->str mapping, dropping any malformed entries.
    return {str(k): str(v) for k, v in data.items() if isinstance(v, (str, int))}


def save_bindings(bindings: Dict[str, str]) -> None:
    """Persist the full bindings mapping, creating the config dir if needed."""
    path = bindings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(bindings, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")


def set_binding(name: str, chat_id: str) -> Dict[str, str]:
    """Add or update a single binding; return the updated mapping."""
    bindings = load_bindings()
    bindings[name] = chat_id
    save_bindings(bindings)
    return bindings


def remove_binding(name: str) -> bool:
    """Remove a binding by name; return ``True`` if it existed."""
    bindings = load_bindings()
    if name not in bindings:
        return False
    del bindings[name]
    save_bindings(bindings)
    return True


def resolve_target(target: str) -> str:
    """Resolve a ``--to`` value to a chat id.

    A value matching a stored binding name resolves to its chat id; otherwise
    the value is returned unchanged so a raw ``oc_...`` chat id works directly.
    """
    return load_bindings().get(target, target)
