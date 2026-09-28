"""Tests for the provider-neutral agent-quoted image endpoint.

``GET /api/workspaces/tabs/{tab_id}/stream/quoted-image?key=...`` resolves a
bare provider image token through the resolver registry in
``services.agent_stream.quoted_images``. The Lark ``img_v3_`` resolver is the
first registered provider; the tests cover:

* the shared security base (key allowlist, root confinement, magic sniff,
  size, opaque 404) via the Lark resolver;
* provider dispatch — an unregistered bare token is never proxied;
* the extension point — a newly registered resolver owns its key shape with
  no route/contract change, and may return a redirect result.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest
from httpx import AsyncClient

from claude_hub.models import AgentType, ExecutionTarget, SessionKind
from claude_hub.services.agent_stream import quoted_images as qi

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32

KEY = "img_v3_0215v_f958a4be-ef9b-4a06-b887-0a048973208g"


def _make_tab(tab_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=tab_id,
        name=f"tab-{tab_id}",
        cwd="/tmp",
        remote_cwd=None,
        target=ExecutionTarget.LOCAL,
        remote_profile_id=None,
        remote_reconnect=True,
        solo_mode=True,
        env={},
        agent_session_id="provider-session-id",
        agent_session_id_verified=False,
        cursor_transport="terminal",
        cursor_data_dir=None,
        cursor_cli_version=None,
        cursor_transcript_path=None,
        cursor_transcript_schema=None,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
    )


@pytest.fixture
def resource_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stand-in lark resource root with one sender tree."""
    from claude_hub.api import agent_stream as agent_stream_api

    root = tmp_path / "tmp_img"
    resources = root / "chuxuan" / "lark-im-resources"
    resources.mkdir(parents=True)
    (resources / f"{KEY}.jpg").write_bytes(JPEG_BYTES)

    monkeypatch.setattr(qi, "default_lark_resource_root", lambda: root)
    monkeypatch.setattr(
        agent_stream_api.ttyd_manager,
        "get_tab",
        lambda tab_id: _make_tab("tab-a") if tab_id == "tab-a" else None,
    )
    return root


async def _get_image(client: AsyncClient, tab_id: str, key: str):
    return await client.get(
        f"/api/workspaces/tabs/{tab_id}/stream/quoted-image",
        params={"key": key},
    )


# ── Lark resolver through the neutral endpoint (security base) ─────────────


async def test_valid_key_returns_bytes_and_content_type(
    client: AsyncClient, resource_root: Path
) -> None:
    resp = await _get_image(client, "tab-a", KEY)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.content == JPEG_BYTES


async def test_png_sniff_across_another_sender_dir(
    client: AsyncClient, resource_root: Path
) -> None:
    other = resource_root / "sc" / "lark-im-resources"
    other.mkdir(parents=True)
    (other / "img_v3_abc-123.png").write_bytes(PNG_BYTES)
    resp = await _get_image(client, "tab-a", "img_v3_abc-123")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"


async def test_unknown_tab_is_404(client: AsyncClient, resource_root: Path) -> None:
    resp = await _get_image(client, "tab-other", KEY)
    assert resp.status_code == 404


async def test_missing_key_is_404(client: AsyncClient, resource_root: Path) -> None:
    resp = await _get_image(client, "tab-a", "img_v3_does-not-exist")
    assert resp.status_code == 404
    assert resp.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "bad_key",
    [
        "img_v2_0215v_f958a4be",  # wrong prefix
        "photo.jpg",  # bare filename, no registered prefix
        "img_v3_",  # prefix only
        "img_v3_a/../../etc/passwd",  # traversal with separators
        "img_v3_..",  # dot outside charset
        "img_v3_a/b",  # path separator
        "img_v3_a\x00b",  # control char
        "img_v3_a*",  # glob metacharacter
        "img_v3_a?b",  # glob metacharacter
        "../img_v3_abc",  # traversal prefix
        "",  # empty
    ],
)
async def test_malformed_keys_are_404(
    client: AsyncClient, resource_root: Path, bad_key: str
) -> None:
    resp = await _get_image(client, "tab-a", bad_key)
    assert resp.status_code == 404


async def test_non_image_bytes_even_with_image_extension_are_404(
    client: AsyncClient, resource_root: Path
) -> None:
    resources = resource_root / "sc" / "lark-im-resources"
    resources.mkdir(parents=True)
    (resources / "img_v3_lying.jpg").write_bytes(b"this is plain text, not a jpeg")
    resp = await _get_image(client, "tab-a", "img_v3_lying")
    assert resp.status_code == 404


async def test_symlink_resolving_outside_root_is_404(
    client: AsyncClient, resource_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(JPEG_BYTES)
    link = resource_root / "chuxuan" / "lark-im-resources" / "img_v3_escape.jpg"
    os.symlink(outside, link)
    resp = await _get_image(client, "tab-a", "img_v3_escape")
    assert resp.status_code == 404


async def test_file_outside_resources_dir_is_not_glob_reachable(
    client: AsyncClient, resource_root: Path
) -> None:
    # Files at the root / directly under a sender dir (missing the
    # lark-im-resources segment) must not be served.
    (resource_root / f"{KEY}.png").write_bytes(PNG_BYTES)
    (resource_root / "chuxuan" / "img_v3_loose.png").write_bytes(PNG_BYTES)
    resp_loose = await _get_image(client, "tab-a", "img_v3_loose")
    assert resp_loose.status_code == 404


# ── Provider dispatch / registry ───────────────────────────────────────────


async def test_unregistered_bare_key_is_not_proxied_404(
    client: AsyncClient, resource_root: Path
) -> None:
    # Well-formed-looking bare tokens from some other provider (Slack/DingTalk/
    # CDN/skill-local) must 404 until a resolver is registered for them.
    for foreign in ["slack_F1234567890ABCDE", "ding_abc-123", "cdn_xyz_789", "tmpimg_42"]:
        resp = await _get_image(client, "tab-a", foreign)
        assert resp.status_code == 404, foreign


def test_lark_is_registered_as_a_provider() -> None:
    names = [r.name for r in qi.registered_resolvers()]
    assert "lark-img-v3" in names


def test_registry_dispatch_picks_lark_for_img_v3() -> None:
    resolver = next(r for r in qi.registered_resolvers() if r.matches("img_v3_abc"))
    assert resolver.name == "lark-img-v3"
    # A foreign token matches no resolver at the dispatch entry point.
    with pytest.raises(qi.QuotedImageUnavailable):
        qi.resolve_quoted_image("slack_F1234567890ABCDE")


def test_extension_point_new_local_resolver_without_route_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Registering a provider is the only change needed to own a key shape."""

    class SkillTmpResolver(qi.LocalFileResolver):
        name = "skill-tmp"
        key_pattern = re.compile(r"skilltmp_[A-Za-z0-9_-]+")

        def roots(self):  # type: ignore[override]
            return [tmp_path / "skillimg"]

        def candidate_files(self, key):  # type: ignore[override]
            return [tmp_path / "skillimg" / f"{key}.png"]

    store = tmp_path / "skillimg"
    store.mkdir()
    (store / "skilltmp_42.png").write_bytes(PNG_BYTES)

    resolver = SkillTmpResolver()
    monkeypatch.setattr(qi, "_REGISTRY", [*qi.registered_resolvers(), resolver])

    result = qi.resolve_quoted_image("skilltmp_42")
    assert isinstance(result, qi.LocalImage)
    assert result.media_type == "image/png"
    # The Lark provider still owns its own shape (dispatch is multi-provider).
    assert any(r.matches("img_v3_x") for r in qi.registered_resolvers())


def test_extension_point_redirect_result_shape() -> None:
    """A future remote/CDN provider can return a RedirectImage."""

    class CdnResolver(qi.QuotedImageResolver):
        name = "cdn"
        key_pattern = re.compile(r"cdn_[A-Za-z0-9_-]+")

        def _resolve(self, key):  # type: ignore[override]
            return qi.RedirectImage(location=f"https://cdn.example.com/{key}.webp")

    resolver = CdnResolver()
    result = resolver.resolve("cdn_abc")
    assert isinstance(result, qi.RedirectImage)
    assert result.location == "https://cdn.example.com/cdn_abc.webp"


async def test_registered_redirect_resolver_gets_a_307(
    client: AsyncClient, resource_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import re as _re

    class CdnResolver(qi.QuotedImageResolver):
        name = "cdn-test"
        key_pattern = _re.compile(r"cdnredir_[A-Za-z0-9_-]+")

        def _resolve(self, key):  # type: ignore[override]
            return qi.RedirectImage(location="https://cdn.example.com/x.webp")

    monkeypatch.setattr(qi, "_REGISTRY", [*qi.registered_resolvers(), CdnResolver()])
    resp = await _get_image(client, "tab-a", "cdnredir_abc")
    assert resp.status_code == 307
    assert resp.headers["location"] == "https://cdn.example.com/x.webp"
    assert resp.headers["x-content-type-options"] == "nosniff"


async def test_missing_resource_root_is_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.api import agent_stream as agent_stream_api

    def get_tab(tab_id: str) -> Optional[SimpleNamespace]:
        return _make_tab("tab-a") if tab_id == "tab-a" else None

    monkeypatch.setattr(qi, "default_lark_resource_root", lambda: None)
    monkeypatch.setattr(agent_stream_api.ttyd_manager, "get_tab", get_tab)
    resp = await _get_image(client, "tab-a", KEY)
    assert resp.status_code == 404
