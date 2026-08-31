"""Server-side redaction for structured-stream events.

Strips environment variable values, masks known secret patterns, and truncates
oversized payload fields before events leave the backend. The original event is
never mutated; a copy with ``redacted=True`` is returned.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict

from ...models import AgentStreamEvent

# Payload keys whose values are environment-variable maps. We keep the keys
# (so the UI can show which vars were set) but drop every value.
_ENV_VALUE_KEYS = frozenset({"env", "environment", "env_vars", "envvars", "variables", "env_map"})

# Substring match on payload keys for secrets. Whole value is masked.
_SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|auth|credential|private[_-]?key)",
    re.IGNORECASE,
)

# Known secret literal patterns (prefix-based).
_TOKEN_PATTERNS = [
    re.compile(r"\bsk-[a-zA-Z0-9]{10,}\b"),
    re.compile(r"\bsk-ant-[a-zA-Z0-9_-]{10,}\b"),
    re.compile(r"\bghp_[a-zA-Z0-9]{20,}\b"),
    re.compile(r"\bgho_[a-zA-Z0-9]{20,}\b"),
    re.compile(r"\bghs_[a-zA-Z0-9]{20,}\b"),
    re.compile(r"\bplat_[a-zA-Z0-9_-]{10,}\b"),
    re.compile(r"\bxox[abprs]-[a-zA-Z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+[a-zA-Z0-9._~+/=-]{10,}\b"),
    re.compile(r"\bANTHROPIC_AUTH_TOKEN\s*=\s*\S+"),
]

# Any single string field longer than this is truncated.
MAX_FIELD_CHARS = 4000
_TRUNCATION_MARKER = "…[truncated]"


def _mask_string(value: str) -> str:
    masked = value
    for pattern in _TOKEN_PATTERNS:
        masked = pattern.sub("[REDACTED]", masked)
    return masked


def _redact_value(key: str, value: Any) -> Any:
    if isinstance(value, dict):
        if key in _ENV_VALUE_KEYS:
            # Keep keys, drop values.
            return {k: "[REDACTED]" for k in value.keys()}
        return {k: _redact_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(key, v) for v in value]
    if isinstance(value, str):
        if _SENSITIVE_KEY_RE.search(key):
            return "[REDACTED]"
        masked = _mask_string(value)
        if len(masked) > MAX_FIELD_CHARS:
            return masked[: MAX_FIELD_CHARS - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
        return masked
    return value


def redact_event(event: AgentStreamEvent) -> AgentStreamEvent:
    """Return a redacted copy of ``event``; never mutates the input."""
    if not event.payload:
        return event.model_copy(update={"redacted": True})
    redacted_payload: Dict[str, Any] = {k: _redact_value(k, v) for k, v in event.payload.items()}
    return event.model_copy(update={"payload": redacted_payload, "redacted": True})
