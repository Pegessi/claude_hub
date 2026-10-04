"""Canonical public URL resolution tests."""

import pytest

from claude_hub.services import public_base_url


@pytest.mark.parametrize(
    ("public_value", "provider_value", "expected"),
    [
        ("https://hub.example.com/", "https://provider.example.com", "https://hub.example.com"),
        (None, "https://provider.example.com/", "https://provider.example.com"),
    ],
)
def test_public_url_env_precedence(monkeypatch, public_value, provider_value, expected) -> None:
    if public_value is None:
        monkeypatch.delenv("CLAUDE_HUB_PUBLIC_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_HUB_PUBLIC_BASE_URL", public_value)
    monkeypatch.setenv("CLAUDE_HUB_PROVIDER_PUBLIC_URL", provider_value)

    assert public_base_url.get_public_base_url() == expected


def test_public_url_local_wildcard_fallback(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_HUB_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("CLAUDE_HUB_PROVIDER_PUBLIC_URL", raising=False)
    monkeypatch.setattr(public_base_url.settings, "host", "::")
    monkeypatch.setattr(public_base_url.settings, "port", 9123)

    assert public_base_url.get_public_base_url() == "http://127.0.0.1:9123"


@pytest.mark.parametrize(
    "value",
    [
        "ftp://hub.example.com",
        "https://user:secret@hub.example.com",
        "https://hub.example.com/path",
        "https://hub.example.com?forwarded=evil",
        "https://hub.example.com/#fragment",
    ],
)
def test_public_url_rejects_non_origin_values(monkeypatch, value) -> None:
    monkeypatch.setenv("CLAUDE_HUB_PUBLIC_BASE_URL", value)

    with pytest.raises(ValueError):
        public_base_url.get_public_base_url()
