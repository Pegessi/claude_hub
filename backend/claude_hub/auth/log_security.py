"""Redaction for authentication secrets that can reach the server logs.

uvicorn prints the full request target, including the raw query string, in two
different places. Two auth flows put credential material there:

- ``GET /api/auth/callback?code=...&state=...`` — the OAuth authorization
  code (a bearer for the token exchange) and the single-use login CSRF state.
  HTTP access records land on the ``uvicorn.access`` logger as
  ``'%s - "%s %s HTTP/%s" %d'`` with the full path at positional arg 2.
- WebSocket handshakes authenticated with ``?session_id=...`` (terminal and
  agent-stream sockets, which cannot always set a Cookie header). In uvicorn
  0.42 every WS implementation logs the handshake on **``uvicorn.error``**
  (not ``uvicorn.access``) as ``'%s - "WebSocket %s" [accepted]'`` /
  ``' ... 403'`` / ``' ... %d'``, with the full scope path at positional
  arg 1. ``uvicorn.error`` has no handler of its own in the default config
  and propagates to the ``uvicorn`` parent handler.

Leaking those into ``backend.log`` turns the log file into a place that holds
replayable bearer values. Everything below keys off the *parameter name* and
replaces only the value, so the request line stays useful for debugging, and
off the record *template* so ordinary ``uvicorn.error`` records (startup,
lifespan, exceptions) are never touched.
"""

from __future__ import annotations

import logging
from typing import Any, List, Mapping, Optional

# Query parameters whose values must never be written to a log. Names are
# matched case-insensitively; the real values are replaced wholesale.
SENSITIVE_QUERY_PARAMS = frozenset({"code", "state", "session_id"})
REDACTED_QUERY_VALUE = "[REDACTED]"

_ACCESS_LOGGER_NAME = "uvicorn.access"
_ERROR_LOGGER_NAME = "uvicorn.error"

# uvicorn's HTTP AccessFormatter indexes the access record args positionally
# as (client_addr, method, full_path, http_version, status_code).
_ACCESS_ARG_FULL_PATH = 2

# uvicorn 0.42 logs WebSocket handshakes on uvicorn.error with
# '%s - "WebSocket %s" ...' and args (client_addr, full_path[, status_code]),
# so the full path is the second positional arg.
_WEBSOCKET_ARG_FULL_PATH = 1
_WEBSOCKET_RECORD_PREFIX = '%s - "WebSocket %s"'


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


def _is_websocket_record(msg: Any) -> bool:
    """Whether ``msg`` is one of uvicorn's WebSocket handshake templates."""
    return isinstance(msg, str) and msg.startswith(_WEBSOCKET_RECORD_PREFIX)


def _mask_record_args(
    msg: Any,
    args: Optional[tuple[Any, ...] | Mapping[str, Any]],
    *,
    websocket_only: bool,
) -> Optional[tuple[Any, ...] | Mapping[str, Any]]:
    """Return record args with the template's full-path entry redacted.

    ``websocket_only`` is used on the shared ``uvicorn.error`` logger: only
    WebSocket handshake templates are masked, so unrelated error/lifespan
    records (whose positional args are unrelated strings) pass through
    untouched. On ``uvicorn.access`` the HTTP access template is masked too.
    """
    if not isinstance(args, tuple):
        return args
    is_websocket = _is_websocket_record(msg)
    if websocket_only and not is_websocket:
        return args
    path_index = _WEBSOCKET_ARG_FULL_PATH if is_websocket else _ACCESS_ARG_FULL_PATH
    if len(args) <= path_index:
        return args
    full_path = args[path_index]
    if not isinstance(full_path, str):
        return args
    masked = redact_sensitive_query(full_path)
    if masked == full_path:
        return args
    new_args: list[Any] = list(args)
    new_args[path_index] = masked
    return tuple(new_args)


class _QueryRedactionFilter(logging.Filter):
    """Redact sensitive query values on records before any handler renders
    them, regardless of handler, formatter or propagation to ancestors.

    Logger filters run once, before the record reaches the logger's handlers
    and before propagation to ancestor loggers. On ``uvicorn.error`` this is
    the primary seam: the record is rewritten in place, so both a handler
    installed directly on the logger and the ``uvicorn`` parent handler it
    propagates to render the masked text.
    """

    _claude_hub_redacts_query = True

    def __init__(self, *, websocket_only: bool = False) -> None:
        super().__init__()
        # True for uvicorn.error: mask only WS handshake templates.
        # False for uvicorn.access: also mask the HTTP access template.
        self.websocket_only = websocket_only

    def filter(self, record: logging.LogRecord) -> bool:
        record.args = _mask_record_args(record.msg, record.args, websocket_only=self.websocket_only)
        return True


def _ensure_redaction_filter(logger_name: str, *, websocket_only: bool) -> None:
    """Attach the mode-matching redaction filter once, idempotently."""
    logger = logging.getLogger(logger_name)
    if not any(
        getattr(f, "_claude_hub_redacts_query", False)
        and getattr(f, "websocket_only", None) is websocket_only
        for f in logger.filters
    ):
        logger.addFilter(_QueryRedactionFilter(websocket_only=websocket_only))


def install_redacting_access_formatter() -> List[str]:
    """Force uvicorn's HTTP and WebSocket access logs to redact secrets.

    uvicorn configures logging (instantiating its handlers/formatter) in
    ``Config.__init__``, before the application is imported. Layered, mutually
    idempotent seams keep redaction in place for the default config, custom
    log configs and reload/reconfiguration:

    1. Logger-level :class:`_QueryRedactionFilter` instances rewrite each
       record before any handler runs and before propagation: on
       ``uvicorn.access`` the HTTP template's path arg (index 2) is masked; on
       ``uvicorn.error`` only the WebSocket handshake templates' path arg
       (index 1) is masked. This alone covers generic/ancestor handlers,
       including the ``uvicorn`` parent handler that renders WS records.
    2. The existing ``uvicorn.access`` handlers (created before import) get a
       redacting AccessFormatter, preserving the prior format string and
       colorization.
    3. ``uvicorn.logging.AccessFormatter`` is replaced in place with a
       redacting subclass so a later ``dictConfig`` also produces redacting
       HTTP access formatters.

    Safe to call more than once. Returns the logger names whose handlers were
    reformatted (empty when nothing needed adjustment).
    """
    access_logger = logging.getLogger(_ACCESS_LOGGER_NAME)

    # HTTP access records (OAuth code/state) on uvicorn.access.
    _ensure_redaction_filter(_ACCESS_LOGGER_NAME, websocket_only=False)
    # WebSocket handshake records (bearer session_id) on uvicorn.error.
    _ensure_redaction_filter(_ERROR_LOGGER_NAME, websocket_only=True)

    try:
        import uvicorn.logging as uvicorn_logging
    except ImportError:  # pragma: no cover - uvicorn is a runtime dependency
        return []

    base_formatter = uvicorn_logging.AccessFormatter
    if not getattr(base_formatter, "_claude_hub_redacts_query", False):

        class _RedactingAccessFormatter(base_formatter):  # type: ignore[misc,valid-type]
            _claude_hub_redacts_query = True

            def formatMessage(self, record: logging.LogRecord) -> str:
                record.args = _mask_record_args(record.msg, record.args, websocket_only=False)
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

    # uvicorn.error needs no formatter swap: its WS records are rewritten by
    # the logger-level filter before its own handlers or the uvicorn parent
    # render them, and DefaultFormatter formats via record.args as given.
    return changed
