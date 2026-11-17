"""Server-side redaction for structured-stream events.

Strips environment variable values, masks known secret patterns, and truncates
oversized payload fields before events leave the backend. Chat turn text fields
get a much larger budget than tool payloads; see ``MAX_CHAT_TEXT_CHARS``. The
original event is never mutated; a copy with ``redacted=True`` is returned.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict

from ...models import AgentStreamEvent, AgentStreamEventType
from ..provider_proxy import redact_provider_proxy_diagnostic

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
# Reader-visible chat text is not a diagnostic field. Redaction runs before the
# event is persisted and before it is fanned out, and history reads serve the
# stored row verbatim, so the 4,000-char cap used to cut the tail of every long
# answer (and of the reader's own message) with no way to recover it Hub-side.
# These fields get a budget that still bounds one event, while tool arguments,
# tool results and every other payload field keep ``MAX_FIELD_CHARS``.
MAX_CHAT_TEXT_CHARS = 128_000
_TRUNCATION_MARKER = "…[truncated]"

# The payload keys, per event type, that carry chat text rather than a tool or
# diagnostic payload. Only these keys, and only at the top level of the payload,
# receive ``MAX_CHAT_TEXT_CHARS``; nested values (metadata, attachments) stay
# on ``MAX_FIELD_CHARS`` so an unbounded field cannot ride along with a turn.
_CHAT_TEXT_KEYS: Dict[AgentStreamEventType, frozenset[str]] = {
    AgentStreamEventType.TEXT_DELTA: frozenset({"text"}),
    AgentStreamEventType.THINKING_DELTA: frozenset({"text"}),
    AgentStreamEventType.TURN_STARTED: frozenset({"summary"}),
    AgentStreamEventType.TURN_COMPLETED: frozenset({"text", "assistant_text", "final_text"}),
}


def _mask_string(value: str) -> str:
    masked = value
    for pattern in _TOKEN_PATTERNS:
        masked = pattern.sub("[REDACTED]", masked)
    return masked


def mask_secrets(text: str) -> str:
    """Mask known secret patterns in text that leaves the Hub (e.g. a Feishu message)."""
    return _mask_string(text)


def _redact_value(key: str, value: Any, *, limit: int = MAX_FIELD_CHARS) -> Any:
    """Mask and bound one payload value; ``limit`` applies to this string only."""
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
        if key.lower() in {"message", "error", "detail", "stderr", "reason"}:
            masked = redact_provider_proxy_diagnostic(masked)
        if len(masked) > limit:
            return masked[: limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
        return masked
    return value


def redact_event(event: AgentStreamEvent) -> AgentStreamEvent:
    """Return a redacted copy of ``event``; never mutates the input."""
    if not event.payload:
        return event.model_copy(update={"redacted": True})
    chat_text_keys = _CHAT_TEXT_KEYS.get(event.type, frozenset())
    redacted_payload: Dict[str, Any] = {
        k: _redact_value(
            k, v, limit=MAX_CHAT_TEXT_CHARS if k in chat_text_keys else MAX_FIELD_CHARS
        )
        for k, v in event.payload.items()
    }
    if event.type in {AgentStreamEventType.STATUS, AgentStreamEventType.ERROR}:
        text = redacted_payload.get("text")
        if isinstance(text, str):
            redacted_payload["text"] = redact_provider_proxy_diagnostic(text)
    return event.model_copy(update={"payload": redacted_payload, "redacted": True})
