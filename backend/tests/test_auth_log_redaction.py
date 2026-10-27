"""Tests for redacting OAuth/WS secrets out of the uvicorn server logs.

Uvicorn renders the full request target, including the raw query string, in
two loggers:

- ``uvicorn.access`` for HTTP lines, via ``AccessFormatter`` — the OAuth
  callback puts ``code``/``state`` there.
- ``uvicorn.error`` for WebSocket handshakes in uvicorn 0.42
  (``'%s - "WebSocket %s" [accepted]'`` / ``' ... 403'`` / ``' ... %d'``,
  path at positional arg 1) — the terminal/agent-stream WS handshake carries
  the bearer ``session_id`` as a query parameter.

Without redaction ``backend.log`` would hold replayable credentials. The WS
tests must emit uvicorn's real templates on ``uvicorn.error``: posting a WS
shape to ``uvicorn.access`` is a combination uvicorn never produces and would
mask the real leak.
"""

from __future__ import annotations

import io
import logging
from contextlib import contextmanager
from typing import Iterator

import pytest
import uvicorn.logging as uvicorn_logging

from claude_hub.auth.log_security import (
    REDACTED_QUERY_VALUE,
    SENSITIVE_QUERY_PARAMS,
    install_redacting_access_formatter,
    redact_sensitive_query,
)

R = REDACTED_QUERY_VALUE
_CLIENT = "203.0.113.9:5512"
_ACCESS_FMT = '%(client_addr)s - "%(request_line)s" %(status_code)s'
_DEFAULT_FMT = "%(levelprefix)s %(message)s"

# Exact templates uvicorn 0.42 emits (protocols/websockets/*_impl.py).
_WS_ACCEPTED_TEMPLATE = '%s - "WebSocket %s" [accepted]'
_WS_403_TEMPLATE = '%s - "WebSocket %s" 403'
_WS_RESPONSE_TEMPLATE = '%s - "WebSocket %s" %d'
# Exact HTTP access template uvicorn emits on uvicorn.access.
_HTTP_ACCESS_TEMPLATE = '%s - "%s %s HTTP/%s" %d'


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "/api/auth/callback?code=abc&state=xyz",
            f"/api/auth/callback?code={R}&state={R}",
        ),
        (
            "/api/terminal/ws/tab?session_id=SECRET&cols=120",
            f"/api/terminal/ws/tab?session_id={R}&cols=120",
        ),
        # Non-sensitive values, names and ordering are preserved.
        (
            "/api/x?foo=bar&code=abc&baz=qux",
            f"/api/x?foo=bar&code={R}&baz=qux",
        ),
        # Parameter names are matched case-insensitively but kept verbatim.
        ("/cb?CODE=abc&StAtE=xyz", f"/cb?CODE={R}&StAtE={R}"),
        # A percent-encoded credential is removed wholesale.
        ("/cb?code=ab%2Fcd%3D&state=s", f"/cb?code={R}&state={R}"),
        # An empty value is still masked.
        ("/cb?code=&x=1", f"/cb?code={R}&x=1"),
        # Absolute-URL targets are handled too.
        (
            "https://hub.example.com/api/auth/callback?code=z&state=q",
            f"https://hub.example.com/api/auth/callback?code={R}&state={R}",
        ),
        # Targets without a sensitive parameter are returned unchanged.
        ("/api/x?foo=bar", "/api/x?foo=bar"),
        ("/health", "/health"),
    ],
)
def test_redact_sensitive_query(raw: str, expected: str) -> None:
    assert redact_sensitive_query(raw) == expected


def test_sensitive_param_set_covers_review_targets() -> None:
    assert {"code", "state", "session_id"} <= SENSITIVE_QUERY_PARAMS


@contextmanager
def _isolated_logger(name: str, *, propagate: bool | None = None) -> Iterator[logging.Logger]:
    """Temporarily own a logger's handlers/filters, restoring global state."""
    logger = logging.getLogger(name)
    saved_handlers = logger.handlers[:]
    saved_filters = logger.filters[:]
    saved_level = logger.level
    saved_propagate = logger.propagate
    logger.handlers = []
    logger.filters = []
    logger.setLevel(logging.INFO)
    if propagate is not None:
        logger.propagate = propagate
    try:
        yield logger
    finally:
        logger.handlers = saved_handlers
        logger.filters = saved_filters
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


def _stream_handler(formatter: logging.Formatter) -> tuple[io.StringIO, logging.Handler]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    return stream, handler


def _log_http_access(full_path: str) -> None:
    """Emit uvicorn's real HTTP access record on uvicorn.access."""
    logging.getLogger("uvicorn.access").info(
        _HTTP_ACCESS_TEMPLATE,
        _CLIENT,
        "GET",
        full_path,
        "1.1",
        400,
    )


def _log_ws_handshake(template: str, full_path: str, *extra: object) -> None:
    """Emit one of uvicorn 0.42's real WS handshake records on uvicorn.error."""
    logging.getLogger("uvicorn.error").info(template, _CLIENT, full_path, *extra)


def _has_redacting_filter(logger_name: str, *, websocket_only: bool) -> bool:
    logger = logging.getLogger(logger_name)
    return any(
        getattr(f, "_claude_hub_redacts_query", False)
        and getattr(f, "websocket_only", None) is websocket_only
        for f in logger.filters
    )


# --- HTTP access records on uvicorn.access --------------------------------


def test_access_formatter_redacts_oauth_query() -> None:
    with _isolated_logger("uvicorn.access") as access_logger:
        stream, handler = _stream_handler(uvicorn_logging.AccessFormatter(_ACCESS_FMT))
        access_logger.addHandler(handler)
        install_redacting_access_formatter()

        _log_http_access("/api/auth/callback?code=TOPSECRETCODE&state=TOPSECRETSTATE&nonce=keep")

        rendered = stream.getvalue()
        assert "TOPSECRETCODE" not in rendered
        assert "TOPSECRETSTATE" not in rendered
        assert "nonce=keep" in rendered
        assert REDACTED_QUERY_VALUE in rendered
        # The installed handler formatter is itself the redacting class.
        assert getattr(handler.formatter, "_claude_hub_redacts_query", False) is True


def test_logger_filter_redacts_through_a_generic_formatter() -> None:
    """A non-AccessFormatter handler (e.g. an ancestor/file handler) is still
    protected: the logger-level filter rewrites the record before dispatch."""
    with _isolated_logger("uvicorn.access") as access_logger:
        stream, handler = _stream_handler(logging.Formatter("%(message)s"))
        access_logger.addHandler(handler)
        install_redacting_access_formatter()
        _log_http_access("/api/auth/callback?code=GENERICSECRET&state=S")
        rendered = stream.getvalue()
        assert "GENERICSECRET" not in rendered
        assert REDACTED_QUERY_VALUE in rendered


# --- WebSocket handshake records on uvicorn.error (the real F1 leak) ------


def test_uvicorn_error_installed_handler_redacts_ws_accepted_and_403() -> None:
    with _isolated_logger("uvicorn.error", propagate=False) as error_logger:
        # Production handler class/formatter for uvicorn.error records.
        stream, handler = _stream_handler(uvicorn_logging.DefaultFormatter(_DEFAULT_FMT))
        error_logger.addHandler(handler)
        install_redacting_access_formatter()

        _log_ws_handshake(
            _WS_ACCEPTED_TEMPLATE,
            "/api/terminal/ws/tab-1?session_id=BEARERACCEPTED&foo=1",
        )
        _log_ws_handshake(
            _WS_403_TEMPLATE,
            "/api/terminal/ws/tab-2?session_id=BEARER403&foo=2",
        )

        rendered = stream.getvalue()
        assert "BEARERACCEPTED" not in rendered
        assert "BEARER403" not in rendered
        assert "foo=1" in rendered and "foo=2" in rendered
        assert rendered.count(REDACTED_QUERY_VALUE) == 2
        assert "WebSocket" in rendered


def test_uvicorn_error_redacts_ws_response_start_template() -> None:
    """The third WS template ('%s - "WebSocket %s" %d', 3 positional args)."""
    with _isolated_logger("uvicorn.error", propagate=False) as error_logger:
        stream, handler = _stream_handler(uvicorn_logging.DefaultFormatter(_DEFAULT_FMT))
        error_logger.addHandler(handler)
        install_redacting_access_formatter()

        _log_ws_handshake(
            _WS_RESPONSE_TEMPLATE,
            "/api/agent/stream?session_id=BEARERRESP&cols=80",
            200,
        )

        rendered = stream.getvalue()
        assert "BEARERRESP" not in rendered
        assert "cols=80" in rendered
        assert "200" in rendered


def test_uvicorn_error_redacts_through_parent_handler() -> None:
    """Real default-config topology: uvicorn.error has no own handler and
    propagates to the uvicorn parent, whose DefaultFormatter renders it.
    The record must already be redacted when it reaches the ancestor."""
    with (
        _isolated_logger("uvicorn.error", propagate=True) as error_logger,
        _isolated_logger("uvicorn", propagate=False) as parent_logger,
    ):
        assert error_logger.handlers == []
        stream, handler = _stream_handler(uvicorn_logging.DefaultFormatter(_DEFAULT_FMT))
        parent_logger.addHandler(handler)
        install_redacting_access_formatter()

        _log_ws_handshake(
            _WS_ACCEPTED_TEMPLATE,
            "/api/terminal/ws/t?session_id=PARENTBEARER&cols=80",
        )

        rendered = stream.getvalue()
        assert "PARENTBEARER" not in rendered
        assert "cols=80" in rendered
        assert REDACTED_QUERY_VALUE in rendered


def test_uvicorn_error_non_websocket_records_pass_through_untouched() -> None:
    """uvicorn.error is a shared logger: startup/lifespan/exception records
    must not be altered, even with a query-shaped string at arg index 1."""
    with _isolated_logger("uvicorn.error", propagate=False) as error_logger:
        stream, handler = _stream_handler(logging.Formatter("%(message)s"))
        error_logger.addHandler(handler)
        install_redacting_access_formatter()

        error_logger.info("Started server process [%d]", 4242)
        error_logger.info(
            "Uvicorn running on %s (Press %s)",
            "http://1.2.3.4/?session_id=KEEPME",
            "CTRL+C",
        )

        rendered = stream.getvalue()
        assert "4242" in rendered
        assert "session_id=KEEPME" in rendered
        assert "CTRL+C" in rendered
        assert REDACTED_QUERY_VALUE not in rendered


# --- install / idempotency ------------------------------------------------


def test_install_is_idempotent() -> None:
    with (
        _isolated_logger("uvicorn.access") as access_logger,
        _isolated_logger("uvicorn.error", propagate=False) as error_logger,
    ):
        stream, handler = _stream_handler(logging.Formatter("%(message)s"))
        access_logger.addHandler(handler)
        error_stream, error_handler = _stream_handler(
            uvicorn_logging.DefaultFormatter(_DEFAULT_FMT)
        )
        error_logger.addHandler(error_handler)

        install_redacting_access_formatter()
        # A second install must not stack filters, or raise.
        install_redacting_access_formatter()
        assert len(access_logger.filters) == 1
        assert len(error_logger.filters) == 1

        _log_http_access("/api/auth/callback?code=ONCE&state=TWICE")
        _log_ws_handshake(
            _WS_ACCEPTED_TEMPLATE,
            "/api/terminal/ws/t?session_id=THRICE",
        )

        assert stream.getvalue().count(REDACTED_QUERY_VALUE) == 2
        assert "ONCE" not in stream.getvalue() and "TWICE" not in stream.getvalue()
        assert "THRICE" not in error_stream.getvalue()
        assert REDACTED_QUERY_VALUE in error_stream.getvalue()


def test_non_access_logger_is_untouched() -> None:
    """Records on other loggers keep their args verbatim."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    other = logging.getLogger("some.other.access-like.logger")
    other.addHandler(handler)
    other.setLevel(logging.INFO)
    try:
        other.info("GET /cb?code=KEEPME %s", "x")
        assert "code=KEEPME" in stream.getvalue()
    finally:
        other.removeHandler(handler)


def test_main_installs_redaction_on_import() -> None:
    import claude_hub.main  # noqa: F401

    # HTTP AccessFormatter class is replaced for post-import dictConfig.
    assert getattr(uvicorn_logging.AccessFormatter, "_claude_hub_redacts_query", False) is True
    # Logger filters cover both production record paths at import time.
    assert _has_redacting_filter("uvicorn.access", websocket_only=False)
    assert _has_redacting_filter("uvicorn.error", websocket_only=True)
