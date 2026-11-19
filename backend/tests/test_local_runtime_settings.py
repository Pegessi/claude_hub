import json

from claude_hub.services import local_runtime_settings


def test_local_settings_use_isolated_file_and_environment_precedence(tmp_path, monkeypatch):
    monkeypatch.setattr(local_runtime_settings, "resolve_runtime_home", lambda: tmp_path)
    monkeypatch.delenv("EXAMPLE_SETTING", raising=False)
    (tmp_path / "local_runtime_settings.json").write_text(
        json.dumps({"EXAMPLE_SETTING": "from-file"})
    )
    assert local_runtime_settings.local_setting("EXAMPLE_SETTING") == "from-file"
    monkeypatch.setenv("EXAMPLE_SETTING", "from-env")
    assert local_runtime_settings.local_setting("EXAMPLE_SETTING") == "from-env"
    monkeypatch.delenv("EXAMPLE_SETTING")
    (tmp_path / "local_runtime_settings.json").write_text("{")
    assert local_runtime_settings.local_setting("EXAMPLE_SETTING", "fallback") == "fallback"


def test_gateway_host_override_adds_to_private_signatures(tmp_path, monkeypatch):
    from claude_hub.models import RemoteProfile
    from claude_hub.models.schemas import RemoteTransport
    from claude_hub.services.remote_profiles import resolve_transport

    monkeypatch.setattr(local_runtime_settings, "resolve_runtime_home", lambda: tmp_path)
    (tmp_path / "local_runtime_settings.json").write_text(
        json.dumps({"CLAUDE_HUB_PTY_GATEWAY_HOSTS": "private-gateway"})
    )
    monkeypatch.setenv("CLAUDE_HUB_PTY_GATEWAY_HOSTS", "additional-gateway")
    for host in ("private-gateway.example", "additional-gateway.example"):
        profile = RemoteProfile(id="test", name="Test", ssh_host=host)
        assert resolve_transport(profile) == RemoteTransport.PTY_GATEWAY
