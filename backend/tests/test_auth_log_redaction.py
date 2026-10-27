"""Tests for redacting OAuth/WS secrets out of the uvicorn access log.

The uvicorn access logger renders the full request target, including the raw
query string. The OAuth callback puts ``code``/``state`` there and the terminal
WebSocket handshake carries the bearer ``session_id`` as a query parameter, so
without redaction ``backend.log`` would hold replayable credentials.
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
_ACCESS_FMT = '%(client_addr)s - "%(request_line)s" %(status_code)s'


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
def _isolated_access_logger() -> Iterator[tuple[io.StringIO, logging.Handler]]:
    """Temporarily own the uvicorn.access logger, restoring global state."""
    access_logger = logging.getLogger("uvicorn.access")
    saved_handlers = access_logger.handlers[:]
    saved_filters = access_logger.filters[:]
    saved_level = access_logger.level
    saved_formatter_cls = uvicorn_logging.AccessFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(uvicorn_logging.AccessFormatter(_ACCESS_FMT))
    access_logger.handlers = [handler]
    access_logger.filters = []
    access_logger.setLevel(logging.INFO)
    try:
        yield stream, handler
    finally:
        access_logger.handlers = saved_handlers
        access_logger.filters = saved_filters
        access_logger.setLevel(saved_level)
        setattr(uvicorn_logging, "AccessFormatter", saved_formatter_cls)


def _log_access(full_path: str) -> None:
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "203.0.113.9:5512",
        "GET",
        full_path,
        "1.1",
        400,
    )


def test_access_formatter_redacts_oauth_query() -> None:
    with _isolated_access_logger() as (stream, handler):
        install_redacting_access_formatter()

        _log_access("/api/auth/callback?code=TOPSECRETCODE&state=TOPSECRETSTATE&nonce=keep")

        rendered = stream.getvalue()
        assert "TOPSECRETCODE" not in rendered
        assert "TOPSECRETSTATE" not in rendered
        assert "nonce=keep" in rendered
        assert REDACTED_QUERY_VALUE in rendered
        # The installed handler formatter is itself the redacting class.
        assert getattr(handler.formatter, "_claude_hub_redacts_query", False) is True


def test_access_formatter_redacts_websocket_session_id() -> None:
    with _isolated_access_logger() as (stream, _handler):
        install_redacting_access_formatter()

        _log_access("/api/terminal/ws/tab-1?session_id=BEARERSECRET&foo=1")

        rendered = stream.getvalue()
        assert "BEARERSECRET" not in rendered
        assert "foo=1" in rendered


def test_install_is_idempotent() -> None:
    with _isolated_access_logger() as (stream, _handler):
        install_redacting_access_formatter()
        # A second install must not stack formatters or filters, or raise.
        install_redacting_access_formatter()
        access_logger = logging.getLogger("uvicorn.access")
        redacting_filters = [
            f for f in access_logger.filters if getattr(f, "_claude_hub_redacts_query", False)
        ]
        assert len(redacting_filters) == 1

        _log_access("/api/auth/callback?code=ONCE&state=TWICE")

        rendered = stream.getvalue()
        assert rendered.count(REDACTED_QUERY_VALUE) == 2
        assert "ONCE" not in rendered and "TWICE" not in rendered


def test_logger_filter_redacts_through_a_generic_formatter() -> None:
    """A non-AccessFormatter handler (e.g. an ancestor/file handler) is still
    protected: the logger-level filter rewrites the record before dispatch."""
    access_logger = logging.getLogger("uvicorn.access")
    saved_filters = access_logger.filters[:]
    saved_level = access_logger.level
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    # Plain formatter rendering the access template, deliberately NOT the
    # redacting AccessFormatter subclass.
    handler.setFormatter(logging.Formatter("%(message)s"))
    access_logger.addHandler(handler)
    access_logger.setLevel(logging.INFO)
    install_redacting_access_formatter()
    try:
        _log_access("/api/terminal/ws/t?session_id=GENERICSECRET&cols=80")
        rendered = stream.getvalue()
        assert "GENERICSECRET" not in rendered
        assert "cols=80" in rendered
        assert REDACTED_QUERY_VALUE in rendered
    finally:
        access_logger.removeHandler(handler)
        access_logger.filters = saved_filters
        access_logger.setLevel(saved_level)


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


def test_main_installs_redacting_formatter_on_import() -> None:
    import claude_hub.main  # noqa: F401

    assert getattr(uvicorn_logging.AccessFormatter, "_claude_hub_redacts_query", False) is True
