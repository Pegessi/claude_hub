"""Tests for the unauthenticated non-loopback listen guard.

The decision function is pure (no DNS, no sockets); enforcement combines the
verdict with auth/opt-in settings. A small in-process lifespan test confirms
an unsafe configuration aborts startup before the app begins serving.
"""

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from claude_hub.config import settings
from claude_hub.services.listen_guard import (
    InsecureListenError,
    enforce_listen_policy,
    enforce_settings_listen_policy,
    evaluate_listen_host,
)

# ── Pure host classification ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.1.2.3",
        "::1",
        "[::1]",
        "localhost",
        "LOCALHOST",
        "ip6-localhost",
    ],
)
def test_loopback_hosts_allowed(host: str) -> None:
    verdict = evaluate_listen_host(host)
    assert verdict.allowed is True
    assert verdict.reason == "loopback"


@pytest.mark.parametrize("host", ["169.254.1.1", "fe80::1", "fe80::abcd:1234"])
def test_link_local_hosts_allowed(host: str) -> None:
    # Consistency with Settings.is_local_network_ip, which exempts
    # link-local peers; link-local is not routable beyond the segment.
    verdict = evaluate_listen_host(host)
    assert verdict.allowed is True
    assert verdict.reason == "link-local"


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "::",
        "*",
        "10.0.0.5",
        "192.168.1.10",
        "172.16.0.1",
        "203.0.113.5",
        "[2001:db8::1]",
        "hub.example.com",
        "my-laptop.local",
        "",
        "   ",
        None,
    ],
)
def test_exposed_hosts_refused(host: str | None) -> None:
    verdict = evaluate_listen_host(host)
    assert verdict.allowed is False
    assert verdict.reason in {"wildcard", "non-local-ip", "hostname", "empty"}


def test_wildcard_reason_is_explicit() -> None:
    assert evaluate_listen_host("0.0.0.0").reason == "wildcard"
    assert evaluate_listen_host("::").reason == "wildcard"
    assert evaluate_listen_host("hub.example.com").reason == "hostname"
    assert evaluate_listen_host("10.0.0.1").reason == "non-local-ip"


# ── Policy enforcement ──────────────────────────────────────────────────────


def test_unauthenticated_wildcard_refuses_with_remediation() -> None:
    with pytest.raises(InsecureListenError) as exc_info:
        enforce_listen_policy("0.0.0.0", auth_enabled=False, allow_insecure=False)
    message = str(exc_info.value)
    assert "FEISHU_APP_ID" in message
    assert "CLAUDE_HUB_ALLOW_INSECURE_LISTEN=1" in message
    assert "127.0.0.1" in message


def test_unauthenticated_lan_ip_refuses() -> None:
    with pytest.raises(InsecureListenError):
        enforce_listen_policy("192.168.1.20", auth_enabled=False, allow_insecure=False)


def test_authenticated_server_may_listen_anywhere() -> None:
    verdict = enforce_listen_policy("0.0.0.0", auth_enabled=True, allow_insecure=False)
    assert verdict.allowed is False  # host still classified exposed
    # ...but no exception is raised.


def test_loopback_unauthenticated_is_warning_free() -> None:
    verdict = enforce_listen_policy("127.0.0.1", auth_enabled=False, allow_insecure=False)
    assert verdict.allowed is True
    verdict = enforce_listen_policy("::1", auth_enabled=False, allow_insecure=False)
    assert verdict.allowed is True


def test_explicit_opt_in_passes(caplog: pytest.LogCaptureFixture) -> None:
    verdict = enforce_listen_policy("0.0.0.0", auth_enabled=False, allow_insecure=True)
    assert verdict.allowed is False
    assert any("CLAUDE_HUB_ALLOW_INSECURE_LISTEN" in record.message for record in caplog.records)


# ── Settings wiring ──────────────────────────────────────────────────────────


def _set_auth(monkeypatch: MonkeyPatch, *, enabled: bool) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "app-id" if enabled else None)
    monkeypatch.setattr(settings, "feishu_app_secret", "secret" if enabled else None)


def test_settings_policy_bind_host_precedence(monkeypatch: MonkeyPatch) -> None:
    _set_auth(monkeypatch, enabled=False)
    monkeypatch.setattr(settings, "allow_insecure_listen", False)
    monkeypatch.setattr(settings, "bind_host", "0.0.0.0")
    monkeypatch.setattr(settings, "host", "127.0.0.1")
    with pytest.raises(InsecureListenError):
        enforce_settings_listen_policy()


def test_settings_policy_falls_back_to_host(monkeypatch: MonkeyPatch) -> None:
    _set_auth(monkeypatch, enabled=False)
    monkeypatch.setattr(settings, "allow_insecure_listen", False)
    monkeypatch.setattr(settings, "bind_host", None)
    monkeypatch.setattr(settings, "host", "127.0.0.1")
    verdict = enforce_settings_listen_policy()
    assert verdict.allowed is True


def test_settings_policy_opt_in(monkeypatch: MonkeyPatch) -> None:
    _set_auth(monkeypatch, enabled=False)
    monkeypatch.setattr(settings, "allow_insecure_listen", True)
    monkeypatch.setattr(settings, "bind_host", "0.0.0.0")
    enforce_settings_listen_policy()  # no exception


def test_settings_policy_auth_enabled(monkeypatch: MonkeyPatch) -> None:
    _set_auth(monkeypatch, enabled=True)
    monkeypatch.setattr(settings, "allow_insecure_listen", False)
    monkeypatch.setattr(settings, "bind_host", "0.0.0.0")
    enforce_settings_listen_policy()  # no exception


# ── Lifespan integration (no socket bound) ──────────────────────────────────


def test_lifespan_aborts_for_unsafe_bind(monkeypatch: MonkeyPatch) -> None:
    _set_auth(monkeypatch, enabled=False)
    monkeypatch.setattr(settings, "allow_insecure_listen", False)
    monkeypatch.setattr(settings, "bind_host", "0.0.0.0")

    @asynccontextmanager
    async def guarded_lifespan(app: FastAPI):
        enforce_settings_listen_policy()
        yield

    with pytest.raises(Exception):  # TestClient surfaces startup failure
        with TestClient(FastAPI(lifespan=guarded_lifespan)):
            pass


def test_lifespan_starts_for_loopback(monkeypatch: MonkeyPatch) -> None:
    _set_auth(monkeypatch, enabled=False)
    monkeypatch.setattr(settings, "allow_insecure_listen", False)
    monkeypatch.setattr(settings, "bind_host", None)
    monkeypatch.setattr(settings, "host", "127.0.0.1")

    @asynccontextmanager
    async def guarded_lifespan(app: FastAPI):
        enforce_settings_listen_policy()
        yield

    with TestClient(FastAPI(lifespan=guarded_lifespan)) as client:
        assert client.get("/").status_code in (404, 200)
