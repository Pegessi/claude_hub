"""Strict preview cleanup must not acknowledge an incomplete filesystem operation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_hub.services.agent_stream import attachments


@pytest.fixture
def preview_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "previews"
    monkeypatch.setattr(attachments, "_state_root", lambda: tmp_path)
    monkeypatch.setattr(attachments, "_attachments_root", lambda: root)
    return root


@pytest.fixture
def aliased_state_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    real_root = tmp_path / "real-state"
    real_root.mkdir()
    alias = tmp_path / "configured-state"
    alias.symlink_to(real_root, target_is_directory=True)
    monkeypatch.setattr(attachments, "_state_root", lambda: alias)
    return alias


_GIF = b"GIF89a\x01\x00\x01\x00\x00\x00\x00"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["unlink", "scandir", "rmdir"])
async def test_strict_clear_retries_real_filesystem_failure(preview_root: Path, operation: str):
    store = attachments.AgentStreamAttachmentStore("terminal-tabs", "retired-tab")
    other = attachments.AgentStreamAttachmentStore("terminal-tabs", "live-tab")
    await store.save("image/gif", _GIF)
    await other.save("image/gif", _GIF)
    original_index = attachments._index_path().read_bytes()

    with patch.object(attachments.os, operation, side_effect=PermissionError("injected failure")):
        with pytest.raises(PermissionError, match="injected failure"):
            await store.clear(strict=True)

    assert store._session_dir.exists()
    assert attachments._index_path().read_bytes() == original_index
    assert other._session_dir.exists()
    await store.clear(strict=True)
    assert not store._session_dir.exists()
    index = store._read_index(strict=True)
    assert not store._session_entries(index)
    assert len(other._session_entries(index)) == 1
    assert other._session_dir.exists()


@pytest.mark.asyncio
async def test_strict_clear_retries_index_replace_failure(preview_root: Path):
    store = attachments.AgentStreamAttachmentStore("terminal-tabs", "retired-tab")
    other = attachments.AgentStreamAttachmentStore("terminal-tabs", "live-tab")
    await store.save("image/gif", _GIF)
    await other.save("image/gif", _GIF)
    original_index = attachments._index_path().read_bytes()

    with patch.object(attachments.os, "replace", side_effect=OSError("index replace failed")):
        with pytest.raises(OSError, match="index replace failed"):
            await store.clear(strict=True)

    assert not store._session_dir.exists()
    assert attachments._index_path().read_bytes() == original_index
    await store.clear(strict=True)
    assert not store._session_entries(store._read_index(strict=True))
    assert len(other._session_entries(other._read_index(strict=True))) == 1
    assert other._session_dir.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [b"{unfinished", b"{}", b"[{}]"])
async def test_strict_clear_preserves_invalid_index_and_files(preview_root: Path, invalid: bytes):
    store = attachments.AgentStreamAttachmentStore("terminal-tabs", "retired-tab")
    await store.save("image/gif", _GIF)
    attachments._index_path().write_bytes(invalid)

    with pytest.raises((ValueError, json.JSONDecodeError)):
        await store.clear(strict=True)

    assert store._session_dir.exists()
    assert attachments._index_path().read_bytes() == invalid


@pytest.mark.parametrize("level", ["root", "workspace", "session"])
def test_strict_directory_cleanup_does_not_follow_symlinks(
    preview_root: Path, tmp_path: Path, level: str
):
    workspace_id, session_id = "terminal-tabs", "retired-tab"
    workspace_dir = preview_root / attachments._hash_component(workspace_id)
    session_dir = workspace_dir / attachments._hash_component(session_id)
    link = {"root": preview_root, "workspace": workspace_dir, "session": session_dir}[level]
    link.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "unrelated"
    outside.mkdir()
    sentinel = outside / "private.txt"
    sentinel.write_bytes(b"unrelated-owner")
    link.symlink_to(outside, target_is_directory=True)

    attachments._safe_clear_session_dir(workspace_id, session_id, strict=True)

    assert not link.is_symlink()
    assert sentinel.read_bytes() == b"unrelated-owner"


@pytest.mark.asyncio
async def test_strict_clear_missing_artifacts_is_idempotent(preview_root: Path):
    store = attachments.AgentStreamAttachmentStore("terminal-tabs", "retired-tab")
    await store.clear(strict=True)
    await store.clear(strict=True)
    assert not store._session_dir.exists()
    assert store._read_index(strict=True) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", [1, 2, 3])
async def test_strict_clear_reconfirms_removal_after_sync_failure(preview_root: Path, fail_at: int):
    store = attachments.AgentStreamAttachmentStore("terminal-tabs", "retired-tab")
    await store.save("image/gif", _GIF)
    sync = attachments.os.fsync
    calls = 0

    def fail_selected_sync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise OSError("injected sync failure")
        sync(fd)

    with patch.object(attachments.os, "fsync", side_effect=fail_selected_sync):
        with pytest.raises(OSError, match="injected sync failure"):
            await store.clear(strict=True)
    assert calls == fail_at
    assert not store._session_dir.exists()

    # Missing files on retry do not prove their prior unlink was durable.
    with patch.object(attachments.os, "fsync", side_effect=OSError("still not confirmed")):
        with pytest.raises(OSError, match="still not confirmed"):
            await store.clear(strict=True)
    await store.clear(strict=True)
    assert store._read_index(strict=True) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
async def test_strict_clear_never_writes_through_preexisting_index_temp(
    preview_root: Path, tmp_path: Path, link_kind: str
):
    store = attachments.AgentStreamAttachmentStore("terminal-tabs", "retired-tab")
    await store.save("image/gif", _GIF)
    unrelated = tmp_path / "unrelated-owner.txt"
    unrelated.write_bytes(b"unrelated private data")
    index_path = attachments._index_path()
    old_temp = index_path.with_suffix(index_path.suffix + ".tmp")
    if link_kind == "symlink":
        old_temp.symlink_to(unrelated)
    else:
        old_temp.hardlink_to(unrelated)

    await store.clear(strict=True)

    assert unrelated.read_bytes() == b"unrelated private data"
    assert old_temp.exists()
    assert not index_path.is_symlink()
    assert index_path.stat().st_mode & 0o777 == 0o600
    assert store._read_index(strict=True) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("with_preview", [False, True])
async def test_strict_clear_accepts_configured_root_alias(
    aliased_state_root: Path, with_preview: bool
):
    store = attachments.AgentStreamAttachmentStore("terminal-tabs", "retired-tab")
    if with_preview:
        await store.save("image/gif", _GIF)
    await store.clear(strict=True)
    await store.clear(strict=True)
    assert aliased_state_root.is_symlink()
    assert not store._session_dir.exists()
    assert store._read_index(strict=True) == []


@pytest.mark.asyncio
async def test_configured_root_alias_does_not_trust_child_symlinks(
    aliased_state_root: Path, tmp_path: Path
):
    store = attachments.AgentStreamAttachmentStore("terminal-tabs", "retired-tab")
    store._session_dir.parent.mkdir(parents=True)
    unrelated = tmp_path / "unrelated-preview-owner"
    unrelated.mkdir()
    sentinel = unrelated / "private.png"
    sentinel.write_bytes(b"unrelated data")
    store._session_dir.symlink_to(unrelated, target_is_directory=True)
    await store.clear(strict=True)
    assert aliased_state_root.is_symlink()
    assert not store._session_dir.is_symlink()
    assert sentinel.read_bytes() == b"unrelated data"


@pytest.mark.asyncio
async def test_default_clear_keeps_legacy_best_effort_behavior(preview_root: Path):
    store = attachments.AgentStreamAttachmentStore("terminal-tabs", "legacy-tab")
    await store.save("image/gif", _GIF)
    with patch.object(attachments.os, "unlink", side_effect=PermissionError("legacy failure")):
        await store.clear()
    assert store._session_dir.exists()
    assert not store._session_entries(store._read_index())
