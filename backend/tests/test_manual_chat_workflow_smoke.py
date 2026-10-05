"""Deterministic smoke-boundary tests; no browser, provider, process, or live proc access."""

from __future__ import annotations

import errno
import json
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests import manual_chat_workflow_smoke as smoke


@pytest.fixture(autouse=True)
def forbid_real_children(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a deterministic boundary test attempted a real child or proc probe")

    monkeypatch.setattr(smoke, "_spawn", forbidden)
    monkeypatch.setattr(smoke.guard, "require_runtime_capabilities", forbidden)
    monkeypatch.setattr(smoke.guard, "stop_owned_descendants", forbidden)
    monkeypatch.setattr(smoke, "time", SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda _: None))
    monkeypatch.setattr(smoke, "_browser", forbidden)


class FakeResponse:
    def __init__(self, value=None, *, status=200, body=None, headers=None):
        self.status = status
        self.headers = headers or {"content-type": "application/json"}
        self._body = json.dumps(value).encode() if body is None else body

    def body(self):
        return self._body


class FakeRoute:
    def __init__(self, method, url, response=None):
        self.request = SimpleNamespace(method=method, url=url)
        self.response = response or FakeResponse({})
        self.fetched = []
        self.fulfilled = None
        self.aborted = False

    def fetch(self, **kwargs):
        self.fetched.append(kwargs)
        return self.response

    def fulfill(self, **kwargs):
        self.fulfilled = kwargs

    def abort(self, reason):
        self.aborted = True


def test_environment_is_an_allowlist_with_private_homes(tmp_path):
    parent = {
        "PATH": "/opt/bin:/usr/bin",
        "LANG": "C.UTF-8",
        "HOME": "/real/home",
        "XDG_CONFIG_HOME": "/real/config",
        "CODEX_HOME": "/real/codex",
        "OPENAI_API_KEY": "private-account-marker",
        "CLAUDE_HUB_TOKEN": "private-hub-marker",
        "CLAUDE_HUB_TAB_ID": "other-tab",
        "CLAUDE_HUB_ALLOW_LIVE_RUNTIME": "1",
        "PYTHONPATH": "/installed/main",
        "BASH_ENV": "/real/startup",
        "TMUX": "default-server",
        "HTTPS_PROXY": "http://user:private-proxy-marker@proxy.invalid:8",
    }
    before = dict(parent)
    env = smoke._environment(
        parent,
        tmp_path,
        Path("/candidate/backend"),
        "http://127.0.0.1:43123",
        "owned",
        "real-model",
        "direct",
    )
    assert parent == before
    assert env["PATH"] == parent["PATH"]
    assert env["PYTHONPATH"] == "/candidate/backend"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["CODEX_MODEL"] == "real-model"
    for key in (
        "OPENAI_API_KEY",
        "CLAUDE_HUB_TOKEN",
        "CLAUDE_HUB_TAB_ID",
        "CLAUDE_HUB_ALLOW_LIVE_RUNTIME",
        "BASH_ENV",
        "TMUX",
        "HTTPS_PROXY",
    ):
        assert key not in env
    for key in (
        "HOME",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "TMPDIR",
        "TMUX_TMPDIR",
    ):
        assert Path(env[key]).is_relative_to(tmp_path)
    assert "private-account-marker" not in json.dumps(env)


@pytest.mark.parametrize("mode", ["auto", "proxy"])
def test_proxy_copy_requires_authorization(mode):
    with pytest.raises(RuntimeError, match="explicit"):
        smoke._proxy_inputs({"HTTPS_PROXY": "http://proxy.invalid:8"}, mode, False)


def test_only_standard_proxy_keys_are_copied_and_direct_does_not_copy_them():
    parent = {key: "opaque-value" for key in smoke._PROXY_NAMES}
    parent.update(
        OPENAI_API_KEY="not-proxy", Http_Proxy="not-standard", CLAUDE_HUB_TOKEN="not-proxy"
    )
    before = dict(parent)
    selected = smoke._proxy_inputs(parent, "proxy", True)
    assert set(selected) == set(smoke._PROXY_NAMES)
    assert selected["NO_PROXY"] == "opaque-value"
    assert smoke._proxy_inputs(parent, "direct", False) == {}
    assert parent == before


def test_proxy_mode_rejects_only_no_proxy_configuration():
    with pytest.raises(RuntimeError, match="not configured"):
        smoke._proxy_inputs({"NO_PROXY": "localhost"}, "proxy", True)


@pytest.mark.parametrize(
    "mode,direct_reachable,expected_proxy,probe_count",
    [
        ("auto", True, False, 1),
        ("auto", False, True, 2),
        ("proxy", False, True, 1),
        ("direct", True, False, 1),
    ],
)
async def test_network_reuses_shared_transport_policy(
    monkeypatch,
    tmp_path,
    caplog,
    mode,
    direct_reachable,
    expected_proxy,
    probe_count,
):
    from claude_hub.services.agent_stream import provider_network

    login = AsyncMock(return_value="chatgpt")
    seen = []

    async def probe(endpoint, proxy_url):
        seen.append((endpoint, proxy_url))
        if proxy_url is None:
            # An HTTP 403 still proves transport reachability in the shared policy.
            return provider_network.ProbeResult(
                direct_reachable,
                "http" if direct_reachable else "dns",
                403 if direct_reachable else None,
            )
        return provider_network.ProbeResult(True, "http", 401)

    monkeypatch.setattr(provider_network, "_codex_login_status", login)
    monkeypatch.setattr(provider_network, "_probe_endpoint", probe)
    caplog.set_level(logging.INFO, logger=provider_network.__name__)
    env = {
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(tmp_path / "codex"),
        "CLAUDE_HUB_PROVIDER_NETWORK_MODE": mode,
        "HTTPS_PROXY": "http://user:private-proxy-marker@proxy.invalid:8",
    }
    before = dict(env)
    result = await smoke._network_selection(env)
    assert result == {"requested_mode": mode, "proxy_selected": expected_proxy}
    assert len(seen) == probe_count
    assert all(endpoint == "https://chatgpt.com/backend-api/" for endpoint, _ in seen)
    assert login.await_count == 1
    assert env == before
    assert "private-proxy-marker" not in json.dumps(result)
    assert "private-proxy-marker" not in caplog.text


async def test_unknown_login_does_not_use_auto_compatibility_inherit(monkeypatch, tmp_path):
    from claude_hub.services.agent_stream import provider_network

    monkeypatch.setattr(provider_network, "_codex_login_status", AsyncMock(return_value=None))
    probe = AsyncMock(side_effect=AssertionError("an unknown account must not be probed"))
    monkeypatch.setattr(provider_network, "_probe_endpoint", probe)
    with pytest.raises(provider_network.ProviderEndpointUnknownError, match="smoke login mode"):
        await smoke._network_selection(
            {
                "HOME": str(tmp_path),
                "CODEX_HOME": str(tmp_path / "codex"),
                "CLAUDE_HUB_PROVIDER_NETWORK_MODE": "auto",
            }
        )
    assert probe.await_count == 0


def test_entry_filter_removes_only_known_fonts_and_resource_hints():
    html = (
        '<!doctype html>\n<link rel="preconnect" href="https://other.invalid">\n'
        '<link rel="dns-prefetch" href="//other.invalid">\n'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">\n'
        '<link rel="stylesheet" href="https://unknown.invalid/font.css">\n'
        '<link rel="stylesheet" href="/assets/main.css">\n'
        "<script>const sample = '<link href=\"https://fonts.gstatic.com/a\">';</script>"
    )
    filtered = smoke._strip_entry_hints(html)
    assert "preconnect" not in filtered and "dns-prefetch" not in filtered
    assert "fonts.googleapis.com" not in filtered
    assert 'href="https://unknown.invalid/font.css"' in filtered
    assert 'href="/assets/main.css"' in filtered
    assert "const sample" in filtered and "fonts.gstatic.com/a" in filtered


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/workspaces/tabs/other/stream/capabilities"),
        ("POST", "/api/workspaces/tabs/source/stream/retry"),
        ("PUT", "/api/workspaces/tabs/source/stream/mode"),
        ("GET", "/api/workspaces/tabs/source/stream/wait"),
        ("GET", "/api/terminal/proxy/source/"),
        ("POST", "/api/tabs/source/work"),
        ("POST", "/api/env-presets/bulk-import"),
        ("DELETE", "/api/feishu/bot/binding"),
        ("GET", "/api/tabs/other/work"),
        ("GET", "/assets/%2e%2e/api/tabs"),
    ],
)
def test_ui_rejects_unscoped_or_mutating_requests(method, path):
    boundary = smoke._WorkflowUiBoundary("http://127.0.0.1:43123", "source", "work")
    route = FakeRoute(method, "http://127.0.0.1:43123" + path)
    boundary.http(route)
    assert route.aborted and boundary.errors
    assert not route.fetched and route.fulfilled is None


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8173/api/tabs",
        "https://external.invalid/assets/app.js",
        "http://user:private-marker@127.0.0.1:43123/api/tabs",
    ],
)
def test_ui_rejects_foreign_origins_and_url_credentials(url):
    boundary = smoke._WorkflowUiBoundary("http://127.0.0.1:43123", "source", "work")
    route = FakeRoute("GET", url)
    boundary.http(route)
    assert route.aborted and not route.fetched
    assert "private-marker" not in json.dumps(boundary.errors)


def test_all_source_stream_endpoints_and_binding_are_local_fixtures(monkeypatch):
    monkeypatch.setattr(smoke.time, "sleep", lambda _: None)
    boundary = smoke._WorkflowUiBoundary("http://127.0.0.1:43123", "source", "work")
    for method, suffix, status in [
        ("GET", "capabilities", 200),
        ("GET", "events", 200),
        ("POST", "wait", 200),
        ("GET", "live", 204),
    ]:
        route = FakeRoute(method, "http://127.0.0.1:43123" + boundary.stream + "/" + suffix)
        boundary.http(route)
        assert route.fulfilled["status"] == status
        assert not route.fetched
        if suffix in {"events", "wait"}:
            assert route.fulfilled["json"] == {"events": [], "next_sequence": -1, "has_more": False}
    binding = FakeRoute("GET", "http://127.0.0.1:43123/api/feishu/bot/binding")
    boundary.http(binding)
    assert binding.fulfilled["json"] == {"binding": None}
    assert not binding.fetched and not boundary.errors
    assert not boundary.ready()  # Real work data is still required.


def test_wait_fixture_has_an_action_budget(monkeypatch):
    monkeypatch.setattr(smoke.time, "sleep", lambda _: None)
    boundary = smoke._WorkflowUiBoundary("http://127.0.0.1:43123", "source", "work")
    boundary.counts["wait"] = 256
    route = FakeRoute("POST", "http://127.0.0.1:43123" + boundary.stream + "/wait")
    boundary.http(route)
    assert route.aborted and not route.fetched
    assert "stream-wait-budget" in boundary.errors


def test_ui_closes_every_websocket_without_connecting_to_server():
    boundary = smoke._WorkflowUiBoundary("http://127.0.0.1:43123", "source", "work")
    route = SimpleNamespace(close=Mock())
    boundary.websocket(route)
    route.close.assert_called_once_with()
    assert boundary.errors == ["unexpected-websocket"]


def test_work_is_fetched_from_real_scoped_api_without_following_redirects():
    boundary = smoke._WorkflowUiBoundary("http://127.0.0.1:43123", "source", "work")
    rows = [{"id": "work", "source_tab_id": "source", "status": "completed"}]
    route = FakeRoute(
        "GET",
        "http://127.0.0.1:43123/api/tabs/source/work",
        FakeResponse(
            rows,
            headers={
                "content-type": "application/json",
                "content-encoding": "gzip",
                "content-length": "99",
            },
        ),
    )
    boundary.http(route)
    assert route.fetched == [{"max_redirects": 0, "timeout": 5000}]
    assert json.loads(route.fulfilled["body"]) == rows
    assert "content-encoding" not in route.fulfilled["headers"]
    assert "content-length" not in route.fulfilled["headers"]
    assert boundary.counts["work"] == 1 and not boundary.errors


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"id": "other", "source_tab_id": "source", "status": "completed"}],
        [{"id": "work", "source_tab_id": "source", "status": "running"}],
    ],
)
def test_work_response_must_match_the_completed_owned_work(rows):
    boundary = smoke._WorkflowUiBoundary("http://127.0.0.1:43123", "source", "work")
    route = FakeRoute("GET", "http://127.0.0.1:43123/api/tabs/source/work", FakeResponse(rows))
    boundary.http(route)
    assert route.aborted and boundary.errors and boundary.counts["work"] == 0


def test_real_redirect_is_not_forwarded_to_browser():
    boundary = smoke._WorkflowUiBoundary("http://127.0.0.1:43123", "source", "work")
    route = FakeRoute(
        "GET",
        "http://127.0.0.1:43123/",
        FakeResponse(status=302, headers={"location": "https://external.invalid"}),
    )
    boundary.http(route)
    assert route.fetched == [{"max_redirects": 0, "timeout": 5000}]
    assert route.aborted and route.fulfilled is None


def test_shell_is_private_and_preserves_quoted_values(tmp_path):
    shell = tmp_path / "shell"
    smoke._write_shell({"SHELL": str(shell), "HTTPS_PROXY": "http://u:p'quoted@proxy.invalid:8"})
    text = shell.read_text()
    assert "--noprofile --norc" in text and "unset BASH_ENV ENV CDPATH" in text
    assert "'\"'\"'" in text
    assert shell.stat().st_mode & 0o777 == 0o700


def test_absence_check_does_not_hide_permission_errors():
    path = SimpleNamespace(lstat=Mock(side_effect=PermissionError(errno.EACCES, "denied")))
    with pytest.raises(PermissionError):
        smoke._absent(path)


def test_process_guard_runs_even_when_known_child_term_fails(monkeypatch, tmp_path):
    process = Mock()
    process.poll.return_value = None
    process.terminate.side_effect = PermissionError(errno.EACCES, "denied")
    process.wait.return_value = 0
    core = Mock(return_value={"complete": True})
    monkeypatch.setattr(smoke.guard, "stop_owned_descendants", core)
    errors = []
    result = smoke._stop_stack([process], {}, tmp_path, tmp_path / "missing-socket", errors, "test")
    core.assert_called_once_with([process], term_timeout=3, kill_timeout=3)
    assert result["processes_verified"] is True
    assert result["complete"] is False and any(item.startswith("terminate:") for item in errors)


def test_stack_exception_cannot_skip_listener_or_sensitive_cleanup(monkeypatch, tmp_path):
    monkeypatch.setattr(smoke, "_stop_stack", Mock(side_effect=RuntimeError("failure")))
    sensitive = Mock(return_value=False)
    monkeypatch.setattr(smoke, "_remove_sensitive", sensitive)
    listener = SimpleNamespace(close=Mock(side_effect=OSError("close failed")))
    errors = []
    result = smoke._final_cleanup([], {}, tmp_path, tmp_path / "socket", listener, None, errors)
    listener.close.assert_called_once_with()
    sensitive.assert_called_once_with(tmp_path, False, errors)
    assert result["complete"] is False
    assert any(item.startswith("stack-cleanup:") for item in errors)
    assert any(item.startswith("listener-close:") for item in errors)


def test_sensitive_cleanup_includes_proxy_wrapper_and_provider_caches(tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / "codex").mkdir(parents=True)
    (runtime / "hub" / "launch_env").mkdir(parents=True)
    (runtime / "shell").write_text("private-proxy-marker")
    (runtime / "codex" / "auth.json").write_text("private-account-marker")
    (runtime / "codex" / "another-cache").write_text("private-cache-marker")
    (runtime / "hub" / "launch_env" / "tab.sh").write_text("private-launch-marker")
    raw = runtime / "raw.stderr"
    raw.write_text("unreviewed private output")
    source = tmp_path / "source-auth.json"
    source.write_text("original source must remain")
    errors = []
    assert smoke._remove_sensitive(runtime, True, errors)
    assert not errors and raw.exists() and source.exists()
    assert not (runtime / "shell").exists()
    assert not (runtime / "codex").exists()
    assert not (runtime / "hub" / "launch_env").exists()


def test_unknown_writers_never_produce_verified_sensitive_removal(tmp_path):
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex" / "auth.json").write_text("synthetic")
    (tmp_path / "shell").write_text("synthetic proxy")
    errors = []
    assert not smoke._remove_sensitive(tmp_path, False, errors)
    assert "sensitive-paths:unverified" in errors
    assert tmp_path.exists()


def test_result_write_failure_retains_private_runtime(monkeypatch, tmp_path, capsys):
    runtime, artifact = tmp_path / "runtime", tmp_path / "artifact"
    runtime.mkdir()
    artifact.mkdir()
    (runtime / "raw.stderr").write_text("private-value-never-published")
    monkeypatch.setattr(smoke, "_write_result", Mock(side_effect=PermissionError("denied")))
    result = {"scenario_passed": True, "cleanup": {"complete": True}, "errors": []}
    assert not smoke._finish_result(result, runtime, artifact)
    assert runtime.exists() and result["runtime_retained"]
    assert result["raw_evidence_is_private"] and not result["shareable_verified"]
    assert "private-value-never-published" not in capsys.readouterr().out


def test_incomplete_cleanup_cannot_pass_even_without_an_error_string(tmp_path, capsys):
    runtime, artifact = tmp_path / "runtime", tmp_path / "artifact"
    runtime.mkdir()
    artifact.mkdir()
    result = {"scenario_passed": True, "cleanup": {"complete": False}, "errors": []}
    assert not smoke._finish_result(result, runtime, artifact)
    assert runtime.exists() and result["status"] == "failed"
    capsys.readouterr()


def test_final_cleanup_closes_listener_before_port_probe(monkeypatch, tmp_path):
    events = []

    def stop(*args):
        events.append("stack")
        return {"complete": True, "processes_verified": True}

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def setsockopt(self, *args):
            pass

        def bind(self, address):
            assert events == ["stack", "listener"]
            assert address == ("127.0.0.1", 43123)
            events.append("probe")

    def sensitive(*args):
        events.append("sensitive")
        return True

    monkeypatch.setattr(smoke, "_stop_stack", stop)
    monkeypatch.setattr(smoke, "_remove_sensitive", sensitive)
    monkeypatch.setattr(
        smoke,
        "socket",
        SimpleNamespace(
            AF_INET=2,
            SOCK_STREAM=1,
            SOL_SOCKET=1,
            SO_REUSEADDR=2,
            socket=lambda *args: Probe(),
        ),
    )
    listener = SimpleNamespace(close=lambda: events.append("listener"))
    result = smoke._final_cleanup([], {}, tmp_path, tmp_path / "socket", listener, 43123, [])
    assert events == ["stack", "listener", "probe", "sensitive"]
    assert result["complete"] and result["port_available_after_close"]


def test_unverified_sensitive_cleanup_cannot_pass_without_an_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(
        smoke, "_stop_stack", Mock(return_value={"complete": True, "processes_verified": True})
    )
    monkeypatch.setattr(smoke, "_remove_sensitive", Mock(return_value=False))
    result = smoke._final_cleanup([], {}, tmp_path, tmp_path / "socket", None, None, [])
    assert not result["complete"]
