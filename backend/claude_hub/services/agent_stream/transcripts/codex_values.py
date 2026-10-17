"""Codex rollout value parsing and protocol markers."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

_FLAT_OBJ_RE = re.compile(r"\{[^{}]*\}")


_CMD_RE = re.compile(r'"cmd"\s*:\s*"((?:[^"\\]|\\.)*)"')


_TRANSIENT_NOTICE_RE = re.compile(r"^\s*(?:reconnect(?:ing|ed)?|retrying)\b", re.IGNORECASE)


_TRANSIENT_NOTICE_MESSAGE_ID = "provider-status:reconnect"


def _transient_provider_notice(message: Any) -> bool:
    """True when an ``error``-channel message is a recoverable reconnect."""
    return isinstance(message, str) and bool(_TRANSIENT_NOTICE_RE.match(message))


_CODE_MODE_NESTED_PREFIX = "code-mode-nested:"


_COLLAB_SPAWN_TOOLS = {"spawnAgent"}


_COLLAB_MESSAGE_TOOLS = {"sendInput"}


def _clean_thread_id(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


def _codex_tool_args(raw_input: Any) -> Dict[str, Any]:
    """Best-effort parse of a codex ``custom_tool_call.input`` string."""
    if not isinstance(raw_input, str) or not raw_input.strip():
        return {}
    try:
        parsed = json.loads(raw_input)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    m = _FLAT_OBJ_RE.search(raw_input)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    m = _CMD_RE.search(raw_input)
    if m:
        try:
            return {"cmd": json.loads('"' + m.group(1) + '"')}
        except (json.JSONDecodeError, ValueError):
            return {"cmd": m.group(1)}
    return {"input": raw_input[:200]}


def _codex_extract_text(content: Any) -> str:
    """Flatten a codex content value (list of blocks or string) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _codex_parse_arguments(arguments: Any) -> Dict[str, Any]:
    """Parse a ``function_call.arguments`` JSON string into a dict."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
            if isinstance(parsed, dict):
                return parsed
            return {"raw": arguments}
        except (json.JSONDecodeError, ValueError):
            return {"raw": arguments[:200]}
    return {}
