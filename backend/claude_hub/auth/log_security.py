"""Redaction for authentication secrets that can reach the access logs.

The uvicorn access logger prints the full request target, including the raw
query string. Two auth flows put credential material there:

- ``GET /api/auth/callback?code=...&state=...`` — the OAuth authorization
  code (a bearer for the token exchange) and the single-use login CSRF state.
- WebSocket handshakes authenticated with ``?session_id=...`` (terminal and
  agent-stream sockets, which cannot always set a Cookie header).

Leaking those into ``backend.log`` turns the log file into a place that holds
replayable bearer values. Everything below keys off the *parameter name* and
replaces only the value, so the request line stays useful for debugging.
"""

from __future__ import annotations

import logging
from typing import Any, List, Mapping, Optional

# Query parameters whose values must never be written to a log. Names are
# matched case-insensitively; the real values are replaced wholesale.
SENSITIVE_QUERY_PARAMS = frozenset({"code", "state", "session_id"})
REDACTED_QUERY_VALUE = "[REDACTED]"

# uvicorn's AccessFormatter indexes the access record args positionally as
# (client_addr, method, full_path, http_version, status_code).
_ACCESS_LOGGER_NAME = "uvicorn.access"
_ACCESS_ARG_FULL_PATH = 2


def redact_sensitive_query(target: str) -> str:
    """Mask sensitive query values in a request target, leaving the rest.

    Works on path-relative targets (``/api/auth/callback?code=x``) and on
    absolute URLs. The path, fragment, parameter names, ordering and any
    non-sensitive values are preserved byte-for-byte; only the value of a
    sensitive parameter is replaced.
    """
    if "?" not in target:
        return target

    path, _, query = target.partition("?")

    def _mask_token(token: str) -> str:
        if not token:
            return token
        key, separator, _value = token.partition("=")
        if separator and key.lower() in SENSITIVE_QUERY_PARAMS:
            return f"{key}={REDACTED_QUERY_VALUE}"
        return token

    masked_query = "&".join(_mask_token(token) for token in query.split("&"))
    return f"{path}?{masked_query}"


def _mask_access_record_args(
    args: Optional[tuple[Any, ...] | Mapping[str, Any]],
) -> Optional[tuple[Any, ...] | Mapping[str, Any]]:
    """Return access-record args with the full-path entry redacted."""
    if not isinstance(args, tuple) or len(args) <= _ACCESS_ARG_FULL_PATH:
        return args
    full_path = args[_ACCESS_ARG_FULL_PATH]
    if not isinstance(full_path, str):
        return args
    masked = redact_sensitive_query(full_path)
    if masked == full_path:
        return args
    new_args: list[Any] = list(args)
    new_args[_ACCESS_ARG_FULL_PATH] = masked
    return tuple(new_args)


class _QueryRedactionFilter(logging.Filter):
    """Redact sensitive query values on every access record, regardless of the
    handler or formatter that eventually renders it.

    Logger filters run once before the record is dispatched to any handler (and
    before propagation to ancestor loggers), so this also covers custom log
    configs that route ``uvicorn.access`` through a generic formatter.
    """

    _claude_hub_redacts_query = True

    def filter(self, record: logging.LogRecord) -> bool:
        record.args = _mask_access_record_args(record.args)
        return True


def install_redacting_access_formatter() -> List[str]:
    """Force the uvicorn access logger to redact sensitive query values.

    uvicorn configures logging (instantiating its handlers/formatter) in
    ``Config.__init__``, before the application is imported. Three layered,
    mutually idempotent seams are covered so redaction holds for the default
    config, custom log configs and reload/reconfiguration:

    1. A logger-level :class:`_QueryRedactionFilter` rewrites the record before
       any handler runs, so even generic/ancestor handlers render masked text.
    2. The existing access handlers (created before import) get a redacting
       formatter, preserving the prior format string and colorization.
    3. ``uvicorn.logging.AccessFormatter`` is replaced in place with a redacting
       subclass so a later ``dictConfig`` also produces redacting formatters.

    Safe to call more than once. Returns the logger names that were changed
    (empty when nothing needed adjustment).
    """
    access_logger = logging.getLogger(_ACCESS_LOGGER_NAME)

    if not any(getattr(f, "_claude_hub_redacts_query", False) for f in access_logger.filters):
        access_logger.addFilter(_QueryRedactionFilter())

    try:
        import uvicorn.logging as uvicorn_logging
    except ImportError:  # pragma: no cover - uvicorn is a runtime dependency
        return []

    base_formatter = uvicorn_logging.AccessFormatter
    if not getattr(base_formatter, "_claude_hub_redacts_query", False):

        class _RedactingAccessFormatter(base_formatter):  # type: ignore[misc,valid-type]
            _claude_hub_redacts_query = True

            def formatMessage(self, record: logging.LogRecord) -> str:
                record.args = _mask_access_record_args(record.args)
                rendered: str = super().formatMessage(record)
                return rendered

        uvicorn_logging.AccessFormatter = _RedactingAccessFormatter  # type: ignore[misc]

    redacting_cls = uvicorn_logging.AccessFormatter

    changed: List[str] = []
    for handler in access_logger.handlers:
        if isinstance(handler.formatter, redacting_cls):
            continue
        previous: Optional[logging.Formatter] = handler.formatter
        fmt = getattr(getattr(previous, "_style", None), "_fmt", None)
        use_colors = getattr(previous, "use_colors", None)
        kwargs = {}
        if use_colors in (True, False):
            kwargs["use_colors"] = use_colors
        handler.setFormatter(redacting_cls(fmt, **kwargs))  # type: ignore[operator]
        changed.append(_ACCESS_LOGGER_NAME)

    return changed
