"""Review reporting must use the Hub bind port, not a provider's inherited PORT."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from claude_hub.config import Settings
from claude_hub.services.workspace_manager.prompts import bootstrap


def test_hub_port_wins_over_inherited_provider_port_in_report_target(monkeypatch):
    monkeypatch.setenv("PORT", "11004")
    monkeypatch.setenv("CLAUDE_HUB_PORT", "8173")
    settings = Settings(_env_file=None)
    assert settings.port == 8173
    monkeypatch.setattr(bootstrap, "settings", settings)
    reviewer = SimpleNamespace(id="reviewer-4", remote_forward_port=None)
    base_url = bootstrap._BootstrapPromptsMixin._report_base_url(None, reviewer)
    assert (
        f"{base_url}/api/workspaces/sessions/{reviewer.id}/reports"
        == "http://localhost:8173/api/workspaces/sessions/reviewer-4/reports"
    )


def test_legacy_port_environment_remains_supported(monkeypatch):
    monkeypatch.delenv("CLAUDE_HUB_PORT", raising=False)
    monkeypatch.setenv("PORT", "18173")
    assert Settings(_env_file=None).port == 18173


def test_explicit_port_keyword_remains_supported(monkeypatch):
    monkeypatch.delenv("CLAUDE_HUB_PORT", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    assert Settings(port=18174, _env_file=None).port == 18174


@pytest.mark.parametrize("port", [8173, 18175])
def test_launcher_forwards_actual_bind_port_to_backend_environment(monkeypatch, tmp_path, port):
    from claude_hub import maso_probe
    from claude_hub import service_launcher as launcher_module

    captured = {}

    class FakeLauncher:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured.update(kwargs)

        def run(self):
            return 0

    monkeypatch.setenv("PORT", "11004")
    monkeypatch.setenv("CLAUDE_HUB_PORT", "9999")
    monkeypatch.setattr(launcher_module, "ServiceLauncher", FakeLauncher)
    monkeypatch.setattr(launcher_module, "RestartStore", lambda path: object())
    monkeypatch.setattr(launcher_module, "resolve_runtime_home", lambda: tmp_path)
    monkeypatch.setattr(launcher_module.shutil, "which", lambda name: f"/mock/{name}")
    monkeypatch.setattr(launcher_module.signal, "signal", lambda *args: None)
    monkeypatch.setattr(maso_probe, "main", lambda argv: 0)
    monkeypatch.setattr(launcher_module.sys, "argv", ["launcher", "--port", str(port)])
    # Wiring test: the listen guard is covered in test_service_restart.py.
    monkeypatch.setattr(launcher_module.settings, "allow_insecure_listen", True)

    assert launcher_module.main() == 0
    assert captured["env"]["CLAUDE_HUB_PORT"] == str(port)
    assert captured["env"]["PORT"] == "11004"
    assert captured["args"][1][-2:] == ["--port", str(port)]
    assert captured["args"][2] == f"http://127.0.0.1:{port}/health"
    # The real bind host is exported so the in-process lifespan guard sees it.
    assert captured["env"]["CLAUDE_HUB_BIND_HOST"] == "0.0.0.0"


@pytest.mark.parametrize("timeout", [45, 1800])
def test_review_verdict_timeout_accepts_bounds(timeout):
    settings = Settings(review_verdict_idle_timeout_seconds=timeout, _env_file=None)
    assert settings.review_verdict_idle_timeout_seconds == timeout


@pytest.mark.parametrize("timeout", [0, 44, 1801])
def test_review_verdict_timeout_rejects_out_of_bounds(timeout):
    with pytest.raises(ValidationError, match="review_verdict_idle_timeout_seconds"):
        Settings(review_verdict_idle_timeout_seconds=timeout, _env_file=None)


def test_remote_forward_port_overrides_local_hub_report_port(monkeypatch):
    monkeypatch.setenv("PORT", "11004")
    monkeypatch.setenv("CLAUDE_HUB_PORT", "8173")
    monkeypatch.setattr(bootstrap, "settings", Settings(_env_file=None))
    reviewer = SimpleNamespace(remote_forward_port=28173)
    assert (
        bootstrap._BootstrapPromptsMixin._report_base_url(None, reviewer)
        == "http://127.0.0.1:28173"
    )


def test_explicit_port_keyword_wins_over_both_inherited_ports(monkeypatch):
    monkeypatch.setenv("PORT", "11004")
    monkeypatch.setenv("CLAUDE_HUB_PORT", "8173")
    assert Settings(port=18174, _env_file=None).port == 18174
