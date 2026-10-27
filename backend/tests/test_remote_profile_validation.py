"""Validation for SSH destination fields on ``RemoteProfile``.

A destination that begins with ``-`` would be parsed by ssh as an option
(e.g. ``-oProxyCommand=...`` — argument injection). The pydantic layer
rejects option-looking / out-of-charset hosts and users, bounds the port,
and auto-discovery from ``~/.ssh/config`` skips invalid entries with a log
instead of crashing.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from claude_hub.models import RemoteProfile
from claude_hub.services import remote_profiles as remote_profiles_module
from claude_hub.services.remote_profiles import RemoteProfileManager


def _profile(**overrides: object) -> RemoteProfile:
    base: dict[str, object] = {"id": "p", "name": "p", "ssh_host": "example.invalid"}
    base.update(overrides)
    return RemoteProfile(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# ssh_host
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "host",
    [
        "example.invalid",
        "mac-mini.local",
        "merlin_dev",
        "merlin_dev_2",
        "host-with-dash.example.org",
        "host.example.",  # absolute FQDN with trailing dot
        "a",
        "127.0.0.1",
        "10.20.30.40",
        "::1",
        "2001:db8::1",
        "fe80::1",
    ],
)
def test_ssh_host_accepts_hostnames_aliases_and_ip_literals(host: str) -> None:
    assert _profile(ssh_host=host).ssh_host == host


def test_ssh_host_is_stripped() -> None:
    assert _profile(ssh_host="  example.invalid  ").ssh_host == "example.invalid"


def test_ssh_host_rejects_overlong_value() -> None:
    long_host = "a." * 127 + "a"  # 255 chars: over the 253 FQDN limit
    assert len(long_host) == 255
    with pytest.raises(ValidationError):
        _profile(ssh_host=long_host)


@pytest.mark.parametrize(
    "host",
    [
        "-oProxyCommand=touch /tmp/pwned",
        "-oProxyCommand=evil",
        "-o=5",
        "-x",
        "host name",
        "host;rm",
        "host$(id)",
        "[::1]",
        "[2001:db8::1]",
        "fe80::1%eth0",  # scoped IPv6: ssh needs brackets, which are rejected
        "2001:db8::1%ens3",
        "",
        "host..example",
        "host..example.",
    ],
)
def test_ssh_host_rejects_option_looking_and_ill_formed_values(host: str) -> None:
    with pytest.raises(ValidationError):
        _profile(ssh_host=host)


# --------------------------------------------------------------------------- #
# user
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("user", ["deploy", "tiger", "u", "a.b-c_d", "user123", "root.admin"])
def test_ssh_user_accepts_conservative_names(user: str) -> None:
    assert _profile(user=user).user == user


@pytest.mark.parametrize("user", ["-oProxyCommand=evil", "-deploy", "bad user", "user;id", "us$er"])
def test_ssh_user_rejects_option_looking_and_ill_formed_values(user: str) -> None:
    with pytest.raises(ValidationError):
        _profile(user=user)


def test_blank_ssh_user_normalizes_to_none() -> None:
    assert _profile(user=None).user is None
    assert _profile(user="").user is None
    assert _profile(user="   ").user is None


# --------------------------------------------------------------------------- #
# port
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("port", [1, 22, 2222, 65535])
def test_port_accepts_1_to_65535(port: int) -> None:
    assert _profile(port=port).port == port


@pytest.mark.parametrize("port", [-1, 0, 65536])
def test_port_rejects_out_of_range(port: int) -> None:
    with pytest.raises(ValidationError):
        _profile(port=port)


def test_port_defaults_to_22() -> None:
    assert _profile().port == 22


# --------------------------------------------------------------------------- #
# ~/.ssh/config discovery and the configured JSON file
# --------------------------------------------------------------------------- #


def test_discovery_skips_invalid_ssh_config_hosts(
    tmp_path: Path, monkeypatch: MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = tmp_path / "config"
    cfg.write_text(
        "Host mac_mini\n"
        "    HostName 127.0.0.1\n"
        "Host -oProxyCommand=evil\n"
        "    HostName 127.0.0.1\n"
        "Host *\n"
        "    User wildcard\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(remote_profiles_module, "SSH_CONFIG_FILE", cfg)
    manager = RemoteProfileManager(path=tmp_path / "missing.json")

    with caplog.at_level("WARNING"):
        profiles = {profile.id: profile for profile in manager.list_profiles()}

    assert "mac_mini" in profiles
    # The option-looking alias is skipped, and the wildcard pattern never was
    # a concrete profile; discovery did not raise.
    assert not any(profile_id.startswith("-") for profile_id in profiles)
    assert "Skipping SSH config host" in caplog.text
    assert "-oProxyCommand=evil" in caplog.text


def test_configured_json_skips_invalid_profile(tmp_path: Path) -> None:
    profiles_file = tmp_path / "remote_profiles.json"
    profiles_file.write_text(
        '[{"id": "good", "name": "good", "ssh_host": "example.invalid"},'
        ' {"id": "evil", "name": "evil", "ssh_host": "-oProxyCommand=evil"}]',
        encoding="utf-8",
    )
    manager = RemoteProfileManager(path=profiles_file)

    profiles = manager._load_configured_profiles()

    assert [profile.id for profile in profiles] == ["good"]
