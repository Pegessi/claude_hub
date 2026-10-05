"""Tests for the restricted agent-produced image endpoint.

``GET /api/workspaces/tabs/{tab_id}/stream/agent-image?path=...`` lets the
browser render an image an agent produced (a Codex/TraeX ``view_image`` call or
a Claude ``Read`` of an image). It must behave as a *restricted* reader, never
an arbitrary file-read primitive:

* the resolved real path must stay inside the tab's own working directory
  (absolute escapes, ``..`` traversal and symlink escapes are denied);
* only files whose magic bytes are a whitelisted image are served;
* scope is per-tab — another tab's working dir is out of bounds;
* every denial (bad path / missing / non-image / unknown tab / remote) is an
  opaque 404 with ``X-Content-Type-Options: nosniff``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest
from httpx import AsyncClient

from claude_hub.models import AgentType, ExecutionTarget, SessionKind

# Minimal magic-byte prefixes are enough: the endpoint sniffs magic bytes and
# never decodes the image, so arbitrary trailing data is acceptable.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def _make_tab(
    tab_id: str, cwd: str, target: ExecutionTarget = ExecutionTarget.LOCAL
) -> SimpleNamespace:
    return SimpleNamespace(
        id=tab_id,
        name=f"tab-{tab_id}",
        cwd=cwd,
        remote_cwd=None,
        target=target,
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
def tab_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The tab's own cwd, containing one valid PNG."""
    from claude_hub.api import agent_stream as agent_stream_api

    cwd = tmp_path / "workspace"
    cwd.mkdir()
    image = cwd / "shot.png"
    image.write_bytes(PNG_BYTES)
    tab = _make_tab("tab-a", str(cwd))
    monkeypatch.setattr(
        agent_stream_api.ttyd_manager, "get_tab", lambda tab_id: tab if tab_id == "tab-a" else None
    )
    return cwd


async def _get_image(client: AsyncClient, tab_id: str, path: str):
    return await client.get(
        f"/api/workspaces/tabs/{tab_id}/stream/agent-image",
        params={"path": path},
    )


def _write_image_reference(
    text: str,
    *,
    tab_id: str = "tab-a",
    kind: str = "text_delta",
    role: str = "assistant",
    split: bool = False,
) -> None:
    from claude_hub.services.agent_stream.store import AgentStreamStore

    path = AgentStreamStore("terminal-tabs", f"terminal-tab-{tab_id}").path
    path.parent.mkdir(parents=True, exist_ok=True)
    chunks = [text[: len(text) // 2], text[len(text) // 2 :]] if split else [text]
    events = [
        {
            "session_id": f"terminal-tab-{tab_id}",
            "tab_id": tab_id,
            "type": kind,
            "turn_id": "turn-a",
            "message_id": f"turn-a:{role}",
            "run_epoch": 1,
            "redacted": True,
            "payload": {
                "role": role,
                "assistant_text" if kind == "turn_completed" else "text": chunk,
            },
        }
        for chunk in chunks
    ]
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")


@pytest.mark.parametrize(
    "kind,split", [("text_delta", False), ("text_delta", True), ("turn_completed", False)]
)
async def test_assistant_referenced_temp_image_is_allowed(
    client: AsyncClient, tab_dir: Path, tmp_path: Path, kind: str, split: bool
) -> None:
    outside = tmp_path / "preview & detail.png"
    outside.write_bytes(PNG_BYTES)
    _write_image_reference(f"![Preview](<{outside}>)", kind=kind, split=split)
    resp = await _get_image(client, "tab-a", str(outside))
    assert resp.status_code == 200
    assert resp.content == PNG_BYTES


@pytest.mark.parametrize(
    "kind,role,tab_id",
    [
        ("text_delta", "user", "tab-a"),
        ("tool_call_completed", "assistant", "tab-a"),
        ("text_delta", "assistant", "tab-b"),
    ],
)
async def test_other_sources_do_not_grant_temp_image_access(
    client: AsyncClient, tab_dir: Path, tmp_path: Path, kind: str, role: str, tab_id: str
) -> None:
    outside = tmp_path / "private.png"
    outside.write_bytes(PNG_BYTES)
    _write_image_reference(f"![Preview]({outside})", kind=kind, role=role, tab_id=tab_id)
    assert (await _get_image(client, "tab-a", str(outside))).status_code == 404


async def test_temp_reference_grants_exact_file_only(
    client: AsyncClient, tab_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "preview.png"
    outside.write_bytes(PNG_BYTES)
    other = tmp_path / "other.png"
    other.write_bytes(PNG_BYTES)
    _write_image_reference(f"![Preview]({outside})")
    assert (await _get_image(client, "tab-a", str(other))).status_code == 404
    outside.write_text("not an image")
    assert (await _get_image(client, "tab-a", str(outside))).status_code == 404


async def test_referenced_temp_symlink_is_denied(
    client: AsyncClient, tab_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "linked.png"
    real = tmp_path / "target.png"
    real.write_bytes(PNG_BYTES)
    outside.symlink_to(real)
    _write_image_reference(f"![Preview]({outside})")
    assert (await _get_image(client, "tab-a", str(outside))).status_code == 404


async def test_reference_cannot_grant_non_temp_file(
    client: AsyncClient, tab_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.services.agent_stream import local_images

    # Model a non-temp file without inspecting or modifying the real home.
    monkeypatch.setattr(local_images, "_TEMP_IMAGE_ROOTS", (tmp_path / "temp",))
    outside = tmp_path / "private.png"
    outside.write_bytes(PNG_BYTES)
    _write_image_reference(f"![Preview]({outside})")
    assert (await _get_image(client, "tab-a", str(outside))).status_code == 404


async def test_path_mention_without_markdown_image_is_not_a_grant(
    client: AsyncClient, tab_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "private.png"
    outside.write_bytes(PNG_BYTES)
    _write_image_reference(f"File at {outside}. [Link]({outside})")
    assert (await _get_image(client, "tab-a", str(outside))).status_code == 404


async def test_valid_absolute_image_returns_bytes_and_content_type(
    client: AsyncClient, tab_dir: Path
) -> None:
    resp = await _get_image(client, "tab-a", str(tab_dir / "shot.png"))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.content == PNG_BYTES


async def test_valid_cwd_relative_image_is_anchored_at_cwd(
    client: AsyncClient, tab_dir: Path
) -> None:
    resp = await _get_image(client, "tab-a", "shot.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"


async def test_relative_subdir_path_is_allowed(client: AsyncClient, tab_dir: Path) -> None:
    nested = tab_dir / "tasks" / "run"
    nested.mkdir(parents=True)
    (nested / "g.png").write_bytes(PNG_BYTES)
    resp = await _get_image(client, "tab-a", os.path.join("tasks", "run", "g.png"))
    assert resp.status_code == 200


async def test_jpeg_sniff_content_type(client: AsyncClient, tab_dir: Path) -> None:
    (tab_dir / "pic.jpg").write_bytes(JPEG_BYTES)
    resp = await _get_image(client, "tab-a", "pic.jpg")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"


async def test_parent_traversal_is_denied(
    client: AsyncClient, tab_dir: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG_BYTES)
    # From cwd, climb out then reference a sibling image.
    resp = await _get_image(client, "tab-a", f"../{secret.name}")
    assert resp.status_code == 404
    assert resp.headers["x-content-type-options"] == "nosniff"


async def test_absolute_path_outside_cwd_is_denied(client: AsyncClient, tab_dir: Path) -> None:
    resp = await _get_image(client, "tab-a", "/etc/passwd")
    assert resp.status_code == 404


async def test_absolute_image_outside_cwd_is_denied(
    client: AsyncClient, tab_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG_BYTES)
    resp = await _get_image(client, "tab-a", str(outside))
    assert resp.status_code == 404


async def test_symlink_to_file_outside_cwd_is_denied(
    client: AsyncClient, tab_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "target.png"
    outside.write_bytes(PNG_BYTES)
    link = tab_dir / "link.png"
    os.symlink(outside, link)
    resp = await _get_image(client, "tab-a", str(link))
    assert resp.status_code == 404


async def test_symlink_to_etc_passwd_is_denied(client: AsyncClient, tab_dir: Path) -> None:
    link = tab_dir / "escape"
    os.symlink("/etc/passwd", link)
    resp = await _get_image(client, "tab-a", str(link))
    assert resp.status_code == 404


async def test_non_image_file_under_cwd_is_denied(client: AsyncClient, tab_dir: Path) -> None:
    (tab_dir / "notes.txt").write_bytes(b"this is plain text, not an image")
    resp = await _get_image(client, "tab-a", "notes.txt")
    assert resp.status_code == 404


async def test_image_extension_with_non_image_bytes_is_denied(
    client: AsyncClient, tab_dir: Path
) -> None:
    (tab_dir / "lying.png").write_bytes(b"not really a png")
    resp = await _get_image(client, "tab-a", "lying.png")
    assert resp.status_code == 404


async def test_missing_file_is_404(client: AsyncClient, tab_dir: Path) -> None:
    resp = await _get_image(client, "tab-a", "does-not-exist.png")
    assert resp.status_code == 404


async def test_empty_path_is_404(client: AsyncClient, tab_dir: Path) -> None:
    resp = await _get_image(client, "tab-a", "   ")
    assert resp.status_code == 404


async def test_unknown_tab_is_404(client: AsyncClient, tab_dir: Path) -> None:
    resp = await _get_image(client, "tab-other", "shot.png")
    assert resp.status_code == 404


async def test_cross_tab_cwd_is_out_of_scope(
    client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An image that lives in a different tab's cwd is not readable here."""
    from claude_hub.api import agent_stream as agent_stream_api

    cwd_a = tmp_path / "tabA"
    cwd_b = tmp_path / "tabB"
    cwd_a.mkdir()
    cwd_b.mkdir()
    (cwd_b / "b.png").write_bytes(PNG_BYTES)

    def get_tab(tab_id: str) -> Optional[SimpleNamespace]:
        if tab_id == "tab-a":
            return _make_tab("tab-a", str(cwd_a))
        if tab_id == "tab-b":
            return _make_tab("tab-b", str(cwd_b))
        return None

    monkeypatch.setattr(agent_stream_api.ttyd_manager, "get_tab", get_tab)

    # tab-a must not read tab-b's file even with its absolute path.
    resp = await _get_image(client, "tab-a", str(cwd_b / "b.png"))
    assert resp.status_code == 404
    # tab-b can read its own file — proving the 404 was scope, not a bad file.
    resp_own = await _get_image(client, "tab-b", "b.png")
    assert resp_own.status_code == 200


async def test_remote_session_cannot_read(
    client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.api import agent_stream as agent_stream_api

    cwd = tmp_path / "remote-ws"
    cwd.mkdir()
    (cwd / "r.png").write_bytes(PNG_BYTES)
    tab = _make_tab("tab-r", str(cwd), target=ExecutionTarget.REMOTE)
    monkeypatch.setattr(agent_stream_api.ttyd_manager, "get_tab", lambda tab_id: tab)
    resp = await _get_image(client, "tab-r", "r.png")
    assert resp.status_code == 404
