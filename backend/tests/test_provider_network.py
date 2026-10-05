from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

import pytest

from claude_hub.services.agent_stream.provider_network import (
    PROVIDER_NETWORK_MODE_ENV,
    ProbeResult,
    ProviderNetworkError,
    ProviderNetworkSelector,
    _codex_login_status,
    _probe_endpoint,
    resolve_provider_endpoint,
)


class RecordingProbe:
    def __init__(self, results: list[ProbeResult]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, Optional[str]]] = []

    async def __call__(self, endpoint: str, proxy: Optional[str]) -> ProbeResult:
        self.calls.append((endpoint, proxy))
        return self.results.pop(0)


def _selection_log_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    prefix = "provider_network_selection "
    return [
        json.loads(record.getMessage()[len(prefix) :])
        for record in caplog.records
        if record.getMessage().startswith(prefix)
    ]


@pytest.mark.asyncio
async def test_auto_prefers_direct_and_removes_all_proxy_spellings() -> None:
    probe = RecordingProbe([ProbeResult(True, "http", 401)])
    selector = ProviderNetworkSelector(probe=probe)
    env = {
        "ANTHROPIC_BASE_URL": "https://provider.example/v1?opaque=query-sensitive",
        "HTTP_PROXY": "http://user:proxy-sensitive@proxy.example:8080",
        "https_proxy": "http://proxy.example:8080",
        "ALL_PROXY": "socks5://proxy.example:1080",
        "NO_PROXY": "localhost",
    }

    selected = await selector.select("claude", env)

    assert probe.calls == [("https://provider.example/v1?opaque=query-sensitive", None)]
    assert "HTTP_PROXY" not in selected
    assert "https_proxy" not in selected
    assert "ALL_PROXY" not in selected
    assert selected["NO_PROXY"] == "localhost"
    assert selected["ANTHROPIC_BASE_URL"] == env["ANTHROPIC_BASE_URL"]


@pytest.mark.asyncio
async def test_success_logs_redacted_route_and_actual_selected_proxy_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(
        logging.INFO,
        logger="claude_hub.services.agent_stream.provider_network",
    )
    probe = RecordingProbe([ProbeResult(False, "tcp"), ProbeResult(True, "http", 403)])
    selector = ProviderNetworkSelector(probe=probe)
    env = {
        "CLAUDE_HUB_TAB_ID": "tab-log-test",
        "CURSOR_API_ENDPOINT": "https://provider.example:8443/private/path?key=query-sensitive",
        "HTTPS_PROXY": "http://proxy-user:proxy-sensitive@proxy.example:7890",
        "PROVIDER_API_KEY": "environment-sensitive",
    }

    selected = await selector.select("cursor", env)

    records = _selection_log_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record["tab_id"] == "tab-log-test"
    assert record["provider"] == "cursor"
    assert record["requested_mode"] == "auto"
    assert record["decision"] == "proxy"
    assert record["endpoint_origin"] == "https://provider.example:8443"
    assert record["direct"] == {
        "attempted": True,
        "reachable": False,
        "phase": "tcp",
        "status_code": None,
    }
    assert record["proxy"] == {
        "attempted": True,
        "reachable": True,
        "phase": "http",
        "status_code": 403,
    }
    assert record["returned_env_has_proxy"] is True
    assert selected["HTTPS_PROXY"] == env["HTTPS_PROXY"]
    log_text = caplog.text
    assert "private/path" not in log_text
    assert "query-sensitive" not in log_text
    assert "proxy-sensitive" not in log_text
    assert "proxy-user" not in log_text
    assert "environment-sensitive" not in log_text


@pytest.mark.asyncio
async def test_direct_log_matches_proxy_free_returned_environment(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(
        logging.INFO,
        logger="claude_hub.services.agent_stream.provider_network",
    )
    selector = ProviderNetworkSelector(probe=RecordingProbe([ProbeResult(True, "http", 401)]))
    env = {
        "CURSOR_API_ENDPOINT": "https://cursor.example/hidden?key=sensitive",
        "ALL_PROXY": "socks5://proxy.example:1080",
    }

    selected = await selector.select("cursor", env)

    record = _selection_log_records(caplog)[0]
    assert record["decision"] == "direct"
    assert record["endpoint_origin"] == "https://cursor.example:443"
    assert record["returned_env_has_proxy"] is False
    assert "ALL_PROXY" not in selected
    assert "hidden" not in caplog.text
    assert "sensitive" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env", "expected_decision", "expected_level"),
    [
        (
            {PROVIDER_NETWORK_MODE_ENV: "inherit", "HTTPS_PROXY": "http://proxy.example"},
            "explicit_inherit",
            logging.INFO,
        ),
        (
            {"CLAUDE_CODE_USE_BEDROCK": "1", "HTTPS_PROXY": "http://proxy.example"},
            "compatibility_inherit",
            logging.WARNING,
        ),
    ],
)
async def test_inherit_logs_not_attempted_without_environment_values(
    caplog: pytest.LogCaptureFixture,
    env: dict[str, str],
    expected_decision: str,
    expected_level: int,
) -> None:
    caplog.set_level(
        logging.INFO,
        logger="claude_hub.services.agent_stream.provider_network",
    )
    selector = ProviderNetworkSelector(probe=RecordingProbe([]))

    selected = await selector.select("claude", env)

    record = _selection_log_records(caplog)[0]
    assert record["decision"] == expected_decision
    assert record["endpoint_origin"] is None
    assert record["direct"] == {
        "attempted": False,
        "reachable": None,
        "phase": "not_attempted",
        "status_code": None,
    }
    assert record["proxy"] == record["direct"]
    assert record["returned_env_has_proxy"] is True
    assert selected == env
    assert caplog.records[-1].levelno == expected_level
    assert "proxy.example" not in caplog.text


@pytest.mark.asyncio
async def test_selection_never_mutates_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://parent.example:7890")
    original = dict(os.environ)
    selector = ProviderNetworkSelector(probe=RecordingProbe([ProbeResult(True, "http", 200)]))

    selected = await selector.select(
        "cursor",
        {
            "CURSOR_API_ENDPOINT": "https://cursor.example",
            "HTTPS_PROXY": "http://parent.example:7890",
        },
    )

    assert os.environ == original
    assert "HTTPS_PROXY" not in selected


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "direct"])
async def test_reachable_direct_route_ignores_malformed_proxy(mode: str) -> None:
    probe = RecordingProbe([ProbeResult(True, "http", 200)])
    selector = ProviderNetworkSelector(probe=probe)
    env = {
        PROVIDER_NETWORK_MODE_ENV: mode,
        "CURSOR_API_ENDPOINT": "https://cursor.example",
        "HTTPS_PROXY": "not-a-proxy-url",
    }

    selected = await selector.select("cursor", env)

    assert probe.calls == [("https://cursor.example", None)]
    assert "HTTPS_PROXY" not in selected


@pytest.mark.asyncio
async def test_auto_uses_configured_proxy_only_after_network_failure() -> None:
    probe = RecordingProbe([ProbeResult(False, "tcp"), ProbeResult(True, "http", 403)])
    selector = ProviderNetworkSelector(probe=probe)
    env = {
        "CURSOR_API_ENDPOINT": "https://cursor.example/api",
        "HTTPS_PROXY": "http://proxy.example:7890",
    }

    selected = await selector.select("cursor", env)

    assert probe.calls == [
        ("https://cursor.example/api", None),
        ("https://cursor.example/api", "http://proxy.example:7890"),
    ]
    assert selected["HTTPS_PROXY"] == env["HTTPS_PROXY"]


@pytest.mark.asyncio
async def test_both_routes_fail_with_redacted_classified_error() -> None:
    probe = RecordingProbe([ProbeResult(False, "dns"), ProbeResult(False, "timeout")])
    selector = ProviderNetworkSelector(probe=probe)
    env = {
        "ANTHROPIC_BASE_URL": "https://provider.example/v1?api_key=query-sensitive",
        "HTTPS_PROXY": "http://proxy-user:proxy-sensitive@proxy.example:7890",
    }

    with pytest.raises(ProviderNetworkError) as caught:
        await selector.select("claude", env)

    message = str(caught.value)
    assert "provider.example:443" in message
    assert "direct dns failure" in message
    assert "proxy timeout failure" in message
    assert "query-sensitive" not in message
    assert "proxy-sensitive" not in message
    assert "proxy-user" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 429])
async def test_http_application_errors_prove_direct_reachability(status: int) -> None:
    probe = RecordingProbe([ProbeResult(True, "http", status)])
    selector = ProviderNetworkSelector(probe=probe)
    env = {
        "ANTHROPIC_BASE_URL": "https://provider.example",
        "HTTPS_PROXY": "http://proxy.example:7890",
    }

    selected = await selector.select("claude", env)

    assert probe.calls == [("https://provider.example", None)]
    assert "HTTPS_PROXY" not in selected


@pytest.mark.asyncio
async def test_explicit_modes_override_automatic_selection() -> None:
    direct_probe = RecordingProbe([ProbeResult(True, "http", 200)])
    direct_selector = ProviderNetworkSelector(probe=direct_probe)
    direct_env = {
        PROVIDER_NETWORK_MODE_ENV: "direct",
        "CURSOR_API_ENDPOINT": "https://cursor.example",
        "HTTPS_PROXY": "http://proxy.example:7890",
    }
    selected_direct = await direct_selector.select("cursor", direct_env)
    assert direct_probe.calls == [("https://cursor.example", None)]
    assert "HTTPS_PROXY" not in selected_direct

    proxy_probe = RecordingProbe([ProbeResult(True, "http", 200)])
    proxy_selector = ProviderNetworkSelector(probe=proxy_probe)
    proxy_env = {**direct_env, PROVIDER_NETWORK_MODE_ENV: "proxy"}
    selected_proxy = await proxy_selector.select("cursor", proxy_env)
    assert proxy_probe.calls == [("https://cursor.example", "http://proxy.example:7890")]
    assert selected_proxy["HTTPS_PROXY"] == "http://proxy.example:7890"


@pytest.mark.asyncio
async def test_forced_proxy_never_falls_back_to_direct() -> None:
    selector = ProviderNetworkSelector(probe=RecordingProbe([]))
    env = {
        PROVIDER_NETWORK_MODE_ENV: "proxy",
        "CURSOR_API_ENDPOINT": "https://cursor.example",
    }

    with pytest.raises(ProviderNetworkError, match="proxy not configured"):
        await selector.select("cursor", env)


@pytest.mark.asyncio
async def test_no_proxy_prevents_proxy_probe() -> None:
    probe = RecordingProbe([ProbeResult(False, "timeout")])
    selector = ProviderNetworkSelector(probe=probe)
    env = {
        "ANTHROPIC_BASE_URL": "https://api.example.test/v1",
        "HTTPS_PROXY": "http://proxy.example:7890",
        "NO_PROXY": ".example.test",
    }

    with pytest.raises(ProviderNetworkError, match="bypassed by NO_PROXY"):
        await selector.select("claude", env)
    assert probe.calls == [("https://api.example.test/v1", None)]


@pytest.mark.asyncio
async def test_lowercase_proxy_and_no_proxy_are_supported() -> None:
    probe = RecordingProbe([ProbeResult(False, "tcp"), ProbeResult(True, "http", 200)])
    selector = ProviderNetworkSelector(probe=probe)
    env = {
        "CURSOR_API_ENDPOINT": "https://cursor.example",
        "https_proxy": "http://right.example:2",
        "no_proxy": "localhost",
    }

    selected = await selector.select("cursor", env)

    assert probe.calls[1][1] == "http://right.example:2"
    assert selected == env


@pytest.mark.asyncio
async def test_conflicting_proxy_case_variants_fail_closed_after_direct_failure() -> None:
    selector = ProviderNetworkSelector(probe=RecordingProbe([ProbeResult(False, "tcp")]))
    env = {
        "CURSOR_API_ENDPOINT": "https://cursor.example",
        "HTTPS_PROXY": "http://one.example:1",
        "https_proxy": "http://two.example:2",
    }

    with pytest.raises(ProviderNetworkError, match="conflicting lowercase and uppercase"):
        await selector.select("cursor", env)


@pytest.mark.asyncio
async def test_inherit_skips_unresolved_endpoint_and_probe() -> None:
    probe = RecordingProbe([])
    selector = ProviderNetworkSelector(probe=probe)
    env = {PROVIDER_NETWORK_MODE_ENV: "inherit", "HTTP_PROXY": "http://p:1"}

    assert await selector.select("codex", env) == env
    assert probe.calls == []


@pytest.mark.asyncio
async def test_codex_login_status_resolves_chatgpt_endpoint_and_is_cached(
    tmp_path: Path,
) -> None:
    login_calls = 0
    probe = RecordingProbe([ProbeResult(True, "http", 401)])

    async def login_status(env: dict[str, str]) -> Optional[str]:
        nonlocal login_calls
        login_calls += 1
        return "chatgpt"

    selector = ProviderNetworkSelector(probe=probe, login_status=login_status)
    env = {"HOME": str(tmp_path), "CODEX_HOME": str(tmp_path / "codex")}

    first = await selector.select("codex", env)
    second = await selector.select("codex", env)

    assert login_calls == 1
    assert probe.calls == [("https://chatgpt.com/backend-api/", None)]
    assert first == second == env


@pytest.mark.asyncio
async def test_codex_api_key_status_uses_api_endpoint(tmp_path: Path) -> None:
    async def login_status(env: dict[str, str]) -> Optional[str]:
        return "api_key"

    probe = RecordingProbe([ProbeResult(True, "http", 403)])
    selector = ProviderNetworkSelector(probe=probe, login_status=login_status)
    env = {"HOME": str(tmp_path), "CODEX_HOME": str(tmp_path / "codex")}

    await selector.select("codex", env)

    assert probe.calls == [("https://api.openai.com/v1", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output,expected",
    [
        (b"Logged in using ChatGPT\n", "chatgpt"),
        (b"Logged in using an API key\n", "api_key"),
        (b"Not logged in\n", None),
    ],
)
async def test_official_codex_login_status_parser_does_not_expose_output(
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
    expected: Optional[str],
) -> None:
    class FakeLoginProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return output, b""

        def kill(self) -> None:
            pass

        async def wait(self) -> int:
            return 0

    async def fake_spawn(*args: object, **kwargs: object) -> FakeLoginProcess:
        return FakeLoginProcess()

    monkeypatch.setattr(
        "claude_hub.services.agent_stream.provider_network.shutil.which",
        lambda *args, **kwargs: "/usr/bin/codex",
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

    assert await _codex_login_status({"PATH": "/usr/bin"}) == expected


@pytest.mark.asyncio
async def test_unknown_codex_login_preserves_auto_but_forced_mode_fails(
    tmp_path: Path,
) -> None:
    async def login_status(env: dict[str, str]) -> Optional[str]:
        return None

    probe = RecordingProbe([])
    selector = ProviderNetworkSelector(probe=probe, login_status=login_status)
    env = {"HOME": str(tmp_path), "CODEX_HOME": str(tmp_path / "codex")}

    assert await selector.select("codex", env) == env
    with pytest.raises(ProviderNetworkError, match="forced network mode"):
        await selector.select("codex", {**env, PROVIDER_NETWORK_MODE_ENV: "direct"})
    assert probe.calls == []


def test_codex_requires_actual_configured_endpoint(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    env = {"HOME": str(tmp_path), "CODEX_HOME": str(codex_home)}
    with pytest.raises(ProviderNetworkError, match="cannot determine the effective codex"):
        resolve_provider_endpoint("codex", env)

    (codex_home / "config.toml").write_text(
        "\n".join(
            [
                'model_provider = "relay"',
                "[model_providers.relay]",
                'base_url = "https://relay.example/v1"',
            ]
        ),
        encoding="utf-8",
    )
    assert resolve_provider_endpoint("codex", env) == "https://relay.example/v1"

    (codex_home / "config.toml").write_text(
        "\n".join(
            [
                'profile = "corp"',
                "[profiles.corp]",
                'model_provider = "profile-relay"',
                "[model_providers.profile-relay]",
                'base_url = "https://profile.example/v1"',
            ]
        ),
        encoding="utf-8",
    )
    assert resolve_provider_endpoint("codex", env) == "https://profile.example/v1"

    (codex_home / "config.toml").write_text(
        "\n".join(
            [
                'model_provider = "openai"',
                'openai_base_url = "https://api.example/v1"',
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ProviderNetworkError, match="forced_login_method"):
        resolve_provider_endpoint("codex", env)

    (codex_home / "config.toml").write_text('forced_login_method = "chatgpt"\n', encoding="utf-8")
    assert resolve_provider_endpoint("codex", env) == "https://chatgpt.com/backend-api/"

    (codex_home / "config.toml").write_text(
        'forced_login_method = "api_key"\nopenai_base_url = "https://api.example/v1"\n',
        encoding="utf-8",
    )
    assert resolve_provider_endpoint("codex", env) == "https://api.example/v1"


def test_traex_uses_supported_env_then_actual_config(tmp_path: Path) -> None:
    trae_home = tmp_path / "trae"
    trae_home.mkdir()
    env = {"HOME": str(tmp_path), "TRAE_HOME": str(trae_home)}
    (trae_home / "traecli.toml").write_text(
        "\n".join(
            [
                'model_provider = "corp"',
                "[model_providers.corp]",
                'base_url = "https://config.example/v1"',
            ]
        ),
        encoding="utf-8",
    )
    assert resolve_provider_endpoint("traex", env) == "https://config.example/v1"
    env["TRAE_API_BASE_URL"] = "https://explicit.example/v1"
    assert resolve_provider_endpoint("traex", env) == "https://explicit.example/v1"


@pytest.mark.parametrize(
    "flag",
    ["CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"],
)
def test_claude_managed_cloud_modes_are_not_guessed(flag: str) -> None:
    with pytest.raises(ProviderNetworkError, match="provider-managed"):
        resolve_provider_endpoint("claude", {flag: "true"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env",
    [
        {},
        {"CLAUDE_CODE_USE_BEDROCK": "true"},
        {"CLAUDE_CODE_USE_VERTEX": "1"},
    ],
)
async def test_unresolved_claude_auto_preserves_provider_managed_routing(
    env: dict[str, str],
) -> None:
    probe = RecordingProbe([])
    selector = ProviderNetworkSelector(probe=probe)

    assert await selector.select("claude", env) == env
    assert probe.calls == []


@pytest.mark.asyncio
async def test_unresolved_claude_forced_route_fails() -> None:
    selector = ProviderNetworkSelector(probe=RecordingProbe([]))

    with pytest.raises(ProviderNetworkError, match="forced network mode"):
        await selector.select("claude", {PROVIDER_NETWORK_MODE_ENV: "direct"})


@pytest.mark.asyncio
async def test_configuration_change_uses_a_new_cache_key() -> None:
    probe = RecordingProbe([ProbeResult(True, "http", 200), ProbeResult(True, "http", 200)])
    selector = ProviderNetworkSelector(probe=probe)
    first = {"CURSOR_API_ENDPOINT": "https://one.example"}
    second = {"CURSOR_API_ENDPOINT": "https://two.example"}

    await selector.select("cursor", first)
    await selector.select("cursor", first)
    await selector.select("cursor", second)

    assert probe.calls == [
        ("https://one.example", None),
        ("https://two.example", None),
    ]


@pytest.mark.asyncio
async def test_concurrent_matching_configuration_coalesces_probe() -> None:
    calls = 0
    release = asyncio.Event()

    async def delayed_probe(endpoint: str, proxy: Optional[str]) -> ProbeResult:
        nonlocal calls
        calls += 1
        await release.wait()
        return ProbeResult(True, "http", 200)

    selector = ProviderNetworkSelector(probe=delayed_probe)
    env = {"CURSOR_API_ENDPOINT": "https://cursor.example"}
    tasks = [asyncio.create_task(selector.select("cursor", env)) for _ in range(6)]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks)

    assert calls == 1
    assert all("HTTP_PROXY" not in result for result in results)


@pytest.mark.asyncio
async def test_different_configurations_respect_probe_concurrency_bound() -> None:
    active = 0
    peak = 0
    release = asyncio.Event()

    async def delayed_probe(endpoint: str, proxy: Optional[str]) -> ProbeResult:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await release.wait()
        active -= 1
        return ProbeResult(True, "http", 200)

    selector = ProviderNetworkSelector(probe=delayed_probe, max_concurrent=2)
    tasks = [
        asyncio.create_task(
            selector.select("cursor", {"CURSOR_API_ENDPOINT": f"https://{index}.example"})
        )
        for index in range(5)
    ]
    for _ in range(20):
        if peak == 2:
            break
        await asyncio.sleep(0)
    assert peak == 2
    release.set()
    await asyncio.gather(*tasks)
    assert peak == 2


@pytest.mark.asyncio
async def test_cancelled_only_waiter_does_not_leak_inflight_probe() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_probe(endpoint: str, proxy: Optional[str]) -> ProbeResult:
        started.set()
        await release.wait()
        return ProbeResult(True, "http", 200)

    selector = ProviderNetworkSelector(probe=delayed_probe)
    task = asyncio.create_task(
        selector.select("cursor", {"CURSOR_API_ENDPOINT": "https://cursor.example"})
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    for _ in range(20):
        if not selector._inflight:
            break
        await asyncio.sleep(0)

    assert selector._inflight == {}


@pytest.mark.asyncio
async def test_completed_probe_caches_before_inflight_entry_is_removed() -> None:
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_probe(endpoint: str, proxy: Optional[str]) -> ProbeResult:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return ProbeResult(True, "http", 200)

    selector = ProviderNetworkSelector(probe=delayed_probe)
    env = {"CURSOR_API_ENDPOINT": "https://cursor.example"}
    first_waiter = asyncio.create_task(selector.select("cursor", env))
    await started.wait()
    internal_task = next(iter(selector._inflight.values()))
    release.set()
    await internal_task

    assert selector._inflight == {}
    assert selector._cache
    await selector.select("cursor", env)
    assert calls == 1
    await first_waiter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected_phase",
    [
        (302, "proxy-redirect"),
        (307, "proxy-redirect"),
        (407, "proxy-auth"),
        (502, "proxy-gateway"),
        (503, "proxy-gateway"),
    ],
)
async def test_proxy_generated_http_statuses_do_not_prove_origin(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected_phase: str,
) -> None:
    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def head(self, url: str, **kwargs: object) -> object:
            return type("Response", (), {"status_code": status})()

    monkeypatch.setattr(
        "claude_hub.services.agent_stream.provider_network.httpx.AsyncClient", FakeClient
    )

    result = await _probe_endpoint("https://provider.example/v1", "http://proxy.example:8080")

    assert result == ProbeResult(False, expected_phase, status)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 503])
async def test_direct_http_status_does_not_trigger_proxy_fallback(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def head(self, url: str, **kwargs: object) -> object:
            return type("Response", (), {"status_code": status})()

    monkeypatch.setattr(
        "claude_hub.services.agent_stream.provider_network.httpx.AsyncClient", FakeClient
    )

    assert await _probe_endpoint("https://provider.example/v1", None) == ProbeResult(
        True, "http", status
    )


@pytest.mark.asyncio
async def test_real_head_401_is_reachable_without_credentials() -> None:
    seen_request = b""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal seen_request
        seen_request = await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        result = await _probe_endpoint(f"http://127.0.0.1:{port}/v1?opaque=query-value", None)
    finally:
        server.close()
        await server.wait_closed()

    assert result == ProbeResult(True, "http", 401)
    assert seen_request.startswith(b"HEAD /v1 HTTP/1.1\r\n")
    assert b"opaque" not in seen_request
    assert b"query-value" not in seen_request
