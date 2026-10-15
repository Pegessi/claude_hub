"""Checks for the shared proxy decision and the HTTPX client factory built on it."""

from __future__ import annotations

from typing import Mapping, Optional
from urllib.parse import urlsplit

import httpx
import pytest

from claude_hub.services.proxy_rules import (
    bypasses_proxy,
    create_async_client,
    environment_proxy_for_url,
)

# Corporate Linux images ship these IPv6 CIDR entries next to the ordinary
# private ranges. HTTPX 0.28 cannot translate them into URL patterns, so a
# client that lets HTTPX read NO_PROXY fails while it is being constructed.
POISONED_NO_PROXY = (
    "localhost,.byted.org,byted.org,127.0.0.1,127.0.0.0/8,169.254.0.0/16,"
    "172.16.0.0/12,192.168.0.0/16,10.0.0.0/8,::1,fe80::/10,fd00::/8"
)
PROXY_URL = "http://sys-proxy.example.test:8118"
_PROXY_ENV_NAMES = (
    "http_proxy",
    "HTTP_PROXY",
    "https_proxy",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
    "no_proxy",
    "NO_PROXY",
)


def resolved_proxy(client: httpx.AsyncClient, url: str) -> Optional[str]:
    """Report the proxy origin an existing client would dial for ``url``."""
    pool = getattr(client._transport_for_url(httpx.URL(url)), "_pool", None)
    proxy_url = getattr(pool, "_proxy_url", None)
    if proxy_url is None:
        return None
    return f"{proxy_url.scheme.decode()}://{proxy_url.host.decode()}:{proxy_url.port}"


def apply_poisoned_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduce the deployment environment that breaks HTTPX client construction."""
    for name in _PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("http_proxy", PROXY_URL)
    monkeypatch.setenv("https_proxy", PROXY_URL)
    monkeypatch.setenv("no_proxy", POISONED_NO_PROXY)


def test_poisoned_no_proxy_still_breaks_a_default_httpx_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apply_poisoned_proxy_environment(monkeypatch)

    with pytest.raises(httpx.InvalidURL):
        httpx.AsyncClient(base_url="https://open.feishu.cn", timeout=15.0)


def test_factory_builds_a_proxied_client_under_the_poisoned_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apply_poisoned_proxy_environment(monkeypatch)

    client = create_async_client("https://open.feishu.cn", timeout=15.0)

    assert resolved_proxy(client, "https://open.feishu.cn/open-apis/x") == PROXY_URL
    assert client.trust_env is True


def test_factory_builds_a_direct_client_for_bypassed_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apply_poisoned_proxy_environment(monkeypatch)

    for bypassed in ("https://hub.byted.org", "http://10.1.2.3:9000", "http://[fd00::1]"):
        client = create_async_client(bypassed, timeout=1.0)
        assert resolved_proxy(client, bypassed) is None, bypassed
        # Environment CA settings must survive the transport that suppresses
        # HTTPX's proxy mounts.
        assert client.trust_env is True


def test_real_world_no_proxy_routes_public_and_internal_targets() -> None:
    proxies: Mapping[str, str] = {
        "http": PROXY_URL,
        "https": PROXY_URL,
        "no": POISONED_NO_PROXY,
    }

    assert environment_proxy_for_url("https://open.feishu.cn/open-apis/x", proxies) == PROXY_URL
    assert environment_proxy_for_url("https://api.anthropic.com", proxies) == PROXY_URL
    assert environment_proxy_for_url("https://hub.byted.org/a", proxies) is None
    assert environment_proxy_for_url("http://127.0.0.1:8173/", proxies) is None
    assert environment_proxy_for_url("http://[fd00::1]/", proxies) is None
    assert environment_proxy_for_url("http://[fe80::1]/", proxies) is None
    assert environment_proxy_for_url("http://10.1.2.3/", proxies) is None
    assert environment_proxy_for_url("http://localhost:5173/", proxies) is None


def test_environment_proxy_prefers_the_scheme_then_the_catch_all() -> None:
    assert environment_proxy_for_url("https://a.example", {"https": "http://s:1"}) == "http://s:1"
    assert environment_proxy_for_url("https://a.example", {"all": "http://a:2"}) == "http://a:2"
    assert (
        environment_proxy_for_url("https://a.example", {"https": "http://s:1", "all": "http://a:2"})
        == "http://s:1"
    )
    assert environment_proxy_for_url("https://a.example", {"https": "   "}) is None
    assert environment_proxy_for_url("https://a.example", {}) is None


def test_environment_proxy_requires_an_absolute_http_url() -> None:
    for rejected in ("open.feishu.cn", "/open-apis", "ftp://host/x", "https:///x"):
        with pytest.raises(ValueError):
            environment_proxy_for_url(rejected, {"https": PROXY_URL})


def test_no_proxy_entry_matching_keeps_its_published_rules() -> None:
    def bypassed(url: str, no_proxy: Optional[str]) -> bool:
        return bypasses_proxy(urlsplit(url), no_proxy)

    assert bypassed("https://anything.example", "*") is True
    assert bypassed("https://a.example", "") is False
    assert bypassed("https://a.example", None) is False
    # Suffix forms, case and a trailing dot all name the same domain.
    assert bypassed("https://api.a.example", ".a.example") is True
    assert bypassed("https://api.a.example", "*.a.example") is True
    assert bypassed("https://API.A.Example.", "a.example") is True
    assert bypassed("https://nota.example", "a.example") is False
    # A port on the entry restricts the match to that port.
    assert bypassed("https://a.example:8443", "a.example:8443") is True
    assert bypassed("https://a.example:8443", "a.example:9000") is False
    assert bypassed("https://a.example", "a.example:443") is True
    assert bypassed("http://a.example", "a.example:80") is True
    # Bracketed IPv6 entries, with and without a port.
    assert bypassed("http://[fd00::1]:8080", "[fd00::1]:8080") is True
    assert bypassed("http://[fd00::1]:8080", "[fd00::1]:9090") is False
    assert bypassed("http://[fd00::1]", "[fd00::/8]") is True
    # CIDR entries for both families.
    assert bypassed("http://10.1.2.3", "10.0.0.0/8") is True
    assert bypassed("http://11.1.2.3", "10.0.0.0/8") is False
    assert bypassed("http://[fe80::abcd]", "fe80::/10") is True
    assert bypassed("http://[2001:db8::1]", "fe80::/10") is False
