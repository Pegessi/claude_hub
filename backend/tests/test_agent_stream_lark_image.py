"""Tests for the restricted Lark (Feishu) image endpoint.

``GET /api/workspaces/tabs/{tab_id}/stream/lark-image?key=img_v3_...`` maps a
bare ``img_v3_<key>`` token (as it appears in Lark markdown bodies) back to the
file lark-cli already downloaded under
``<root>/<sender>/lark-im-resources/img_v3_<key>.<ext>``.

It must stay a restricted reader:

* the key matches a strict ``img_v3_[A-Za-z0-9_-]+`` charset — no path
  separators, traversal, or glob metacharacters can reach the filesystem;
* the file is located by a fixed-shape glob inside the monkeypatched resource
  root — never by a caller-supplied path;
* a symlink that resolves outside the root is denied;
* only files whose magic bytes sniff as PNG/JPEG/GIF/WebP are served;
* the tab must exist (same ownership lookup as the other stream endpoints);
* every denial is an opaque 404 with ``X-Content-Type-Options: nosniff``.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest
from httpx import AsyncClient

from claude_hub.models import AgentType, ExecutionTarget, SessionKind

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
    """Stand-in for ``~/.claude/oncall/.tmp_img`` with one sender tree."""
    from claude_hub.api import agent_stream as agent_stream_api

    root = tmp_path / "tmp_img"
    resources = root / "chuxuan" / "lark-im-resources"
    resources.mkdir(parents=True)
    (resources / f"{KEY}.jpg").write_bytes(JPEG_BYTES)

    monkeypatch.setattr(agent_stream_api, "_lark_image_resource_root", lambda: root)
    monkeypatch.setattr(
        agent_stream_api.ttyd_manager,
        "get_tab",
        lambda tab_id: _make_tab("tab-a") if tab_id == "tab-a" else None,
    )
    return root


async def _get_image(client: AsyncClient, tab_id: str, key: str):
    return await client.get(
        f"/api/workspaces/tabs/{tab_id}/stream/lark-image",
        params={"key": key},
    )


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
    # The unknown-tab 404 comes from the shared tab lookup (no custom headers);
    # every *image* denial below carries the opaque nosniff response.
    resp = await _get_image(client, "tab-other", KEY)
    assert resp.status_code == 404


async def test_missing_key_is_404(client: AsyncClient, resource_root: Path) -> None:
    resp = await _get_image(client, "tab-a", "img_v3_does-not-exist")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "bad_key",
    [
        "img_v2_0215v_f958a4be",  # wrong prefix
        "photo.jpg",  # bare filename, no img_v3 prefix
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
    # Same key stem at the root and directly under a sender dir (missing the
    # lark-im-resources segment) must not be served.
    (resource_root / f"{KEY}.png").write_bytes(PNG_BYTES)
    sender = resource_root / "chuxuan"
    (sender / "img_v3_loose.png").write_bytes(PNG_BYTES)
    resp = await _get_image(client, "tab-a", KEY)
    # The valid <sender>/lark-im-resources/<key>.jpg still wins for KEY; a key
    # with no file in a resources dir must 404 even if a loose copy exists.
    assert resp.status_code == 200
    resp_loose = await _get_image(client, "tab-a", "img_v3_loose")
    assert resp_loose.status_code == 404


async def test_missing_resource_root_is_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.api import agent_stream as agent_stream_api

    def get_tab(tab_id: str) -> Optional[SimpleNamespace]:
        return _make_tab("tab-a") if tab_id == "tab-a" else None

    monkeypatch.setattr(agent_stream_api, "_lark_image_resource_root", lambda: None)
    monkeypatch.setattr(agent_stream_api.ttyd_manager, "get_tab", get_tab)
    resp = await _get_image(client, "tab-a", KEY)
    assert resp.status_code == 404
